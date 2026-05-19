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
