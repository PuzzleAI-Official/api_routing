from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from typing import Any

import httpx
from hardening_lib import (
    DEFAULT_ADMIN_TOKEN,
    DEFAULT_BASE_URL,
    SessionLocal,
    create_report_dir,
    seed_hardening_tenant,
    tiny_pdf_bytes,
    write_report,
)
from puzzle_gateway.models import BillingLedgerEntry, Job, ProviderAttempt, RoutingDecision
from puzzle_shared import JobStatus
from sqlalchemy import func, select

PERCENT = 100.0
DEFAULT_ERROR_RATE_LIMIT = 0.01
DEFAULT_ROUTING_P95_LIMIT_MS = 50.0
HTTP_SUCCESS_MIN = 200
HTTP_ERROR_MIN = 400
ERROR_PREVIEW_CHARS = 200


@dataclass
class Sample:
    scenario: str
    status_code: int
    latency_ms: float
    ok: bool
    error: str | None = None


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile_value)))
    return round(ordered[index], 3)


async def _request(client: httpx.AsyncClient, scenario: str, tenant_index: int) -> Sample:
    started_at = time.perf_counter()
    try:
        tenant = client.headers_by_tenant[tenant_index % len(client.headers_by_tenant)]  # type: ignore[attr-defined]
        idem = f"load-{scenario}-{tenant_index}-{time.time_ns()}"
        if scenario == "healthz":
            response = await client.get("/healthz")
        elif scenario == "core_run":
            response = await client.post(
                "/v1/core/mock:run",
                headers={**tenant, "Idempotency-Key": idem},
                json={"payload": {"scenario": scenario}, "strategy": "balanced"},
            )
        elif scenario == "core_submit":
            response = await client.post(
                "/v1/core/mock:submit",
                headers={**tenant, "Idempotency-Key": idem},
                json={"payload": {"scenario": scenario}, "strategy": "balanced"},
            )
        elif scenario == "documents_process":
            response = await client.post(
                "/v1/documents:process",
                headers={**tenant, "Idempotency-Key": idem},
                data={"metadata": json.dumps({"provider_set": "documents-default"})},
                files={"file": ("sample.pdf", tiny_pdf_bytes(), "application/pdf")},
            )
        elif scenario == "documents_submit":
            response = await client.post(
                "/v1/documents:submit",
                headers={**tenant, "Idempotency-Key": idem},
                data={"metadata": json.dumps({"provider_set": "documents-default"})},
                files={"file": ("sample.pdf", tiny_pdf_bytes(), "application/pdf")},
            )
        elif scenario == "invoices_extract":
            response = await client.post(
                "/v1/documents/invoices:extract",
                headers={**tenant, "Idempotency-Key": idem},
                data={
                    "metadata": json.dumps(
                        {"provider_set": "documents-default", "line_items_mode": "preferred"}
                    )
                },
                files={"file": ("invoice.pdf", tiny_pdf_bytes(), "application/pdf")},
            )
        elif scenario == "invoices_submit":
            response = await client.post(
                "/v1/documents/invoices:submit",
                headers={**tenant, "Idempotency-Key": idem},
                data={
                    "metadata": json.dumps(
                        {"provider_set": "documents-default", "line_items_mode": "preferred"}
                    )
                },
                files={"file": ("invoice.pdf", tiny_pdf_bytes(), "application/pdf")},
            )
        else:
            raise ValueError(f"Unknown load scenario: {scenario}")
        latency_ms = (time.perf_counter() - started_at) * 1000
        return Sample(
            scenario=scenario,
            status_code=response.status_code,
            latency_ms=round(latency_ms, 3),
            ok=HTTP_SUCCESS_MIN <= response.status_code < HTTP_ERROR_MIN,
            error=(
                None
                if response.status_code < HTTP_ERROR_MIN
                else response.text[:ERROR_PREVIEW_CHARS]
            ),
        )
    except Exception as exc:  # noqa: BLE001 - load script records request failures
        latency_ms = (time.perf_counter() - started_at) * 1000
        return Sample(
            scenario=scenario,
            status_code=0,
            latency_ms=round(latency_ms, 3),
            ok=False,
            error=exc.__class__.__name__,
        )


async def run_load_phase(
    *,
    base_url: str,
    tenant_headers: list[dict[str, str]],
    scenarios: list[str],
    rps: float,
    duration_seconds: float,
    concurrency: int,
) -> list[Sample]:
    samples: list[Sample] = []
    semaphore = asyncio.Semaphore(concurrency)
    total_requests = max(1, int(rps * duration_seconds))
    interval = 1.0 / rps

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        client.headers_by_tenant = tenant_headers  # type: ignore[attr-defined]

        async def run_one(index: int) -> None:
            async with semaphore:
                scenario = scenarios[index % len(scenarios)]
                samples.append(await _request(client, scenario, index))

        tasks = []
        started_at = time.perf_counter()
        for index in range(total_requests):
            tasks.append(asyncio.create_task(run_one(index)))
            target_time = started_at + ((index + 1) * interval)
            await asyncio.sleep(max(0.0, target_time - time.perf_counter()))
        await asyncio.gather(*tasks)
    return samples


