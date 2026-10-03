# The two workloads.
#
# One image, two deployments. The collector Job and the dashboard Service share
# every line of model code, so splitting them into separate images would buy
# nothing but drift between the ratio logic that writes and the ratio logic that
# displays.

# ------------------------------------------------------------ collector

resource "google_cloud_run_v2_job" "collector" {
  project  = var.project_id
  location = var.region
  name     = "qms-collector"

  deletion_protection = false

  template {
    template {
      service_account = google_service_account.collector.email
      timeout         = var.collector_timeout
      max_retries     = 1

      containers {
        image   = var.image
        command = ["python"]
        args    = ["-m", "collector.cli", "collect"]

        resources {
          limits = {
            cpu    = "1"
            memory = var.collector_memory
          }
        }

        env {
          name  = "QMS_PROJECT"
          value = var.project_id
        }
        env {
          name  = "QMS_ORG"
          value = var.organization_id
        }
        env {
          name  = "QMS_DATASET"
          value = google_bigquery_dataset.quota.dataset_id
        }
        env {
          name  = "QMS_BQ_LOCATION"
          value = var.region
        }
      }
    }
  }

  depends_on = [google_project_service.this]
}

# ------------------------------------------------------------ dashboard

resource "google_cloud_run_v2_service" "dashboard" {
  project     = var.project_id
  location    = var.region
  name        = "qms-dashboard"
  iap_enabled = var.iap_enabled

  deletion_protection = false

  # Access is controlled by IAM (see google_cloud_run_v2_service_iam_member in
  # iam.tf), not by network position. Ingress stays open because the supported
  # access path -- `gcloud run services proxy` -- reaches the service over the
  # public endpoint with an identity token attached. Restricting ingress here
  # would break that without adding protection: an unauthenticated request is
  # already rejected.
  #
  # When IAP and an external load balancer land, this becomes
  # INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.dashboard.email

    scaling {
      # Zero minimum: the dashboard is consulted occasionally, and a cold start
      # is cheaper than a warm instance idling all day.
      min_instance_count = 0
      max_instance_count = var.dashboard_max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "QMS_PROJECT"
        value = var.project_id
      }
      env {
        name  = "QMS_DATASET"
        value = google_bigquery_dataset.quota.dataset_id
      }
      env {
        name  = "QMS_BQ_LOCATION"
        value = var.region
      }
      env {
        name  = "QMS_ORG"
        value = var.organization_id
      }
      env {
        name  = "QMS_AUTHZ_MODE"
        value = "enforced"
      }
      env {
        name  = "QMS_IAP_AUDIENCE"
        value = "/projects/${data.google_project.this.number}/locations/${var.region}/services/qms-dashboard"
      }

      # Liveness only. /healthz never touches BigQuery on purpose: conflating
      # "is the process serving?" with "is the warehouse up?" turns a transient
      # BigQuery error into a restart loop.
      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 6
      }

      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds = 30
      }
    }
  }

  depends_on = [google_project_service.this]
}
