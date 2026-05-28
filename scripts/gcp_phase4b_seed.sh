#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-puzzle-476822}"
REGION="${REGION:-us-east4}"
VPC_NAME="${VPC_NAME:-puzzle-ai-vpc}"
AR_REPO="${AR_REPO:-puzzle-staging-containers}"
TAG="${TAG:?Set TAG to the image tag printed by gcp_phase4b_build_deploy.sh}"
SEED_JOB="${SEED_JOB:-puzzle-staging-seed}"
MIGRATE_SA="${MIGRATE_SA:-puzzle-staging-migrate}"
DATABASE_URL_SECRET="${DATABASE_URL_SECRET:-puzzle-staging-database-url}"
ADMIN_TOKEN_SECRET="${ADMIN_TOKEN_SECRET:-puzzle-staging-admin-token}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/gateway:${TAG}"

gcloud config set project "${PROJECT_ID}" >/dev/null

SUBNET_NAME="${SUBNET_NAME:-$(gcloud compute networks subnets list \
  --filter="network:${VPC_NAME} AND region:${REGION}" \
  --format="value(name)" \
  --limit=1)}"
if [[ -z "${SUBNET_NAME}" ]]; then
  echo "No subnet found for ${VPC_NAME} in ${REGION}." >&2
  exit 1
fi

gcloud run jobs deploy "${SEED_JOB}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${MIGRATE_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --tasks=1 \
  --max-retries=0 \
  --command=puzzle-gateway \
  --args=seed-phase4b-staging,--region,${REGION} \
  --network="${VPC_NAME}" \
  --subnet="${SUBNET_NAME}" \
  --vpc-egress=private-ranges-only \
  --set-env-vars="PUZZLE_ENV=staging,PUZZLE_ENABLE_ADMIN_API=false,PUZZLE_DOCUMENT_CAPABILITY_MODE=verified" \
  --set-secrets="DATABASE_URL=${DATABASE_URL_SECRET}:latest,PUZZLE_ADMIN_TOKEN=${ADMIN_TOKEN_SECRET}:latest"

gcloud run jobs execute "${SEED_JOB}" --region="${REGION}" --wait

echo "Seed job finished. Copy the api_key from the log output below and export it as PUZZLE_API_KEY."
gcloud logging read \
  "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${SEED_JOB}\"" \
  --limit=20 \
  --format="value(textPayload)"
