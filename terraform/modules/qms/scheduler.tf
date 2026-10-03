# Daily trigger for the collector.
#
# Cloud Scheduler calls the Cloud Run Admin API with an OAuth token minted for
# the scheduler service account, which holds run.invoker on this one job.
#
# The collector is idempotent: it re-collects whole days and replaces the
# affected partitions, so a duplicate or retried trigger cannot double-count.
# That is what makes an unattended daily schedule safe.

resource "google_cloud_scheduler_job" "daily_collect" {
  project   = var.project_id
  region    = var.region
  name      = "qms-daily-collect"
  schedule  = var.collection_schedule
  time_zone = var.schedule_time_zone

  description = "Runs the QMS collector once a day."

  # Quota metrics arrive with some lag and the dashboard is explicitly not
  # real-time, so a failed run can simply wait for tomorrow rather than
  # hammering the API.
  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.collector.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [
    google_project_service.this,
    google_cloud_run_v2_job_iam_member.scheduler_runs_collector,
  ]
}
