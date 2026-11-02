output "dataset_id" {
  description = "The dataset that holds the routines."
  value       = google_bigquery_dataset.drex.dataset_id
}

output "routines" {
  description = "The routines' fully qualified names."
  value       = { for name, routine in google_bigquery_routine.drex : name => "${var.project_id}.${routine.dataset_id}.${routine.routine_id}" }
}

output "function_uri" {
  description = "The Cloud Run function's URL, the routines' endpoint."
  value       = google_cloudfunctions2_function.drex.service_config[0].uri
}

output "connection_name" {
  description = "The BigQuery connection, as projects/<project>/locations/<location>/connections/<id>."
  value       = google_bigquery_connection.drex.name
}

output "connection_service_account" {
  description = "The connection's service account. Grant it roles/storage.objectViewer on the buckets of an object table you pass to decide_image."
  value       = time_sleep.connection_service_account.triggers.email
}

output "function_service_account" {
  description = "The service account the Cloud Run function runs as."
  value       = google_service_account.function.email
}
