variable "project_id" {
  type        = string
  description = "GCP project ID."
}

variable "region" {
  type        = string
  description = "Primary GCP region."
  default     = "us-central1"
}

variable "environment" {
  type        = string
  description = "Environment name."
  default     = "staging"
}

variable "database_tier" {
  type        = string
  description = "Cloud SQL instance tier."
  default     = "db-f1-micro"
}

variable "container_image" {
  type        = string
  description = "Initial container image for all Cloud Run services."
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}
