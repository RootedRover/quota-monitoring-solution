# Identity and access.
#
# The governing rule here is one service account per workload, each holding the
# narrowest role that lets it do its job and nothing else. Four accounts rather
# than one shared identity means a compromise of the dashboard cannot corrupt
# the data, and a compromise of the build pipeline cannot read the organisation.
#
# Only the collector holds anything at the organisation node. Everything else is
# scoped to the host project or, where the API supports it, to the individual
# resource.

data "google_project" "this" {
  project_id = var.project_id
}

# ---------------------------------------------------------------- accounts

resource "google_service_account" "collector" {
  project      = var.project_id
  account_id   = "qms-collector"
  display_name = "QMS collector job"
  description  = "Reads quota usage and limits org-wide; writes the daily rollup to BigQuery."
}

resource "google_service_account" "dashboard" {
  project      = var.project_id
  account_id   = "qms-dashboard"
  display_name = "QMS dashboard service"
  description  = "Reads the BigQuery views. Has no write access to anything."
}

resource "google_service_account" "build" {
  project      = var.project_id
  account_id   = "qms-build"
  display_name = "QMS Cloud Build"
  description  = "Builds and pushes the container image. Cannot read quota data."
}

resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "qms-scheduler"
  display_name = "QMS scheduler trigger"
  description  = "Starts the collector job on a schedule. Can invoke that one job and nothing else."
}

# ------------------------------------------------- collector: organisation
#
# These three are the only grants in this module above the project. They are
# read-only, and they are what makes an org-wide sweep possible at all:
#
#   monitoring.viewer   -- PromQL reads of serviceruntime quota metrics
#   cloudquotas.viewer  -- authoritative limits, refresh intervals, dimensions
#   browser             -- walk the folder/project tree to discover what to scan
#
# roles/browser is the smallest role that permits the Resource Manager v3 walk;
# it grants read on the hierarchy and confers no access to anything inside the
# projects it lists.

locals {
  collector_org_roles = [
    "roles/monitoring.viewer",
    "roles/cloudquotas.viewer",
    "roles/browser",
  ]
}

resource "google_organization_iam_member" "collector" {
  for_each = toset(local.collector_org_roles)

  org_id = var.organization_id
  role   = each.value
  member = google_service_account.collector.member
}

# ----------------------------------------------------- collector: project
#
# dataEditor rather than dataOwner: the collector writes rows and replaces
# partitions, but must not be able to delete the dataset itself.

resource "google_bigquery_dataset_iam_member" "collector_writes" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.quota.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_service_account.collector.member
}

# jobUser is project-level because that is the only scope BigQuery offers for
# the right to run a query or load job. It confers no data access on its own.
resource "google_project_iam_member" "collector_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = google_service_account.collector.member
}

# ----------------------------------------------------- dashboard: project
#
# Scoped to the one dataset, and read-only within it. The dashboard issues
# SELECTs against precomputed views and has no write path to anything.

resource "google_bigquery_dataset_iam_member" "dashboard_reads" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.quota.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = google_service_account.dashboard.member
}

resource "google_project_iam_member" "dashboard_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = google_service_account.dashboard.member
}

# Read-only IAM policy analysis across the organisation so the dashboard can
# enforce Option B per-user row-level authorization (filtering projects to those
# where the signed-in caller holds cloudquotas.quotaInfos.list at Org, Folder,
# or Project scope).
#
# Cloud Asset analyzeIamPolicy requires:
#   * roles/cloudasset.viewer                (analyzeIamPolicy, searchAllIamPolicies, searchAllResources)
#   * roles/iam.securityReviewer             (iam.roles.get for custom roles + getIamPolicy fallback)
#   * roles/serviceusage.serviceUsageConsumer on the host project (serviceusage.services.use)
locals {
  dashboard_org_roles = [
    "roles/cloudasset.viewer",
    "roles/iam.securityReviewer",
  ]
}

