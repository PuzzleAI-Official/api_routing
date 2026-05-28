#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-puzzle-476822}"
REGION="${REGION:-us-east4}"
VPC_NAME="${VPC_NAME:-puzzle-ai-vpc}"
AR_REPO="${AR_REPO:-puzzle-staging-containers}"
TAG="${TAG:-phase4b-$(date +%Y%m%d%H%M%S)}"
GATEWAY_SERVICE="${GATEWAY_SERVICE:-puzzle-staging-gateway}"
WORKER_POOL="${WORKER_POOL:-puzzle-staging-worker}"
TELEMETRY_POOL="${TELEMETRY_POOL:-puzzle-staging-telemetry}"
MIGRATE_JOB="${MIGRATE_JOB:-puzzle-staging-migrate}"
GATEWAY_SA="${GATEWAY_SA:-puzzle-staging-gateway}"
WORKER_SA="${WORKER_SA:-puzzle-staging-worker}"
TELEMETRY_SA="${TELEMETRY_SA:-puzzle-staging-telemetry}"
MIGRATE_SA="${MIGRATE_SA:-puzzle-staging-migrate}"
REDIS_INSTANCE="${REDIS_INSTANCE:-puzzle-redis-staging}"
DOCUMENTS_BUCKET="${DOCUMENTS_BUCKET:-${PROJECT_ID}-puzzle-staging-documents}"
ARTIFACTS_BUCKET="${ARTIFACTS_BUCKET:-${PROJECT_ID}-puzzle-staging-artifacts}"
DATABASE_URL_SECRET="${DATABASE_URL_SECRET:-puzzle-staging-database-url}"
ADMIN_TOKEN_SECRET="${ADMIN_TOKEN_SECRET:-puzzle-staging-admin-token}"
KMS_RING="${KMS_RING:-puzzle-staging-keyring}"
KMS_KEY="${KMS_KEY:-puzzle-staging-vault}"

gcloud config set project "${PROJECT_ID}" >/dev/null

SUBNET_NAME="${SUBNET_NAME:-$(gcloud compute networks subnets list \
  --filter="network:${VPC_NAME} AND region:${REGION}" \
  --format="value(name)" \
  --limit=1)}"
if [[ -z "${SUBNET_NAME}" ]]; then
  echo "No subnet found for ${VPC_NAME} in ${REGION}. Run gcp_phase4b_create_resources.sh first." >&2
  exit 1
fi

REDIS_HOST="$(gcloud redis instances describe "${REDIS_INSTANCE}" --region="${REGION}" --format="value(host)")"
KMS_KEY_NAME="projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_RING}/cryptoKeys/${KMS_KEY}"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
GATEWAY_IMAGE="${IMAGE_BASE}/gateway:${TAG}"
WORKER_IMAGE="${IMAGE_BASE}/worker:${TAG}"
TELEMETRY_IMAGE="${IMAGE_BASE}/telemetry:${TAG}"

COMMON_ENV="PUZZLE_ENV=staging,PUZZLE_ENABLE_ADMIN_API=false,PUZZLE_DOCUMENT_CAPABILITY_MODE=verified,PUZZLE_RATE_LIMIT_PER_MINUTE=10000,PUZZLE_DB_POOL_SIZE=10,PUZZLE_DB_MAX_OVERFLOW=5,PUZZLE_DB_POOL_TIMEOUT_SECONDS=10,REDIS_URL=redis://${REDIS_HOST}:6379/0,PUZZLE_OBJECT_STORE_BACKEND=gcs,PUZZLE_DOCUMENTS_BUCKET=${DOCUMENTS_BUCKET},PUZZLE_ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET},PUZZLE_KMS_BACKEND=gcp,PUZZLE_KMS_KEY_NAME=${KMS_KEY_NAME}"

echo "== Building images with Cloud Build =="
gcloud builds submit \
  --config=cloudbuild.phase4b.yaml \
  --substitutions="_REGION=${REGION},_REPO=${AR_REPO},_TAG=${TAG}" \
  .

echo "== Deploying migration job =="
gcloud run jobs deploy "${MIGRATE_JOB}" \
  --image="${GATEWAY_IMAGE}" \
  --region="${REGION}" \
  --service-account="${MIGRATE_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --tasks=1 \
  --max-retries=0 \
  --command=python \
  --args=-m,puzzle_gateway.migration_bootstrap \
  --network="${VPC_NAME}" \
  --subnet="${SUBNET_NAME}" \
  --vpc-egress=private-ranges-only \
  --set-env-vars="${COMMON_ENV}" \
  --set-secrets="DATABASE_URL=${DATABASE_URL_SECRET}:latest,PUZZLE_ADMIN_TOKEN=${ADMIN_TOKEN_SECRET}:latest"

gcloud run jobs execute "${MIGRATE_JOB}" --region="${REGION}" --wait

echo "== Deploying gateway service =="
gcloud run deploy "${GATEWAY_SERVICE}" \
  --image="${GATEWAY_IMAGE}" \
  --region="${REGION}" \
  --service-account="${GATEWAY_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --allow-unauthenticated \
  --port=8000 \
  --cpu=2 \
  --memory=2Gi \
  --min-instances=3 \
  --max-instances=10 \
  --concurrency=40 \
  --network="${VPC_NAME}" \
  --subnet="${SUBNET_NAME}" \
  --vpc-egress=private-ranges-only \
  --set-env-vars="${COMMON_ENV}" \
  --set-secrets="DATABASE_URL=${DATABASE_URL_SECRET}:latest,PUZZLE_ADMIN_TOKEN=${ADMIN_TOKEN_SECRET}:latest"

echo "== Deploying worker pool =="
gcloud beta run worker-pools deploy "${WORKER_POOL}" \
  --image="${WORKER_IMAGE}" \
  --region="${REGION}" \
  --service-account="${WORKER_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --cpu=1 \
  --memory=1Gi \
  --scaling=1 \
  --network="${VPC_NAME}" \
  --subnet="${SUBNET_NAME}" \
  --vpc-egress=private-ranges-only \
  --set-env-vars="${COMMON_ENV}" \
  --set-secrets="DATABASE_URL=${DATABASE_URL_SECRET}:latest"

echo "== Deploying telemetry worker pool =="
gcloud beta run worker-pools deploy "${TELEMETRY_POOL}" \
  --image="${TELEMETRY_IMAGE}" \
  --region="${REGION}" \
  --service-account="${TELEMETRY_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --cpu=1 \
  --memory=1Gi \
  --scaling=1 \
  --network="${VPC_NAME}" \
  --subnet="${SUBNET_NAME}" \
  --vpc-egress=private-ranges-only \
  --set-env-vars="PUZZLE_ENV=staging,PUZZLE_ENABLE_ADMIN_API=false" \
  --set-secrets="DATABASE_URL=${DATABASE_URL_SECRET}:latest"

GATEWAY_URL="$(gcloud run services describe "${GATEWAY_SERVICE}" --region="${REGION}" --format='value(status.url)')"
cat <<EOF
Deployment complete.

TAG=${TAG}
GATEWAY_URL=${GATEWAY_URL}

Next:
  Run scripts/gcp_phase4b_seed.sh, then export PUZZLE_API_KEY from its logs.
  Run scripts/gcp_phase4b_smoke.sh.
EOF
