terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.0"
    }
  }

  # Local state by default so this example works out of the box. For anything
  # shared, move it to GCS: state contains every resource id and, for some
  # resource types, values you would not want in a git repository.
  #
  # backend "gcs" {
  #   bucket = "my-tfstate-bucket"
  #   prefix = "qms"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "qms" {
  source = "../modules/qms"

  project_id      = var.project_id
  organization_id = var.organization_id
  region          = var.region
  image           = var.image
  dataset_id      = var.dataset_id

  dashboard_invokers = var.dashboard_invokers

  collection_schedule = var.collection_schedule
  schedule_time_zone  = var.schedule_time_zone
}

output "dashboard_url" {
  value = module.qms.dashboard_url
}

output "proxy_command" {
  value = module.qms.proxy_command
}

output "build_command" {
  value = module.qms.build_command
}

output "service_accounts" {
  value = module.qms.service_accounts
}
