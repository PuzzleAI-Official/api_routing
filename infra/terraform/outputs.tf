output "artifact_registry_repository" {
  value = google_artifact_registry_repository.containers.name
}

output "cloud_sql_instance" {
  value = google_sql_database_instance.postgres.connection_name
}

output "documents_bucket" {
  value = google_storage_bucket.documents.name
}

output "artifacts_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "gateway_url" {
  value = module.gateway_service.url
}
