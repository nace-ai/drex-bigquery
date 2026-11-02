locals {
  apis = toset([
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "bigqueryconnection.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])

  string  = jsonencode({ typeKind = "STRING" })
  json    = jsonencode({ typeKind = "JSON" })
  float64 = jsonencode({ typeKind = "FLOAT64" })

  # The routines. BigQuery remote functions take no optional arguments, so model is a required last argument
  # everywhere, and NULL means the default. user_defined_context names the routine
  routines = {
    noul = {
      description = "The probability, from 0 to 1, that the answer to instructions about input is yes. criteria: NULL, or a JSON object describing true and false."
      arguments   = { input = local.string, instructions = local.string, criteria = local.json, model = local.string }
      return_type = local.float64
      rows        = var.max_batching_rows
    }
    choice = {
      description = "The label input fits best: {choice, probabilities: [{value, probability}], confidence}. labels: a JSON array of 2 to 255 labels, of {label, description} objects, or an object of label to description."
      arguments   = { input = local.string, instructions = local.string, labels = local.json, model = local.string }
      return_type = local.json
      rows        = var.max_batching_rows
    }
    score = {
      description = "Where input sits on a scale: {score, probabilities: [{index, value, probability}], confidence}. levels: a JSON array of 2 to 255 levels, lowest first."
      arguments   = { input = local.string, instructions = local.string, levels = local.json, model = local.string }
      return_type = local.json
      rows        = var.max_batching_rows
    }
    decide = {
      description = "Drex's response body for JSON state and a JSON object of questions."
      arguments   = { state = local.json, questions = local.json, model = local.string }
      return_type = local.json
      rows        = var.max_batching_rows
    }
    decide_image = {
      description = "Drex's response body for JSON state, 1 to 8 images (signed Cloud Storage URLs from EXTERNAL_OBJECT_TRANSFORM) and a JSON object of questions. NULL model means drex-max."
      arguments   = { state = local.json, images = local.json, questions = local.json, model = local.string }
      return_type = local.json
      rows        = var.image_max_batching_rows
    }
  }

  # The order the function reads each call's arguments in
  argument_order = {
    noul         = ["input", "instructions", "criteria", "model"]
    choice       = ["input", "instructions", "labels", "model"]
    score        = ["input", "instructions", "levels", "model"]
    decide       = ["state", "questions", "model"]
    decide_image = ["state", "images", "questions", "model"]
  }

  function_environment = merge(
    {
      DREX_INFLIGHT_PER_REQUEST   = tostring(var.inflight_per_request)
      DREX_BATCH_DEADLINE_SECONDS = tostring(var.timeout_seconds - 60)
      # functions-framework's gunicorn threads: one per batch Cloud Run admits, so none waits for a thread
      THREADS = tostring(var.container_concurrency)
    },
    var.base_url == "" ? {} : { NACE_BASE_URL = var.base_url },
    var.openrouter_referer == "" ? {} : { NACE_OPENROUTER_REFERER = var.openrouter_referer },
    var.openrouter_title == "" ? {} : { NACE_OPENROUTER_TITLE = var.openrouter_title },
  )
}

resource "google_project_service" "apis" {
  for_each = var.enable_apis ? local.apis : toset([])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# The function's source, uploaded for Cloud Build, which builds and deploys it

data "archive_file" "function" {
  type        = "zip"
  source_dir  = "${path.module}/../function"
  output_path = "${path.root}/.terraform/tmp/${var.name}-function.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

resource "google_storage_bucket" "source" {
  project                     = var.project_id
  name                        = "${var.project_id}-${var.name}-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  labels                      = var.labels

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_object" "source" {
  bucket = google_storage_bucket.source.name
  # Named by its content, so a change to the source deploys again
  name   = "function-${data.archive_file.function.output_md5}.zip"
  source = data.archive_file.function.output_path
}

resource "google_service_account" "build" {
  project      = var.project_id
  account_id   = "${var.name}-build"
  display_name = "Builds the ${var.name} Cloud Run function"

  depends_on = [google_project_service.apis]
}

# Cloud Run Builder, the role Google's docs name for a build account: reads the function's source from Cloud Functions'
# bucket, writes the image and the build logs
resource "google_project_iam_member" "build" {
  project = var.project_id
  role    = "roles/run.builder"
  member  = google_service_account.build.member
}

# A new account's grant reaches Cloud Storage only after a while (76 to 82 s, measured), and the build's first step
# reads the source once, without retrying. Waits on the first apply only
resource "time_sleep" "build_role" {
  create_duration = "180s"
  triggers        = { member = google_project_iam_member.build.member }
}

resource "google_service_account" "function" {
  project      = var.project_id
  account_id   = "${var.name}-function"
  display_name = "Runs the ${var.name} Cloud Run function"

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "api_key" {
  project   = var.project_id
  secret_id = var.api_key_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.function.member
}

resource "google_secret_manager_secret_iam_member" "openrouter_api_key" {
  count = var.openrouter_api_key_secret == "" ? 0 : 1

  project   = var.project_id
  secret_id = var.openrouter_api_key_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.function.member
}

resource "google_cloudfunctions2_function" "drex" {
  project     = var.project_id
  name        = var.name
  location    = var.region
  description = "Drex integration for BigQuery: answers the remote functions of dataset ${var.dataset_id}"
  labels      = var.labels

  build_config {
    runtime         = "python312"
    entry_point     = "drex"
    service_account = google_service_account.build.id

    source {
      storage_source {
        bucket = google_storage_bucket.source.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    max_instance_count               = var.max_instances
    max_instance_request_concurrency = var.container_concurrency
    available_cpu                    = "1"
    available_memory                 = var.memory
    timeout_seconds                  = var.timeout_seconds
    service_account_email            = google_service_account.function.email
    # Only callers with the invoker role (the connection's service account) get in
    ingress_settings      = "ALLOW_ALL"
    environment_variables = local.function_environment

    secret_environment_variables {
      key        = "NACE_API_KEY"
      project_id = var.project_id
      secret     = var.api_key_secret
      version    = "latest"
    }

    dynamic "secret_environment_variables" {
      for_each = var.openrouter_api_key_secret == "" ? [] : [var.openrouter_api_key_secret]
      content {
        key        = "OPENROUTER_API_KEY"
        project_id = var.project_id
        secret     = secret_environment_variables.value
        version    = "latest"
      }
    }
  }

  depends_on = [
    time_sleep.build_role,
    google_secret_manager_secret_iam_member.api_key,
    google_secret_manager_secret_iam_member.openrouter_api_key,
  ]
}

# BigQuery calls the function as the connection's service account

resource "google_bigquery_connection" "drex" {
  project       = var.project_id
  connection_id = var.name
  location      = var.location
  friendly_name = "Drex"
  description   = "Calls the ${var.name} Cloud Run function for the remote functions of dataset ${var.dataset_id}"

  cloud_resource {}

  depends_on = [google_project_service.apis]
}

# BigQuery creates the connection's service account a little after the connection, and IAM refuses a grant to it until
# then. The invoker grant and the connection_service_account output take it from here, so every grant waits
resource "time_sleep" "connection_service_account" {
  create_duration = "60s"
  triggers        = { email = google_bigquery_connection.drex.cloud_resource[0].service_account_id }
}

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloudfunctions2_function.drex.service_config[0].service
  role     = "roles/run.invoker"
  member   = "serviceAccount:${time_sleep.connection_service_account.triggers.email}"
}

resource "google_bigquery_dataset" "drex" {
  project                    = var.project_id
  dataset_id                 = var.dataset_id
  location                   = var.location
  description                = "Drex's remote functions: noul, choice, score, decide and decide_image."
  delete_contents_on_destroy = var.delete_dataset_contents_on_destroy
  labels                     = var.labels

  depends_on = [google_project_service.apis]
}

resource "google_bigquery_routine" "drex" {
  for_each = local.routines

  project         = var.project_id
  dataset_id      = google_bigquery_dataset.drex.dataset_id
  routine_id      = each.key
  routine_type    = "SCALAR_FUNCTION"
  definition_body = ""
  description     = each.value.description
  return_type     = each.value.return_type

  dynamic "arguments" {
    for_each = local.argument_order[each.key]
    content {
      name      = arguments.value
      data_type = each.value.arguments[arguments.value]
    }
  }

  remote_function_options {
    endpoint             = google_cloudfunctions2_function.drex.service_config[0].uri
    connection           = google_bigquery_connection.drex.name
    max_batching_rows    = tostring(each.value.rows)
    user_defined_context = { fn = each.key }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.invoker]
}
