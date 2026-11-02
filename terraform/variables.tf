variable "project_id" {
  description = "The Google Cloud project for the dataset, the connection and the Cloud Run function."
  type        = string
}

variable "region" {
  description = "The Cloud Run function's region. It must be in the dataset's location: any region of a multi-region (US, EU), or the dataset's own region."
  type        = string
  default     = "us-central1"
}

variable "location" {
  description = "The location of the dataset and the BigQuery connection: the location of the tables you query."
  type        = string
  default     = "US"
}

variable "dataset_id" {
  description = "The dataset that holds the routines, called as dataset.noul(...), dataset.choice(...) and so on."
  type        = string
  default     = "drex"
}

variable "name" {
  description = "The name of the function, the connection and the service accounts, and the prefix of the source bucket."
  type        = string
  default     = "drex"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,17}[a-z0-9]$", var.name))
    error_message = "name must be 2 to 19 lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "api_key_secret" {
  description = "The Secret Manager secret (its ID in project_id) whose latest version is the Nace API key. Create it outside Terraform, so the key never enters Terraform state."
  type        = string
}

variable "base_url" {
  description = "NACE_BASE_URL: the API's base URL. Empty for https://console.nace.ai; https://openrouter.ai/api with an OpenRouter key in api_key_secret."
  type        = string
  default     = ""
}

variable "openrouter_api_key_secret" {
  description = "Optional Secret Manager secret (its ID in project_id) whose latest version is an OpenRouter key, given to the function as OPENROUTER_API_KEY. With an OpenRouter base_url it is used instead of api_key_secret. Empty for none."
  type        = string
  default     = ""
}

variable "openrouter_referer" {
  description = "NACE_OPENROUTER_REFERER: the HTTP-Referer OpenRouter attributes requests to. Empty for https://nace.ai. Used only with an OpenRouter base_url."
  type        = string
  default     = ""
}

variable "openrouter_title" {
  description = "NACE_OPENROUTER_TITLE: the X-OpenRouter-Title OpenRouter shows for the app. Empty for Drex integration for BigQuery. Used only with an OpenRouter base_url."
  type        = string
  default     = ""
}

variable "max_instances" {
  description = "The most Cloud Run instances. The requests in flight at once are at most max_instances x container_concurrency x inflight_per_request, which should stay within the account's limit."
  type        = number
  default     = 1

  validation {
    condition     = var.max_instances >= 1 && floor(var.max_instances) == var.max_instances
    error_message = "max_instances must be a whole number, at least 1."
  }
}

variable "container_concurrency" {
  description = "The BigQuery batches one instance answers at once."
  type        = number
  default     = 1

  validation {
    condition     = var.container_concurrency >= 1 && floor(var.container_concurrency) == var.container_concurrency
    error_message = "container_concurrency must be a whole number, at least 1."
  }
}

variable "inflight_per_request" {
  description = "The Drex requests one batch has in flight at once."
  type        = number
  default     = 8

  validation {
    condition     = var.inflight_per_request >= 1 && floor(var.inflight_per_request) == var.inflight_per_request
    error_message = "inflight_per_request must be a whole number, at least 1."
  }
}

variable "max_batching_rows" {
  description = "The most rows BigQuery sends in one batch to noul, choice, score and decide."
  type        = number
  default     = 50

  validation {
    condition     = var.max_batching_rows >= 1 && floor(var.max_batching_rows) == var.max_batching_rows
    error_message = "max_batching_rows must be a whole number, at least 1."
  }
}

variable "image_max_batching_rows" {
  description = "The most rows BigQuery sends in one batch to decide_image. Google recommends 1 for functions over object tables."
  type        = number
  default     = 1

  validation {
    condition     = var.image_max_batching_rows >= 1 && floor(var.image_max_batching_rows) == var.image_max_batching_rows
    error_message = "image_max_batching_rows must be a whole number, at least 1."
  }
}

variable "timeout_seconds" {
  description = "The Cloud Run function's request timeout. A batch answers within this less 60 seconds, with NULL for rows still waiting, so the function never times out: BigQuery would send the whole batch again, and its answered rows would be billed again."
  type        = number
  default     = 300

  validation {
    condition     = var.timeout_seconds >= 120 && var.timeout_seconds <= 3600
    error_message = "timeout_seconds must be between 120 and 3600."
  }
}

variable "memory" {
  description = "The Cloud Run function's memory."
  type        = string
  default     = "512M"
}

variable "enable_apis" {
  description = "Whether to enable the APIs the module uses. They are left enabled on destroy."
  type        = bool
  default     = true
}

variable "delete_dataset_contents_on_destroy" {
  description = "Whether destroy deletes the dataset with any tables you added to it. When false, destroy fails on a dataset that holds tables of yours."
  type        = bool
  default     = false
}

variable "labels" {
  description = "Labels for the resources that take them."
  type        = map(string)
  default     = {}
}
