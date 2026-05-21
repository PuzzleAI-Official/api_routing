# Puzzle SDK & Gateway

Greenfield Phase 0 + Phase 1 implementation for the Puzzle SDK and routing gateway.

The repository is a Python 3.12 monorepo with:

- `packages/puzzle`: public SDK.
- `packages/shared`: shared schemas and error contracts.
- `services/gateway`: FastAPI API + routing core.
- `services/worker`: async job worker.
- `services/telemetry`: telemetry/eval ingest service.
- `infra/terraform`: GCP staging baseline.
- `tests`: core and integration-style tests.

Local development is designed for `uv` and Docker Compose. The base Python environment can still run tests with `python -m pytest` after dependencies are installed.

## Local hardening flow

1. Start Docker Desktop.
2. Run `docker compose up --build`.
3. In another terminal, run `python scripts/local_smoke.py`.

The smoke script creates a tenant, creates an API key, creates the default mock `ProviderSet`, configures two mock providers, and sends a sync mock request through the gateway.

Admin APIs are enabled only for local/test environments by default. In non-local environments they are disabled unless `PUZZLE_ENABLE_ADMIN_API=true` is explicitly set, and they always require `X-Admin-Token`.
