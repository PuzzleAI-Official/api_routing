from __future__ import annotations

import argparse
import json
import subprocess
import time
from typing import Any

import httpx
from hardening_lib import (
    DEFAULT_ADMIN_TOKEN,
    DEFAULT_BASE_URL,
    SessionLocal,
    create_report_dir,
    seed_fake_document_services,
    seed_hardening_tenant,
    tiny_pdf_bytes,
    write_report,
)
from puzzle_gateway.models import (
    BillingLedgerEntry,
    Job,
    ProviderAttempt,
    RoutingDecision,
    TelemetryEvent,
    TelemetryOutboxEvent,
)
from puzzle_shared import JobStatus
from sqlalchemy import func, select

HTTP_OK = 200
HTTP_UNAVAILABLE = 503
MIN_FALLBACK_ATTEMPTS = 2


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True)  # noqa: S603, S607


def wait_for_gateway(base_url: str, *, timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=5.0)
            if response.status_code == HTTP_OK:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("Gateway did not become healthy")


def post_invoice(
    client: httpx.Client,
    *,
    api_key: str,
    idempotency_key: str,
) -> httpx.Response:
    return client.post(
        "/v1/documents/invoices:extract",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": idempotency_key},
        data={
            "metadata": json.dumps(
                {"provider_set": "documents-default", "line_items_mode": "preferred"}
            )
        },
        files={"file": ("invoice.pdf", tiny_pdf_bytes(), "application/pdf")},
    )


def post_document(
    client: httpx.Client,
    *,
    api_key: str,
    idempotency_key: str,
) -> httpx.Response:
    return client.post(
        "/v1/documents:process",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": idempotency_key},
        data={"metadata": json.dumps({"provider_set": "documents-default"})},
        files={"file": ("document.pdf", tiny_pdf_bytes(), "application/pdf")},
    )


def wait_for_job(
    client: httpx.Client,
    *,
    api_key: str,
    job_id: str,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {api_key}"})
        response.raise_for_status()
        payload = response.json()
        if payload["status"] in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value}:
            return payload
        time.sleep(1)
    raise RuntimeError(f"Job did not finish: {job_id}")


def counts_for_tenant(tenant_id: str) -> dict[str, int]:
    with SessionLocal() as session:
        return {
            "attempts": int(
                session.scalar(
                    select(func.count())
                    .select_from(ProviderAttempt)
                    .where(ProviderAttempt.tenant_id == tenant_id)
                )
                or 0
            ),
            "billing": int(
                session.scalar(
                    select(func.count())
                    .select_from(BillingLedgerEntry)
                    .where(BillingLedgerEntry.tenant_id == tenant_id)
                )
                or 0
            ),
            "pending_outbox": int(
                session.scalar(
                    select(func.count())
                    .select_from(TelemetryOutboxEvent)
                    .where(
                        TelemetryOutboxEvent.tenant_id == tenant_id,
                        TelemetryOutboxEvent.status == "pending",
                    )
                )
                or 0
            ),
            "telemetry": int(
                session.scalar(
                    select(func.count())
                    .select_from(TelemetryEvent)
                    .where(TelemetryEvent.tenant_id == tenant_id)
                )
                or 0
            ),
        }


