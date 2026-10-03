variable "project_id" {
  type        = string
  description = "Project that hosts the dataset, the images and both Cloud Run workloads."
}

variable "organization_id" {
  type        = string
  description = <<-EOT
    Organisation to scan, e.g. "957650833838". The collector service account is
    granted read-only roles at this node, which is the only grant in this module
    that reaches outside the host project.
  EOT
}

variable "region" {
  type        = string
  description = <<-EOT
    Region for Artifact Registry, Cloud Run, Cloud Scheduler and the BigQuery
    dataset. Keeping them together avoids a cross-region read on every dashboard
    page load. Note BigQuery cannot relocate a dataset after creation.
  EOT
  default     = "asia-south1"
}

variable "dataset_id" {
  type        = string
  description = "BigQuery dataset holding quota_daily and the dashboard views."
  default     = "quota_monitoring"

  # RE2 refuses repetition counts above 1000, so the 1024-character BigQuery
  # limit has to be checked separately rather than as {1,1024}: a pattern that
  # fails to compile makes can() return false for every input.
  validation {
    condition     = can(regex("^[A-Za-z0-9_]+$", var.dataset_id)) && length(var.dataset_id) <= 1024
    error_message = "dataset_id must be 1-1024 letters, digits and underscores."
  }
}

variable "image" {
  type        = string
  description = <<-EOT
    Fully-qualified image reference for both workloads, e.g.
    "asia-south1-docker.pkg.dev/my-project/qms/qms:v6".

    This must already exist in Artifact Registry before the Cloud Run resources
    can be created; see the two-phase bootstrap in the module README.
  EOT
}

variable "dashboard_invokers" {
  type        = list(string)
  description = <<-EOT
    Principals granted roles/run.invoker on the dashboard, each as a fully
    qualified IAM member such as "user:alice@example.com" or
    "group:platform-team@example.com".

    Deliberately not defaulted to anything. An empty list means nobody but
    project administrators can reach the dashboard, which is the correct
    starting point; "allUsers" would publish your organisation's quota
    headroom to the internet.
  EOT
  default     = []

  validation {
    condition     = !contains(var.dashboard_invokers, "allUsers") && !contains(var.dashboard_invokers, "allAuthenticatedUsers")
    error_message = "Refusing to make the dashboard public. It exposes org-wide quota and capacity posture."
  }
}

variable "collection_schedule" {
  type        = string
  description = "Cron for the daily collection."
  default     = "30 2 * * *"
}

variable "schedule_time_zone" {
  type        = string
  description = "IANA time zone for collection_schedule."
  default     = "Etc/UTC"
}

variable "collector_memory" {
  type        = string
  description = "Memory for the collector job. Scales with the number of projects scanned."
  default     = "1Gi"
}

variable "collector_timeout" {
  type        = string
  description = "Per-task timeout for the collector job."
  default     = "1800s"
}

variable "dashboard_max_instances" {
  type        = number
  description = "Upper bound on dashboard instances. The dashboard is read-only and cached; it does not need to scale far."
  default     = 3
}

variable "build_source_retention_days" {
  type        = number
  description = "Days to keep uploaded build sources. They are ephemeral inputs, not artefacts."
  default     = 7
}

variable "delete_dataset_contents_on_destroy" {
  type        = bool
  description = <<-EOT
    Leave false. When false, `terraform destroy` fails rather than silently
    dropping the collected history, which is the only copy of it.
  EOT
  default     = false
}
