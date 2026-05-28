# Phase 4B GCP Validation Report

Date: 2026-05-22

Project: `puzzle-476822`
Region: `us-east4`
Gateway revision: `puzzle-staging-gateway-00012-7mj`
Image tag: `phase4b-drills-20260521203048`

## Result

Phase 4B cloud runtime validation is complete enough to move to Phase 5 development.

Deferred before public production launch:

- Cloud SQL restore drill.
- Worker restart/redelivery drill.

These are still important production operations checks, but they are not blocking Phase 5 product development.

## Root-Cause Fix Completed

The provider fallback drill exposed a real observability gap:

- Invoice execution recorded circuit-breaker failures under `documents.invoice.extract`.
- Invoice routing initially inherited document routing checks under `documents.process`.
- As a result, execution skipped the open circuit correctly, but the routing decision did not explain the skip as `circuit_open`.

Fix:

- Invoice routing now checks the invoice-specific circuit breaker before workflow scoring.
- Routing decisions now record `circuit_open` for skipped invoice provider services.
- A regression test covers the invoice circuit-open skip.

## Verified Drills

### Telemetry Outage

Telemetry worker pool was scaled from 1 to 0.

Five gateway requests were sent while telemetry was down.

Observed:

- Gateway requests succeeded: `5 / 5`
- Pending telemetry outbox rows during outage: `5`
- Telemetry events during outage: `0`

Telemetry worker pool was restored to 1.

Observed after restore:

- Pending telemetry outbox rows: `0`
- Processed outbox rows: `5`
- Telemetry events written: `5`

Result: pass.

### Provider Failure And Fallback

A fresh staging tenant was seeded.

The primary fake invoice provider was changed to `transient_failure`.

Observed:

- First invoice request attempted primary, primary failed, secondary succeeded.
- Replay returned the stored secondary result.
- Billing rows for the first request: `1`
- After repeated primary failures, the circuit breaker opened.
- Fourth invoice request skipped primary and used secondary directly.
- Fourth routing decision included `fake-doc-invoice-primary:circuit_open`.
- Billing rows for the fourth request: `1`

Result: pass.

## Final Smoke

Final post-drill smoke on the fixed revision passed:

- `GET /health`
- `GET /ready`
- `GET /version`
- Core mock sync with idempotency replay.
- Core mock async job completion.
- Documents sync.
- Documents async job completion.
- Invoice sync.
- Invoice async job completion.

## Test And Hygiene Checks

Local tests:

- `88 passed`
- `6 skipped`
- Coverage: `85.10%`

GCP log/resource hygiene:

- Final gateway severity error logs: `0`
- Final gateway HTTP 5xx logs: `0`
- Temporary drill services left behind: `0`
- Temporary drill jobs left behind: `0`

## Current Status

Phase 4B is ready for Phase 5 development work.

Before public production launch, run the deferred Cloud SQL restore and worker restart/redelivery drills with the required GCP permissions.
