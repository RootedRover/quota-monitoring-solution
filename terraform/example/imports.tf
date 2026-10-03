# Adoption of the resources created by hand during the first deployment.
#
# These are declarative `import` blocks (Terraform >= 1.5) rather than
# `terraform import` commands, so the adoption itself is reviewable in the diff
# and reproducible by anyone replaying this migration.
#
# Delete this file once `terraform plan` is clean and the state has been
# written; keeping it costs nothing but it is noise after the first apply.
#
# NOT listed here, on purpose -- four over-broad bindings created during the
# first deployment that this module deliberately does not reproduce:
#
#   qms-build     roles/storage.objectAdmin       (project)
#   qms-build     roles/artifactregistry.writer   (project)
#   qms-collector roles/bigquery.dataEditor       (project)
#   qms-dashboard roles/bigquery.dataViewer       (project)
#
# Terraform cannot revoke what it never knew about, so importing them would be
# the wrong move: it would put them under management and then keep them. They
# are removed by scripts/revoke-legacy-iam.sh, which must run after this apply.

# ------------------------------------------------------------- accounts

import {
  to = module.qms.google_service_account.collector
  id = "projects/${var.project_id}/serviceAccounts/qms-collector@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = module.qms.google_service_account.dashboard
  id = "projects/${var.project_id}/serviceAccounts/qms-dashboard@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = module.qms.google_service_account.build
  id = "projects/${var.project_id}/serviceAccounts/qms-build@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = module.qms.google_service_account.scheduler
  id = "projects/${var.project_id}/serviceAccounts/qms-scheduler@${var.project_id}.iam.gserviceaccount.com"
}

# ------------------------------------------------------------- org roles

import {
  to = module.qms.google_organization_iam_member.collector["roles/monitoring.viewer"]
  id = "${var.organization_id} roles/monitoring.viewer serviceAccount:qms-collector@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = module.qms.google_organization_iam_member.collector["roles/cloudquotas.viewer"]
  id = "${var.organization_id} roles/cloudquotas.viewer serviceAccount:qms-collector@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = module.qms.google_organization_iam_member.collector["roles/browser"]
  id = "${var.organization_id} roles/browser serviceAccount:qms-collector@${var.project_id}.iam.gserviceaccount.com"
}

# --------------------------------------------------------- project roles
#
# Only the three that survive the narrowing.

import {
  to = module.qms.google_project_iam_member.collector_jobs
  id = "${var.project_id} roles/bigquery.jobUser serviceAccount:qms-collector@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = module.qms.google_project_iam_member.dashboard_jobs
  id = "${var.project_id} roles/bigquery.jobUser serviceAccount:qms-dashboard@${var.project_id}.iam.gserviceaccount.com"
}

import {
  to = module.qms.google_project_iam_member.build_logs
  id = "${var.project_id} roles/logging.logWriter serviceAccount:qms-build@${var.project_id}.iam.gserviceaccount.com"
}

# ------------------------------------------------------------- resources

import {
  to = module.qms.google_bigquery_dataset.quota
  id = "projects/${var.project_id}/datasets/quota_monitoring"
}

import {
  to = module.qms.google_artifact_registry_repository.qms
  id = "projects/${var.project_id}/locations/${var.region}/repositories/qms"
}

import {
  to = module.qms.google_cloud_run_v2_job.collector
  id = "projects/${var.project_id}/locations/${var.region}/jobs/qms-collector"
}

import {
  to = module.qms.google_cloud_run_v2_service.dashboard
  id = "projects/${var.project_id}/locations/${var.region}/services/qms-dashboard"
}

import {
  to = module.qms.google_cloud_scheduler_job.daily_collect
  id = "projects/${var.project_id}/locations/${var.region}/jobs/qms-daily-collect"
}

import {
  to = module.qms.google_cloud_run_v2_job_iam_member.scheduler_runs_collector
  id = "projects/${var.project_id}/locations/${var.region}/jobs/qms-collector roles/run.invoker serviceAccount:qms-scheduler@${var.project_id}.iam.gserviceaccount.com"
}
