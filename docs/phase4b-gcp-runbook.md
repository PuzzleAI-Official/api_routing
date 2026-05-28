# Phase 4B GCP Runbook

This runbook deploys an isolated Puzzle staging stack in project `puzzle-476822`, region `us-east4`.

It intentionally does not use `puzzle-sql-prod`.

## 1. Push The Source

Push the current branch to a private GitHub repo, then clone it in Cloud Shell.

```bash
git clone <private-repo-url>
cd <repo>
git checkout <branch>
```

## 2. Discover Existing Infra

```bash
bash scripts/gcp_phase4b_discover.sh
```

Check that `puzzle-ai-vpc` exists and has a subnet in `us-east4`.

## 3. Create Isolated Staging Resources

```bash
bash scripts/gcp_phase4b_create_resources.sh
```

This creates:

- `puzzle-staging-containers`
- `puzzle-sql-staging`
- `puzzle-redis-staging`
- `puzzle-476822-puzzle-staging-documents`
- `puzzle-476822-puzzle-staging-artifacts`
- `puzzle-staging-keyring` / `puzzle-staging-vault`
- staging secrets
- runtime service accounts and IAM bindings

Staging Cloud Run should set bounded DB pool env vars so Cloud SQL connection usage is predictable.
The currently verified Phase 4B staging shape is:

- gateway: `PUZZLE_DB_POOL_SIZE=10`, `PUZZLE_DB_MAX_OVERFLOW=5`
- worker: `PUZZLE_DB_POOL_SIZE=3`, `PUZZLE_DB_MAX_OVERFLOW=3`
- telemetry: `PUZZLE_DB_POOL_SIZE=2`, `PUZZLE_DB_MAX_OVERFLOW=2`
- `PUZZLE_DB_POOL_TIMEOUT_SECONDS=10`
- `PUZZLE_RATE_LIMIT_PER_MINUTE=10000` for fake-provider hardening load tests

## 4. Build And Deploy

```bash
bash scripts/gcp_phase4b_build_deploy.sh
```

Save the printed `TAG` and `GATEWAY_URL`.

## 5. Seed A Staging Tenant

```bash
export TAG=<tag-from-build-deploy>
bash scripts/gcp_phase4b_seed.sh
```

Copy the `api_key` printed in the job logs.

```bash
export PUZZLE_API_KEY=<api-key-from-seed-log>
```

## 6. Run Smoke Tests

```bash
export GATEWAY_URL=<gateway-url-from-build-deploy>
bash scripts/gcp_phase4b_smoke.sh
```

The smoke script checks health, core mock sync/async, documents sync/async, invoices sync/async, worker completion, and idempotency replay.
For Cloud Run, use `/health` and `/ready` as the canonical probes. Cloud Run reserves some paths ending in `z`, so `/healthz` remains local/backward-compatible but should not be used as a cloud production probe.

## 7. Private Admin Checks

Keep the public gateway deployed with `PUZZLE_ENABLE_ADMIN_API=false`.
For staging-only admin checks, deploy a separate IAM-protected admin service with `PUZZLE_ENABLE_ADMIN_API=true`, no unauthenticated access, and the same private VPC/database/storage settings.

Use that private admin service for:

- provider service visibility;
- workflow provider visibility;
- deletion requests;
- cross-tenant deletion checks.

## 8. Manual Staging Drills

Use fake providers only for these drills.

- Redis outage: temporarily deploy a gateway revision with a bad `REDIS_URL`; completed idempotency replay should work, new billable requests should fail closed.
- Worker restart: redeploy `puzzle-staging-worker` while async jobs are queued; jobs should complete once.
- Telemetry outage: scale `puzzle-staging-telemetry` to zero, send requests, then scale it back to one; telemetry outbox should drain.
- Provider fallback: change fake provider behavior through a DB/admin job and verify fallback succeeds.
- Circuit breaker: force repeated fake provider failures and verify later routes skip it.
- Restore: restore the latest Cloud SQL backup to a temporary clone, verify durable rows and vault decryptability from a Cloud Run job, then delete the clone and temporary secret.

Cloud SQL restore drill safety notes:

- The operator running the drill must have permission to create and delete temporary Cloud SQL instances. In practice, verify `cloudsql.instances.delete` before creating the clone, otherwise the restore can leave behind a billable instance.
- `gcloud sql backups restore <backup-id>` does not support override flags such as `--no-backup` or `--no-assign-ip`. Those overrides are only supported for backup-name based restore to a new instance. For backup-id restore, omit those flags or use a backup-name restore flow.
- The temporary restore instance must use the staging VPC/private connectivity and must never point at `puzzle-sql-prod`.
- The restore verification job should connect to the temporary restore database, check key source-of-truth row counts, and run a KMS wrap/unwrap probe. If active provider credentials exist, it should decrypt one restored credential and roll back the audit write.
- Delete the restore instance, temporary Cloud Run job, and temporary Secret Manager secret after the report is captured.

## 9. Phase 4B Completion Checklist

- Gateway is reachable on Cloud Run.
- Worker and telemetry run as Cloud Run worker pools.
- Cloud SQL is private and has backups/PITR.
- Redis is used by gateway and worker.
- GCS stores document and raw provider objects.
- KMS wraps provider credential data keys.
- Secret Manager stores sensitive runtime configuration.
- Smoke tests pass.
- Failure drills pass.
- No secrets, raw provider payloads, raw document bytes, or full document text appear in logs.
