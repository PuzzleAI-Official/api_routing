resource "google_cloud_run_v2_service" "service" {
  name     = var.name
  location = var.region
  labels   = var.labels

  template {
    service_account = var.service_account_email
    scaling {
      min_instance_count = 1
      max_instance_count = 10
    }
    containers {
      image = var.image
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }
}