resource "google_organization_iam_member" "dashboard_iam_analyzer" {
  for_each = toset(local.dashboard_org_roles)

  org_id = var.organization_id
  role   = each.value
  member = google_service_account.dashboard.member
}

resource "google_project_iam_member" "dashboard_service_usage" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = google_service_account.dashboard.member
}

# --------------------------------------------------------- build: narrowed
#
# The first cut of this deployment gave the build account project-wide
# roles/storage.objectAdmin and project-wide roles/artifactregistry.writer,
# because that is what makes `gcloud builds submit` work on the first try. Both
# are wider than necessary:
#
#   * objectAdmin over the whole project let the build read and overwrite every
#     bucket, including the shared gs://PROJECT_cloudbuild used by unrelated
#     builds. It is replaced by objectViewer on a dedicated source bucket this
#     module owns -- read-only, one bucket. The build only ever needs to *fetch*
#     the tarball; the human or CI runner uploading it authenticates as itself.
#
#   * artifactregistry.writer over the project let the build push to any
#     repository. It is replaced by the same role scoped to this repository.
#
# logging.logWriter stays project-level because Cloud Logging offers no
# narrower scope, and it is required by options.logging=CLOUD_LOGGING_ONLY in
# cloudbuild.yaml. Using Cloud Logging is itself the least-privilege choice: the
# alternative, a regional logs bucket, needs roles/storage.admin on that bucket.

resource "google_storage_bucket_iam_member" "build_reads_source" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.build.member
}

resource "google_artifact_registry_repository_iam_member" "build_pushes" {
  project    = var.project_id
  location   = google_artifact_registry_repository.qms.location
  repository = google_artifact_registry_repository.qms.name
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.build.member
}

resource "google_project_iam_member" "build_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = google_service_account.build.member
}

# Cloud Run pulls images as its own service agent, not as the workload's service
# account. Granting it reader on just this repository makes the dependency
# explicit rather than relying on a project-level grant that Google may or may
# not have added when the API was enabled.
resource "google_artifact_registry_repository_iam_member" "run_pulls" {
  project    = var.project_id
  location   = google_artifact_registry_repository.qms.location
  repository = google_artifact_registry_repository.qms.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:service-${data.google_project.this.number}@serverless-robot-prod.iam.gserviceaccount.com"
}

# ------------------------------------------------------ scheduler: one job
#
# Bound to the job resource, not the project. The scheduler can start this
# collector and has no standing to invoke the dashboard or any future workload.

resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_collector" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.collector.name
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler.member
}

# ------------------------------------------------------- dashboard callers
#
# Explicit allow-list, bound to the service. Empty by default; see the variable.

resource "google_cloud_run_v2_service_iam_member" "dashboard_invokers" {
  for_each = toset(var.dashboard_invokers)

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.dashboard.name
  role     = "roles/run.invoker"
  member   = each.value
}

# When Direct Cloud Run IAP is enabled, Google's IAP service agent invokes the
# Cloud Run revision on behalf of authenticated users, and dashboard_invokers
# are granted roles/iap.httpsResourceAccessor on the IAP-secured service.
resource "google_cloud_run_v2_service_iam_member" "iap_service_agent_invoker" {
  count = var.iap_enabled ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.dashboard.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-iap.iam.gserviceaccount.com"

  depends_on = [google_cloud_run_v2_service.dashboard]
}

resource "google_iap_web_cloud_run_service_iam_member" "dashboard_iap_accessors" {
  for_each = var.iap_enabled ? toset(var.dashboard_invokers) : toset([])

  project                = var.project_id
  location               = var.region
  cloud_run_service_name = google_cloud_run_v2_service.dashboard.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value

  depends_on = [google_cloud_run_v2_service.dashboard]
}

moved {
  from = google_organization_iam_member.dashboard_iam_analyzer
  to   = google_organization_iam_member.dashboard_iam_analyzer["roles/cloudasset.viewer"]
}
