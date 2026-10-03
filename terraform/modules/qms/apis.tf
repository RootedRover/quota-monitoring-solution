# APIs the solution needs.
#
# disable_on_destroy is false throughout. These are project-wide switches, and
# tearing down this module must not break unrelated workloads that happen to
# use the same API.

locals {
  services = [
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudquotas.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
  ]
}

resource "google_project_service" "this" {
  for_each = toset(local.services)

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}
