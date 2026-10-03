# Data and artefact storage.

# ------------------------------------------------------------- BigQuery
#
# Terraform owns the dataset; the collector owns the table and the views.
#
# That split is deliberate. The table schema lives in collector/sinks/bigquery.py
# and the view SQL in collector/sinks/views.py, both applied idempotently on
# every run. Restating either here would create two sources of truth for the
# same structure, and the first schema change would silently diverge them.

resource "google_bigquery_dataset" "quota" {
  project    = var.project_id
  dataset_id = var.dataset_id
  location   = var.region

  friendly_name = "Quota monitoring"
  description   = "Daily quota usage, limits and derived ratios. Table and views are managed by the collector."

  # Deliberately NOT setting default_partition_expiration_ms. The previous
  # generation of this solution set it to one day, which made a 30-day peak
  # arithmetically impossible while appearing to work. Retention belongs on the
  # table, where the collector sets it to 400 days.

  delete_contents_on_destroy = var.delete_dataset_contents_on_destroy

  depends_on = [google_project_service.this]
}

# -------------------------------------------------------- build source
#
# A dedicated bucket, so the build service account can be given read on exactly
# this and nothing else. The alternative -- the shared gs://PROJECT_cloudbuild
# that `gcloud builds submit` creates by default -- is used by every build in
# the project, so granting access to it grants access to other teams' sources.
#
# Pass it explicitly at build time:
#   gcloud builds submit --gcs-source-staging-dir=gs://<this bucket>/source ...

resource "google_storage_bucket" "build_source" {
  project  = var.project_id
  name     = "${var.project_id}-qms-build-source"
  location = var.region

  # Uploaded sources are ephemeral inputs to a build, not artefacts worth
  # keeping. The image in Artifact Registry is the artefact.
  lifecycle_rule {
    condition {
      age = var.build_source_retention_days
    }
    action {
      type = "Delete"
    }
  }

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Guards against a source tarball being overwritten between upload and build.
  versioning {
    enabled = true
  }

  force_destroy = true

  depends_on = [google_project_service.this]
}

# ---------------------------------------------------- Artifact Registry

resource "google_artifact_registry_repository" "qms" {
  project       = var.project_id
  location      = var.region
  repository_id = "qms"
  description   = "Quota Monitoring Solution images"
  format        = "DOCKER"

  docker_config {
    immutable_tags = false
  }

  depends_on = [google_project_service.this]
}
