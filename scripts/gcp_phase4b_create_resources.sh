#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-puzzle-476822}"
REGION="${REGION:-us-east4}"
VPC_NAME="${VPC_NAME:-puzzle-ai-vpc}"
AR_REPO="${AR_REPO:-puzzle-staging-containers}"
SQL_INSTANCE="${SQL_INSTANCE:-puzzle-sql-staging}"
DB_NAME="${DB_NAME:-puzzle}"
DB_USER="${DB_USER:-puzzle_app}"
REDIS_INSTANCE="${REDIS_INSTANCE:-puzzle-redis-staging}"
DOCUMENTS_BUCKET="${DOCUMENTS_BUCKET:-${PROJECT_ID}-puzzle-staging-documents}"
ARTIFACTS_BUCKET="${ARTIFACTS_BUCKET:-${PROJECT_ID}-puzzle-staging-artifacts}"
KMS_RING="${KMS_RING:-puzzle-staging-keyring}"
KMS_KEY="${KMS_KEY:-puzzle-staging-vault}"
DB_PASSWORD_SECRET="${DB_PASSWORD_SECRET:-puzzle-staging-db-password}"
DATABASE_URL_SECRET="${DATABASE_URL_SECRET:-puzzle-staging-database-url}"
ADMIN_TOKEN_SECRET="${ADMIN_TOKEN_SECRET:-puzzle-staging-admin-token}"
GATEWAY_SA="${GATEWAY_SA:-puzzle-staging-gateway}"
WORKER_SA="${WORKER_SA:-puzzle-staging-worker}"
TELEMETRY_SA="${TELEMETRY_SA:-puzzle-staging-telemetry}"
MIGRATE_SA="${MIGRATE_SA:-puzzle-staging-migrate}"

gcloud config set project "${PROJECT_ID}" >/dev/null

ensure_secret_value() {
  local secret_name="$1"
  local value="$2"
  if gcloud secrets describe "${secret_name}" >/dev/null 2>&1; then
    printf "%s" "${value}" | gcloud secrets versions add "${secret_name}" --data-file=- >/dev/null
  else
    printf "%s" "${value}" | gcloud secrets create "${secret_name}" --replication-policy=automatic --data-file=- >/dev/null
  fi
}

ensure_service_account() {
  local account_id="$1"
  local display_name="$2"
  if ! gcloud iam service-accounts describe "${account_id}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${account_id}" --display-name="${display_name}" >/dev/null
  fi
}

grant_project_role() {
  local account_id="$1"
  local role="$2"
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${account_id}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="${role}" \
    --quiet >/dev/null
}

grant_secret_accessor() {
  local account_id="$1"
  local secret_name="$2"
  gcloud secrets add-iam-policy-binding "${secret_name}" \
    --member="serviceAccount:${account_id}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null
}

grant_bucket_role() {
  local account_id="$1"
  local bucket_name="$2"
  gcloud storage buckets add-iam-policy-binding "gs://${bucket_name}" \
    --member="serviceAccount:${account_id}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/storage.objectUser" \
    --quiet >/dev/null
}

echo "== Enabling APIs =="
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  sqladmin.googleapis.com \
  redis.googleapis.com \
  storage.googleapis.com \
  cloudkms.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  vpcaccess.googleapis.com >/dev/null

SUBNET_NAME="${SUBNET_NAME:-$(gcloud compute networks subnets list \
  --filter="network:${VPC_NAME} AND region:${REGION}" \
  --format="value(name)" \
  --limit=1)}"
if [[ -z "${SUBNET_NAME}" ]]; then
  echo "No subnet found for ${VPC_NAME} in ${REGION}. Create one first, then rerun." >&2
  exit 1
fi

echo "== Artifact Registry =="
if ! gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Puzzle staging containers" >/dev/null
fi

echo "== Buckets =="
for bucket in "${DOCUMENTS_BUCKET}" "${ARTIFACTS_BUCKET}"; do
  if ! gcloud storage buckets describe "gs://${bucket}" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${bucket}" \
      --location="${REGION}" \
      --uniform-bucket-level-access \
      --public-access-prevention >/dev/null
  fi
  gcloud storage buckets update "gs://${bucket}" \
    --uniform-bucket-level-access \
    --public-access-prevention >/dev/null
done

echo "== KMS =="
gcloud kms keyrings describe "${KMS_RING}" --location="${REGION}" >/dev/null 2>&1 \
  || gcloud kms keyrings create "${KMS_RING}" --location="${REGION}" >/dev/null
gcloud kms keys describe "${KMS_KEY}" --keyring="${KMS_RING}" --location="${REGION}" >/dev/null 2>&1 \
  || gcloud kms keys create "${KMS_KEY}" --keyring="${KMS_RING}" --location="${REGION}" --purpose=encryption --rotation-period=30d >/dev/null

echo "== Secrets =="
if gcloud secrets describe "${DB_PASSWORD_SECRET}" >/dev/null 2>&1; then
  DB_PASSWORD="$(gcloud secrets versions access latest --secret="${DB_PASSWORD_SECRET}")"