def wait_for_async_jobs(tenant_ids: list[str], *, timeout_seconds: float) -> int:
    deadline = time.monotonic() + timeout_seconds
    pending_count = 0
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            pending = session.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.tenant_id.in_(tenant_ids),
                    Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
                )
            )
        pending_count = int(pending or 0)
        if pending_count == 0:
            return 0
        time.sleep(1)
    return pending_count


def collect_db_metrics(tenant_ids: list[str]) -> dict[str, Any]:
    with SessionLocal() as session:
        routing_overheads = [
            float(metrics["routing_overhead_ms"])
            for metrics in (
                row.decision_json.get("metrics", {})
                for row in session.scalars(
                    select(RoutingDecision).where(RoutingDecision.tenant_id.in_(tenant_ids))
                )
            )
            if "routing_overhead_ms" in metrics
        ]
        provider_attempts = int(
            session.scalar(
                select(func.count())
                .select_from(ProviderAttempt)
                .where(ProviderAttempt.tenant_id.in_(tenant_ids))
            )
            or 0
        )
        failed_attempts = int(
            session.scalar(
                select(func.count())
                .select_from(ProviderAttempt)
                .where(
                    ProviderAttempt.tenant_id.in_(tenant_ids),
                    ProviderAttempt.status != "succeeded",
                )
            )
            or 0
        )
        billing_rows = int(
            session.scalar(
                select(func.count())
                .select_from(BillingLedgerEntry)
                .where(BillingLedgerEntry.tenant_id.in_(tenant_ids))
            )
            or 0
        )
        duplicate_jobs = int(
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.tenant_id.in_(tenant_ids),
                    Job.status == JobStatus.SUCCEEDED.value,
                    Job.attempt_count > 1,
                )
            )
            or 0
        )
    return {
        "routing_overhead_p50_ms": percentile(routing_overheads, 0.50),
        "routing_overhead_p95_ms": percentile(routing_overheads, 0.95),
        "routing_overhead_p99_ms": percentile(routing_overheads, 0.99),
        "provider_attempt_count": provider_attempts,
        "fallback_or_failed_attempt_count": failed_attempts,
        "fallback_rate": round(failed_attempts / provider_attempts, 6) if provider_attempts else 0,
        "billing_ledger_count": billing_rows,
        "succeeded_jobs_with_multiple_attempts": duplicate_jobs,
    }


def verify_idempotency_replay(base_url: str, tenant_header: dict[str, str], tenant_id: str) -> bool:
    idem = f"load-replay-{time.time_ns()}"
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        first = client.post(
            "/v1/documents/invoices:extract",
            headers={**tenant_header, "Idempotency-Key": idem},
            data={
                "metadata": json.dumps(
                    {"provider_set": "documents-default", "line_items_mode": "preferred"}
                )
            },
            files={"file": ("invoice.pdf", tiny_pdf_bytes(), "application/pdf")},
        )
        first.raise_for_status()
        with SessionLocal() as session:
            attempts_before = int(
                session.scalar(
                    select(func.count())
                    .select_from(ProviderAttempt)
                    .where(ProviderAttempt.tenant_id == tenant_id)
                )
                or 0
            )
            billing_before = int(
                session.scalar(
                    select(func.count())
                    .select_from(BillingLedgerEntry)
                    .where(BillingLedgerEntry.tenant_id == tenant_id)
                )
                or 0
            )
        replay = client.post(
            "/v1/documents/invoices:extract",
            headers={**tenant_header, "Idempotency-Key": idem},
            data={
                "metadata": json.dumps(
                    {"provider_set": "documents-default", "line_items_mode": "preferred"}
                )
            },
            files={"file": ("invoice.pdf", tiny_pdf_bytes(), "application/pdf")},
        )
        replay.raise_for_status()
        with SessionLocal() as session:
            attempts_after = int(
                session.scalar(
                    select(func.count())
                    .select_from(ProviderAttempt)
                    .where(ProviderAttempt.tenant_id == tenant_id)
                )
                or 0
            )
            billing_after = int(
                session.scalar(
                    select(func.count())
                    .select_from(BillingLedgerEntry)
                    .where(BillingLedgerEntry.tenant_id == tenant_id)
                )
                or 0
            )
    return (
        replay.json().get("replayed") is True
        and attempts_before == attempts_after
        and billing_before == billing_after
    )


