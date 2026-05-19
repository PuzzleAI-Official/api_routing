provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  name_prefix = "puzzle-${var.environment}"
  labels = {
    app         = "puzzle"
    environment = var.environment
  }
}

resource "google_artifact_registry_repository" "containers" {
  location      = var.region
  repository_id = "${local.name_prefix}-containers"
  description   = "Puzzle container images"
  format        = "DOCKER"
  labels        = local.labels
}

resource "google_service_account" "gateway" {
  account_id   = "${local.name_prefix}-gateway"
  display_name = "Puzzle Gateway"
}

resource "google_service_account" "worker" {
  account_id   = "${local.name_prefix}-worker"
  display_name = "Puzzle Worker"
}

resource "google_service_account" "telemetry" {
  account_id   = "${local.name_prefix}-telemetry"
  display_name = "Puzzle Telemetry"
}

resource "google_sql_database_instance" "postgres" {
  name             = "${local.name_prefix}-postgres"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    tier              = var.database_tier
    availability_type = "ZONAL"
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
    ip_configuration {
      ipv4_enabled = true
    }
  }
}

resource "google_sql_database" "puzzle" {
  name     = "puzzle"
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "puzzle" {
  name     = "puzzle"
  instance = google_sql_database_instance.postgres.name
  password = random_password.db_password.result
}

resource "random_password" "db_password" {
  length  = 32
  special = true
}

resource "google_redis_instance" "redis" {
  name           = "${local.name_prefix}-redis"
  tier           = "BASIC"
  memory_size_gb = 1
  region         = var.region
  labels         = local.labels
}

resource "google_pubsub_topic" "jobs" {
  name   = "${local.name_prefix}-jobs"
  labels = local.labels
}

resource "google_pubsub_subscription" "jobs" {
  name  = "${local.name_prefix}-jobs-worker"
  topic = google_pubsub_topic.jobs.name
  ack_deadline_seconds = 60
}

resource "google_pubsub_topic" "telemetry" {
  name   = "${local.name_prefix}-telemetry"
  labels = local.labels
}

resource "google_pubsub_subscription" "telemetry" {
  name  = "${local.name_prefix}-telemetry-ingest"
  topic = google_pubsub_topic.telemetry.name
  ack_deadline_seconds = 30
}

resource "google_storage_bucket" "documents" {
  name                        = "${var.project_id}-${local.name_prefix}-documents"
  location                    = var.region
  uniform_bucket_level_access = true
  labels                      = local.labels
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-${local.name_prefix}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  labels                      = local.labels
}

resource "google_kms_key_ring" "puzzle" {
  name     = "${local.name_prefix}-keyring"
  location = var.region
}

resource "google_kms_crypto_key" "vault" {
  name            = "${local.name_prefix}-vault"
  key_ring        = google_kms_key_ring.puzzle.id
  rotation_period = "2592000s"
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "${local.name_prefix}-db-password"
  labels    = local.labels
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db_password.result
}

module "gateway_service" {
  source                = "./modules/cloud_run_service"
  name                  = "${local.name_prefix}-gateway"
  region                = var.region
  image                 = var.container_image
  service_account_email = google_service_account.gateway.email
  labels                = local.labels
}

module "worker_service" {
  source                = "./modules/cloud_run_service"
  name                  = "${local.name_prefix}-worker"
  region                = var.region
  image                 = var.container_image
  service_account_email = google_service_account.worker.email
  labels                = local.labels
}

module "telemetry_service" {
  source                = "./modules/cloud_run_service"
  name                  = "${local.name_prefix}-telemetry"
  region                = var.region
  image                 = var.container_image
  service_account_email = google_service_account.telemetry.email
  labels                = local.labels
}

resource "google_project_iam_member" "gateway_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "worker_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "gateway_kms" {
  project = var.project_id
  role    = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "worker_pubsub" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_project_iam_member" "telemetry_pubsub" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${google_service_account.telemetry.email}"
}