else
  DB_PASSWORD="$(openssl rand -base64 32 | tr -d '\n')"
  ensure_secret_value "${DB_PASSWORD_SECRET}" "${DB_PASSWORD}"
fi
if ! gcloud secrets describe "${ADMIN_TOKEN_SECRET}" >/dev/null 2>&1; then
  ensure_secret_value "${ADMIN_TOKEN_SECRET}" "$(openssl rand -base64 32 | tr -d '\n')"
fi

echo "== Cloud SQL =="
VPC_SELF_LINK="projects/${PROJECT_ID}/global/networks/${VPC_NAME}"
if ! gcloud sql instances describe "${SQL_INSTANCE}" >/dev/null 2>&1; then
  gcloud sql instances create "${SQL_INSTANCE}" \
    --database-version=POSTGRES_16 \
    --region="${REGION}" \
    --tier=db-g1-small \
    --availability-type=zonal \
    --network="${VPC_SELF_LINK}" \
    --no-assign-ip \
    --backup-start-time=04:00 \
    --enable-point-in-time-recovery \
    --storage-auto-increase >/dev/null
fi
gcloud sql databases describe "${DB_NAME}" --instance="${SQL_INSTANCE}" >/dev/null 2>&1 \
  || gcloud sql databases create "${DB_NAME}" --instance="${SQL_INSTANCE}" >/dev/null
if gcloud sql users list --instance="${SQL_INSTANCE}" --format="value(name)" | grep -qx "${DB_USER}"; then
  gcloud sql users set-password "${DB_USER}" --instance="${SQL_INSTANCE}" --password="${DB_PASSWORD}" >/dev/null
else
  gcloud sql users create "${DB_USER}" --instance="${SQL_INSTANCE}" --password="${DB_PASSWORD}" >/dev/null
fi
SQL_PRIVATE_IP="$(gcloud sql instances describe "${SQL_INSTANCE}" --format="value(ipAddresses[0].ipAddress)")"
DATABASE_URL="postgresql+psycopg://${DB_USER}:${DB_PASSWORD}@${SQL_PRIVATE_IP}:5432/${DB_NAME}"
ensure_secret_value "${DATABASE_URL_SECRET}" "${DATABASE_URL}"

echo "== Redis =="
if ! gcloud redis instances describe "${REDIS_INSTANCE}" --region="${REGION}" >/dev/null 2>&1; then
  gcloud redis instances create "${REDIS_INSTANCE}" \
    --region="${REGION}" \
    --size=1 \
    --tier=basic \
    --redis-version=redis_7_0 \
    --network="${VPC_NAME}" >/dev/null
fi

echo "== Service accounts =="
ensure_service_account "${GATEWAY_SA}" "Puzzle staging gateway"
ensure_service_account "${WORKER_SA}" "Puzzle staging worker"
ensure_service_account "${TELEMETRY_SA}" "Puzzle staging telemetry"
ensure_service_account "${MIGRATE_SA}" "Puzzle staging migrations"

for account in "${GATEWAY_SA}" "${WORKER_SA}" "${TELEMETRY_SA}" "${MIGRATE_SA}"; do
  grant_project_role "${account}" "roles/logging.logWriter"
  grant_project_role "${account}" "roles/monitoring.metricWriter"
done
for account in "${GATEWAY_SA}" "${WORKER_SA}" "${MIGRATE_SA}"; do
  grant_project_role "${account}" "roles/cloudsql.client"
  grant_secret_accessor "${account}" "${DATABASE_URL_SECRET}"
  grant_secret_accessor "${account}" "${ADMIN_TOKEN_SECRET}"
done
for account in "${GATEWAY_SA}" "${WORKER_SA}"; do
  grant_bucket_role "${account}" "${DOCUMENTS_BUCKET}"
  grant_bucket_role "${account}" "${ARTIFACTS_BUCKET}"
  gcloud kms keys add-iam-policy-binding "${KMS_KEY}" \
    --keyring="${KMS_RING}" \
    --location="${REGION}" \
    --member="serviceAccount:${account}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/cloudkms.cryptoKeyEncrypterDecrypter" \
    --quiet >/dev/null
done

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer" \
  --quiet >/dev/null

REDIS_HOST="$(gcloud redis instances describe "${REDIS_INSTANCE}" --region="${REGION}" --format="value(host)")"
KMS_KEY_NAME="projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_RING}/cryptoKeys/${KMS_KEY}"

cat <<EOF
Phase 4B staging resources are ready.

SUBNET_NAME=${SUBNET_NAME}
SQL_PRIVATE_IP=${SQL_PRIVATE_IP}
REDIS_URL=redis://${REDIS_HOST}:6379/0
DOCUMENTS_BUCKET=${DOCUMENTS_BUCKET}
ARTIFACTS_BUCKET=${ARTIFACTS_BUCKET}
KMS_KEY_NAME=${KMS_KEY_NAME}
DATABASE_URL_SECRET=${DATABASE_URL_SECRET}
ADMIN_TOKEN_SECRET=${ADMIN_TOKEN_SECRET}
EOF