def summarize_samples(samples: list[Sample], elapsed_seconds: float) -> dict[str, Any]:
    latencies = [sample.latency_ms for sample in samples]
    failures = [sample for sample in samples if not sample.ok]
    by_scenario: dict[str, dict[str, Any]] = {}
    for scenario in sorted({sample.scenario for sample in samples}):
        scenario_samples = [sample for sample in samples if sample.scenario == scenario]
        scenario_latencies = [sample.latency_ms for sample in scenario_samples]
        by_scenario[scenario] = {
            "count": len(scenario_samples),
            "error_count": len([sample for sample in scenario_samples if not sample.ok]),
            "p95_ms": percentile(scenario_latencies, 0.95),
        }
    return {
        "request_count": len(samples),
        "actual_rps": round(len(samples) / max(elapsed_seconds, 0.001), 3),
        "error_count": len(failures),
        "error_rate": round(len(failures) / len(samples), 6) if samples else 0,
        "client_latency_p50_ms": percentile(latencies, 0.50),
        "client_latency_p95_ms": percentile(latencies, 0.95),
        "client_latency_p99_ms": percentile(latencies, 0.99),
        "client_latency_avg_ms": round(statistics.fmean(latencies), 3) if latencies else 0,
        "by_scenario": by_scenario,
        "errors": [
            {
                "scenario": sample.scenario,
                "status_code": sample.status_code,
                "error": sample.error,
            }
            for sample in failures[:25]
        ],
    }


async def main_async() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 4A local load hardening.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--admin-token", default=DEFAULT_ADMIN_TOKEN)
    parser.add_argument("--tenant-count", type=int, default=64)
    parser.add_argument("--sustained-rps", type=float, default=25.0)
    parser.add_argument("--sustained-duration", type=float, default=300.0)
    parser.add_argument("--peak-rps", type=float, default=100.0)
    parser.add_argument("--peak-duration", type=float, default=60.0)
    parser.add_argument("--concurrency", type=int, default=200)
    parser.add_argument("--job-drain-timeout", type=float, default=120.0)
    args = parser.parse_args()

    report_dir = create_report_dir("load")
    scenarios = [
        "healthz",
        "core_run",
        "core_submit",
        "documents_process",
        "documents_submit",
        "invoices_extract",
        "invoices_submit",
    ]
    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        client.get("/healthz").raise_for_status()
        tenants = [
            seed_hardening_tenant(
                client,
                admin_token=args.admin_token,
                name=f"phase4a-load-{index}-{time.time_ns()}",
            )
            for index in range(args.tenant_count)
        ]
    tenant_headers = [{"Authorization": f"Bearer {tenant.api_key}"} for tenant in tenants]
    tenant_ids = [tenant.tenant_id for tenant in tenants]

    started_at = time.perf_counter()
    sustained = await run_load_phase(
        base_url=args.base_url,
        tenant_headers=tenant_headers,
        scenarios=scenarios,
        rps=args.sustained_rps,
        duration_seconds=args.sustained_duration,
        concurrency=args.concurrency,
    )
    peak = await run_load_phase(
        base_url=args.base_url,
        tenant_headers=tenant_headers,
        scenarios=scenarios,
        rps=args.peak_rps,
        duration_seconds=args.peak_duration,
        concurrency=args.concurrency,
    )
    elapsed_seconds = time.perf_counter() - started_at
    pending_async_jobs = wait_for_async_jobs(tenant_ids, timeout_seconds=args.job_drain_timeout)
    samples = sustained + peak
    sample_metrics = summarize_samples(samples, elapsed_seconds)
    db_metrics = collect_db_metrics(tenant_ids)
    replay_safe = verify_idempotency_replay(args.base_url, tenant_headers[0], tenant_ids[0])
    passed = (
        sample_metrics["error_rate"] <= DEFAULT_ERROR_RATE_LIMIT
        and db_metrics["routing_overhead_p95_ms"] <= DEFAULT_ROUTING_P95_LIMIT_MS
        and db_metrics["succeeded_jobs_with_multiple_attempts"] == 0
        and pending_async_jobs == 0
        and replay_safe
    )
    payload = {
        "passed": passed,
        "tenant_count": len(tenants),
        "sustained_rps_target": args.sustained_rps,
        "peak_rps_target": args.peak_rps,
        "replay_safe": replay_safe,
        "pending_async_jobs": pending_async_jobs,
        **sample_metrics,
        **db_metrics,
    }
    write_report(report_dir, "load", payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not passed:
        raise SystemExit(1)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
