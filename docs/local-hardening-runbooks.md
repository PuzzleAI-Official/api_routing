# Phase 4A Local Hardening Runbooks

These runbooks cover the Docker Compose local stack only. They are the rehearsal version of the cloud runbooks we will need in Phase 4B.

## Redis Down

Expected behavior:

- Completed idempotency replay still succeeds from Postgres.
- New billable requests fail closed with `dependency_unavailable`.
- No provider attempt or billing ledger row is created for rejected new work.
- Circuit breaker and rate-limit checks do not silently bypass Redis.

First checks:

- `docker compose ps redis`
- `docker compose logs redis`
- `docker compose restart redis`
- Re-run `python scripts/local_hardening_chaos.py` after Redis is healthy.

## Worker Stuck Or Restarted

Expected behavior:

- Queued jobs can be claimed after the worker restarts.
- Stale `running` jobs are retried after the local stale-lock timeout.
- Terminal `succeeded`, `failed`, or `cancelled` jobs are not executed again.
- Accepted jobs create one billing ledger row.

First checks:

- `docker compose ps worker`
- `docker compose logs worker`
- Inspect `jobs.status`, `jobs.attempt_count`, `jobs.locked_at`, and `jobs.last_error_json`.
- Restart with `docker compose restart worker`.

## Telemetry Lag

Expected behavior:

- Gateway requests do not wait for telemetry ingest.
- `telemetry_outbox_events` rows remain `pending` while telemetry is stopped.
- Pending outbox rows drain into `telemetry_events` after telemetry restarts.

First checks:

- `docker compose ps telemetry`
- `docker compose logs telemetry`
- Inspect pending rows in `telemetry_outbox_events`.
- Restart with `docker compose restart telemetry`.

## Provider Outage

Expected behavior:

- Retryable provider failures record failed attempts.
- Routing walks the fallback list.
- Only the accepted final result is billed.
- Repeated failures open the circuit breaker and future routing skips the bad provider.

First checks:

- Inspect `provider_attempts.status` and `provider_attempts.error_code`.
- Inspect the latest `routing_decisions.decision_json`.
- Confirm skipped providers include `circuit_open` after repeated failures.

## Deletion Request

Expected behavior:

- Document bytes and raw provider responses are deleted from object storage.
- Stored document, workflow, job, idempotency, and telemetry payloads are redacted.
- Billing, provider attempt, routing, and audit metadata remain available.
- Cross-tenant deletion attempts return not found.

First checks:

- Run `python scripts/local_deletion_smoke.py`.
- Inspect `data_deletion_requests.status` and `summary_json`.
- Confirm `audit_log_entries` contains `data_deletion.completed`.

## Local Restore

Expected behavior:

- Postgres restores into a clean local restore database.
- Durable tables have matching or greater row counts after seed data is added.
- Vault credentials decrypt with the same local KMS master key.
- Document object refs exist in the copied object-store volume.

First checks:

- Run `python scripts/local_dr_check.py`.
- Confirm Docker can run `pg_dump`, `createdb`, and `psql` inside the Postgres service.
- Confirm the `puzzle-object-store` Docker volume exists.
