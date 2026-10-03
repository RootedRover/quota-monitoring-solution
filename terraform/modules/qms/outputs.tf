output "dashboard_url" {
  description = "Dashboard endpoint. Requires an identity token; see proxy_command."
  value       = google_cloud_run_v2_service.dashboard.uri
}

output "proxy_command" {
  description = "How a human actually opens the dashboard in a browser."
  value       = "gcloud run services proxy ${google_cloud_run_v2_service.dashboard.name} --region ${var.region} --project ${var.project_id}"
}

output "collector_job" {
  description = "Cloud Run job name, for manual execution."
  value       = google_cloud_run_v2_job.collector.name
}

output "build_command" {
  description = "Build and push a new image, scoped to the dedicated source bucket."
  value = join(" ", [
    "gcloud builds submit",
    "--region=${var.region}",
    "--config=cloudbuild.yaml",
    "--gcs-source-staging-dir=gs://${google_storage_bucket.build_source.name}/source",
    "--service-account=projects/${var.project_id}/serviceAccounts/${google_service_account.build.email}",
    "--project=${var.project_id}",
  ])
}

output "dataset" {
  description = "Fully-qualified dataset."
  value       = "${var.project_id}.${google_bigquery_dataset.quota.dataset_id}"
}

output "service_accounts" {
  description = "The four workload identities, for auditing."
  value = {
    collector = google_service_account.collector.email
    dashboard = google_service_account.dashboard.email
    build     = google_service_account.build.email
    scheduler = google_service_account.scheduler.email
  }
}
