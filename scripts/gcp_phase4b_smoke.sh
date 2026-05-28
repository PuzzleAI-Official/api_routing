#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-puzzle-476822}"
REGION="${REGION:-us-east4}"
GATEWAY_SERVICE="${GATEWAY_SERVICE:-puzzle-staging-gateway}"
GATEWAY_URL="${GATEWAY_URL:-$(gcloud run services describe "${GATEWAY_SERVICE}" --region="${REGION}" --format='value(status.url)' 2>/dev/null || true)}"
PUZZLE_API_KEY="${PUZZLE_API_KEY:?Export PUZZLE_API_KEY from gcp_phase4b_seed.sh output first}"

if [[ -z "${GATEWAY_URL}" ]]; then
  echo "GATEWAY_URL is not set and could not be discovered." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
SAMPLE_PDF="${TMP_DIR}/sample.pdf"
printf '%s\n' '%PDF-1.4' '1 0 obj<</Type/Catalog>>endobj' '%%EOF' > "${SAMPLE_PDF}"

json_field() {
  local field="$1"
  python3 -c "import json,sys; print(json.load(sys.stdin)['${field}'])"
}

wait_for_job() {
  local job_id="$1"
  local status=""
  for _ in $(seq 1 60); do
    response="$(curl -sS -f \
      -H "Authorization: Bearer ${PUZZLE_API_KEY}" \
      "${GATEWAY_URL}/v1/jobs/${job_id}")"
    status="$(printf "%s" "${response}" | json_field status)"
    if [[ "${status}" == "succeeded" ]]; then
      return 0
    fi
    if [[ "${status}" == "failed" || "${status}" == "cancelled" ]]; then
      echo "Job ${job_id} ended with status ${status}" >&2
      echo "${response}" >&2
      return 1
    fi
    sleep 2
  done
  echo "Timed out waiting for job ${job_id}" >&2
  return 1
}

echo "== Health =="
curl -sS -f "${GATEWAY_URL}/health" >/dev/null
curl -sS -f "${GATEWAY_URL}/ready" >/dev/null
curl -sS -f "${GATEWAY_URL}/version" >/dev/null

echo "== Core mock sync =="
CORE_KEY="phase4b-core-sync-$(date +%s)"
CORE_RESPONSE="$(curl -sS -f -X POST "${GATEWAY_URL}/v1/core/mock:run" \
  -H "Authorization: Bearer ${PUZZLE_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: ${CORE_KEY}" \
  -d '{"payload":{"smoke":"core-sync"}}')"
CORE_REPLAY="$(curl -sS -f -X POST "${GATEWAY_URL}/v1/core/mock:run" \
  -H "Authorization: Bearer ${PUZZLE_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: ${CORE_KEY}" \
  -d '{"payload":{"smoke":"core-sync"}}')"
[[ "$(printf "%s" "${CORE_REPLAY}" | json_field replayed)" == "True" ]]

echo "== Core mock async =="
CORE_ASYNC="$(curl -sS -f -X POST "${GATEWAY_URL}/v1/core/mock:submit" \
  -H "Authorization: Bearer ${PUZZLE_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: phase4b-core-async-$(date +%s)" \
  -d '{"payload":{"smoke":"core-async"}}')"
wait_for_job "$(printf "%s" "${CORE_ASYNC}" | json_field job_id)"

echo "== Documents sync =="
curl -sS -f -X POST "${GATEWAY_URL}/v1/documents:process" \
  -H "Authorization: Bearer ${PUZZLE_API_KEY}" \
  -H "Idempotency-Key: phase4b-doc-sync-$(date +%s)" \
  -F "file=@${SAMPLE_PDF};type=application/pdf" \
  -F 'metadata={}' >/dev/null

echo "== Documents async =="
DOC_ASYNC="$(curl -sS -f -X POST "${GATEWAY_URL}/v1/documents:submit" \
  -H "Authorization: Bearer ${PUZZLE_API_KEY}" \
  -H "Idempotency-Key: phase4b-doc-async-$(date +%s)" \
  -F "file=@${SAMPLE_PDF};type=application/pdf" \
  -F 'metadata={}')"
wait_for_job "$(printf "%s" "${DOC_ASYNC}" | json_field job_id)"

echo "== Invoice sync =="
curl -sS -f -X POST "${GATEWAY_URL}/v1/documents/invoices:extract" \
  -H "Authorization: Bearer ${PUZZLE_API_KEY}" \
  -H "Idempotency-Key: phase4b-invoice-sync-$(date +%s)" \
  -F "file=@${SAMPLE_PDF};type=application/pdf" >/dev/null

echo "== Invoice async =="
INVOICE_ASYNC="$(curl -sS -f -X POST "${GATEWAY_URL}/v1/documents/invoices:submit" \
  -H "Authorization: Bearer ${PUZZLE_API_KEY}" \
  -H "Idempotency-Key: phase4b-invoice-async-$(date +%s)" \
  -F "file=@${SAMPLE_PDF};type=application/pdf")"
wait_for_job "$(printf "%s" "${INVOICE_ASYNC}" | json_field job_id)"

echo "Phase 4B smoke passed against ${GATEWAY_URL}."
