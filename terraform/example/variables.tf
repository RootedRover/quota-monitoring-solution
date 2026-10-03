variable "project_id" {
  type        = string
  description = "Project hosting the deployment."
}

variable "organization_id" {
  type        = string
  description = "Organisation to scan."
}

variable "region" {
  type    = string
  default = "asia-south1"
}

variable "image" {
  type        = string
  description = "Image reference; must exist before the Cloud Run resources are created."
}

variable "dashboard_invokers" {
  type        = list(string)
  description = "Who may open the dashboard, e.g. [\"user:you@example.com\"]."
  default     = []
}

variable "dataset_id" {
  type    = string
  default = "quota_monitoring"
}

variable "collection_schedule" {
  type        = string
  description = "Cron for the daily collection, in schedule_time_zone."
  default     = "30 2 * * *"
}

variable "schedule_time_zone" {
  type    = string
  default = "Asia/Kolkata"
}