def redis_down_scenario(base_url: str, admin_token: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        tenant = seed_hardening_tenant(
            client,
            admin_token=admin_token,
            name=f"phase4a-chaos-redis-{time.time_ns()}",
        )
        idem = f"redis-replay-{time.time_ns()}"
        first = post_invoice(client, api_key=tenant.api_key, idempotency_key=idem)
        first.raise_for_status()
        counts_before = counts_for_tenant(tenant.tenant_id)
        compose("stop", "redis")
        try:
            replay = post_invoice(client, api_key=tenant.api_key, idempotency_key=idem)
            new = post_invoice(
                client,
                api_key=tenant.api_key,
                idempotency_key=f"redis-new-{time.time_ns()}",
            )
            counts_after = counts_for_tenant(tenant.tenant_id)
        finally:
            compose("start", "redis")
            time.sleep(3)
    return {
        "passed": (
            replay.status_code == HTTP_OK
            and replay.json().get("replayed") is True
            and new.status_code == HTTP_UNAVAILABLE
            and new.json().get("error", {}).get("code") == "dependency_unavailable"
            and counts_before["attempts"] == counts_after["attempts"]
            and counts_before["billing"] == counts_after["billing"]
        ),
        "replay_status": replay.status_code,
        "new_status": new.status_code,
        "counts_before": counts_before,
        "counts_after": counts_after,
    }


def worker_restart_scenario(base_url: str, admin_token: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        tenant = seed_hardening_tenant(
            client,
            admin_token=admin_token,
            name=f"phase4a-chaos-worker-{time.time_ns()}",
        )
        submit = client.post(
            "/v1/documents/invoices:submit",
            headers={
                "Authorization": f"Bearer {tenant.api_key}",
                "Idempotency-Key": f"worker-{time.time_ns()}",
            },
            data={
                "metadata": json.dumps(
                    {"provider_set": "documents-default", "line_items_mode": "preferred"}
                )
            },
            files={"file": ("invoice.pdf", tiny_pdf_bytes(), "application/pdf")},
        )
        submit.raise_for_status()
        compose("restart", "worker")
        job = wait_for_job(client, api_key=tenant.api_key, job_id=submit.json()["job_id"])
    with SessionLocal() as session:
        job_row = session.get(Job, submit.json()["job_id"])
        attempts = int(
            session.scalar(
                select(func.count())
                .select_from(ProviderAttempt)
                .where(
                    ProviderAttempt.tenant_id == tenant.tenant_id,
                    ProviderAttempt.job_id == submit.json()["job_id"],
                )
            )
            or 0
        )
    return {
        "passed": (
            job["status"] == JobStatus.SUCCEEDED.value
            and attempts == 1
            and job_row is not None
            and job_row.status == JobStatus.SUCCEEDED.value
        ),
        "job_status": job["status"],
        "attempts": attempts,
    }


def telemetry_down_scenario(base_url: str, admin_token: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        tenant = seed_hardening_tenant(
            client,
            admin_token=admin_token,
            name=f"phase4a-chaos-telemetry-{time.time_ns()}",
        )
        compose("stop", "telemetry")
        try:
            response = post_invoice(
                client,
                api_key=tenant.api_key,
                idempotency_key=f"telemetry-{time.time_ns()}",
            )
            response.raise_for_status()
            pending_before = counts_for_tenant(tenant.tenant_id)["pending_outbox"]
        finally:
            compose("start", "telemetry")
        deadline = time.monotonic() + 60
        telemetry_after = 0
        while time.monotonic() < deadline:
            telemetry_after = counts_for_tenant(tenant.tenant_id)["telemetry"]
            if telemetry_after >= 1:
                break
            time.sleep(1)
    return {
        "passed": response.status_code == HTTP_OK and pending_before >= 1 and telemetry_after >= 1,
        "request_status": response.status_code,
        "pending_outbox_before_restart": pending_before,
        "telemetry_after_restart": telemetry_after,
    }


def provider_fallback_scenario(base_url: str, admin_token: str, behavior: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        tenant = seed_hardening_tenant(
            client,
            admin_token=admin_token,
            name=f"phase4a-chaos-provider-{behavior}-{time.time_ns()}",
        )
        seed_fake_document_services(
            tenant.tenant_id,
            primary_behavior=behavior,
            secondary_behavior="success",
            include_invoice=False,
        )
        response = post_document(
            client,
            api_key=tenant.api_key,
            idempotency_key=f"provider-{behavior}-{time.time_ns()}",
        )
        response.raise_for_status()
    with SessionLocal() as session:
        attempts = list(
            session.scalars(
                select(ProviderAttempt)
                .where(ProviderAttempt.tenant_id == tenant.tenant_id)
                .order_by(ProviderAttempt.created_at)
            )
        )
        billing = int(
            session.scalar(
                select(func.count())
                .select_from(BillingLedgerEntry)
                .where(BillingLedgerEntry.tenant_id == tenant.tenant_id)
            )
            or 0
        )
    return {
        "passed": (
            response.status_code == HTTP_OK
            and len(attempts) >= MIN_FALLBACK_ATTEMPTS
            and attempts[0].status == "failed"
            and attempts[-1].status == "succeeded"
            and billing == 1
        ),
        "behavior": behavior,
        "request_status": response.status_code,
        "attempt_statuses": [attempt.status for attempt in attempts],
        "billing_rows": billing,
    }


def circuit_breaker_scenario(base_url: str, admin_token: str) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        tenant = seed_hardening_tenant(
            client,
            admin_token=admin_token,
            name=f"phase4a-chaos-circuit-{time.time_ns()}",
        )
        seed_fake_document_services(
            tenant.tenant_id,
            primary_behavior="transient_failure",
            secondary_behavior="success",
            include_invoice=False,
        )
        for _ in range(4):
            response = post_document(
                client,
                api_key=tenant.api_key,
                idempotency_key=f"circuit-{time.time_ns()}",
            )
            response.raise_for_status()
    with SessionLocal() as session:
        latest = session.scalars(
            select(RoutingDecision)
            .where(RoutingDecision.tenant_id == tenant.tenant_id)
            .order_by(RoutingDecision.created_at.desc())
            .limit(1)
        ).first()
    skipped = latest.decision_json.get("skipped_provider_services", []) if latest else []
    return {
        "passed": any(item.get("reason") == "circuit_open" for item in skipped),
        "skipped_provider_services": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 4A local chaos hardening.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--admin-token", default=DEFAULT_ADMIN_TOKEN)
    args = parser.parse_args()

    wait_for_gateway(args.base_url)
    report_dir = create_report_dir("chaos")
    results = {
        "redis_down": redis_down_scenario(args.base_url, args.admin_token),
        "worker_restart": worker_restart_scenario(args.base_url, args.admin_token),
        "telemetry_down": telemetry_down_scenario(args.base_url, args.admin_token),
        "provider_timeout": provider_fallback_scenario(args.base_url, args.admin_token, "timeout"),
        "provider_malformed": provider_fallback_scenario(
            args.base_url,
            args.admin_token,
            "malformed_response",
        ),
        "circuit_breaker": circuit_breaker_scenario(args.base_url, args.admin_token),
    }
    passed = all(result["passed"] for result in results.values())
    payload = {"passed": passed, **results}
    write_report(report_dir, "chaos", payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
