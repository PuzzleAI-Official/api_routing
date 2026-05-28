#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-puzzle-476822}"
REGION="${REGION:-us-east4}"
VPC_NAME="${VPC_NAME:-puzzle-ai-vpc}"

gcloud config set project "${PROJECT_ID}" >/dev/null

echo "== Active account =="
gcloud auth list --filter=status:ACTIVE --format="value(account)"

echo "== Project =="
gcloud projects describe "${PROJECT_ID}" --format="table(projectId,name,lifecycleState)"

echo "== Required APIs =="
gcloud services list --enabled \
  --filter="name:(run.googleapis.com OR cloudbuild.googleapis.com OR artifactregistry.googleapis.com OR sqladmin.googleapis.com OR redis.googleapis.com OR storage.googleapis.com OR cloudkms.googleapis.com OR secretmanager.googleapis.com OR vpcaccess.googleapis.com)" \
  --format="table(config.name)"

echo "== VPC =="
gcloud compute networks describe "${VPC_NAME}" --format="yaml(name,selfLink,autoCreateSubnetworks)" || true

echo "== Subnets in ${REGION} =="
gcloud compute networks subnets list \
  --filter="network:${VPC_NAME} AND region:${REGION}" \
  --format="table(name,region,ipCidrRange,privateIpGoogleAccess)"

echo "== Cloud SQL instances =="
gcloud sql instances list --format="table(name,region,databaseVersion,ipAddresses.ipAddress,settings.ipConfiguration.ipv4Enabled)"

echo "== Redis instances =="
gcloud redis instances list --region="${REGION}" --format="table(name,host,port,region,tier)" || true

echo "== Buckets with puzzle prefix =="
gcloud storage buckets list --filter="name:puzzle" --format="table(name,location,publicAccessPrevention)"

echo "== Artifact Registry repositories =="
gcloud artifacts repositories list --location="${REGION}" --format="table(name,format,location)"

echo "== KMS key rings =="
gcloud kms keyrings list --location="${REGION}" --format="table(name)" || true

echo "== Secret Manager secrets with staging prefix =="
gcloud secrets list --filter="name:puzzle-staging" --format="table(name,createTime)" || true

echo "Discovery complete. This script is read-only."
