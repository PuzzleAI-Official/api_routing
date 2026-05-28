from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass
from typing import Any

import httpx

HTTP_SUCCESS_CEILING = 400
MAX_ERROR_RATE = 0.01
TINY_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


@dataclass
class Sample:
    scenario: str
    status_code: int
    latency_ms: float
    ok: bool
    error: str | None = None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


async def _wait_job(client: httpx.AsyncClient, api_key: str, job_id: str) -> None:
    for _ in range(60):
        response = await client.get(
            f"/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        payload = response.json()
        if payload["status"] == "succeeded":
            return
        if payload["status"] in {"failed", "cancelled"}:
            raise RuntimeError(f"job {job_id} ended as {payload['status']}")
        await asyncio.sleep(1)
    raise TimeoutError(f"timed out waiting for job {job_id}")


async def _run_one(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    scenario: str,
    sequence: int,
    wait_async: bool,
) -> Sample:
    started = time.perf_counter()
    status_code = 0
    error: str | None = None
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        idem = f"phase4b-load-{scenario}-{time.time_ns()}-{sequence}"
        if scenario == "health":
            response = await client.get("/health")
        elif scenario == "core_sync":
            response = await client.post(
                "/v1/core/mock:run",
                headers={**headers, "Idempotency-Key": idem},
                json={"payload": {"load": "core_sync", "sequence": sequence}},
            )
        elif scenario == "core_async":
            response = await client.post(
                "/v1/core/mock:submit",
                headers={**headers, "Idempotency-Key": idem},
                json={"payload": {"load": "core_async", "sequence": sequence}},
            )
            if wait_async and response.status_code < HTTP_SUCCESS_CEILING:
                await _wait_job(client, api_key, response.json()["job_id"])
        elif scenario == "documents_sync":
            response = await client.post(
                "/v1/documents:process",
                headers={**headers, "Idempotency-Key": idem},
                files={"file": ("load.pdf", TINY_PDF, "application/pdf")},
                data={"metadata": "{}"},
            )
        elif scenario == "documents_async":
            response = await client.post(
                "/v1/documents:submit",
                headers={**headers, "Idempotency-Key": idem},
                files={"file": ("load.pdf", TINY_PDF, "application/pdf")},
                data={"metadata": "{}"},
            )
            if wait_async and response.status_code < HTTP_SUCCESS_CEILING:
                await _wait_job(client, api_key, response.json()["job_id"])
        elif scenario == "invoices_sync":
            response = await client.post(
                "/v1/documents/invoices:extract",
                headers={**headers, "Idempotency-Key": idem},
                files={"file": ("load.pdf", TINY_PDF, "application/pdf")},
            )
        elif scenario == "invoices_async":
            response = await client.post(
                "/v1/documents/invoices:submit",
                headers={**headers, "Idempotency-Key": idem},
                files={"file": ("load.pdf", TINY_PDF, "application/pdf")},
            )
            if wait_async and response.status_code < HTTP_SUCCESS_CEILING:
                await _wait_job(client, api_key, response.json()["job_id"])
        else:
            raise ValueError(f"unknown scenario: {scenario}")
        status_code = response.status_code
        response.raise_for_status()
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok = False
        error = exc.__class__.__name__
    latency_ms = (time.perf_counter() - started) * 1000
    return Sample(
        scenario=scenario,
        status_code=status_code,
        latency_ms=latency_ms,
        ok=ok,
        error=error,
    )


async def _run_phase(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    scenarios: list[str],
    rps: int,
    duration_seconds: int,
    concurrency: int,
    wait_async: bool,
) -> list[Sample]:
    semaphore = asyncio.Semaphore(concurrency)
    tasks: list[asyncio.Task[Sample]] = []
    total = rps * duration_seconds
    start = time.perf_counter()

    async def guarded(index: int) -> Sample:
        async with semaphore:
            scenario = scenarios[index % len(scenarios)]
            return await _run_one(
                client,
                api_key=api_key,
                scenario=scenario,
                sequence=index,
                wait_async=wait_async,
            )

    for index in range(total):
        expected = start + (index / rps)
        delay = expected - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(guarded(index)))
    return await asyncio.gather(*tasks)


def _summarize(samples: list[Sample], elapsed_seconds: float) -> dict[str, Any]:
    latencies = [sample.latency_ms for sample in samples]
    errors = [sample for sample in samples if not sample.ok]
    by_scenario: dict[str, dict[str, Any]] = {}
    for scenario in sorted({sample.scenario for sample in samples}):
        subset = [sample for sample in samples if sample.scenario == scenario]
        subset_latencies = [sample.latency_ms for sample in subset]
        by_scenario[scenario] = {
            "count": len(subset),
            "errors": sum(1 for sample in subset if not sample.ok),
            "p50_ms": _percentile(subset_latencies, 50),
            "p95_ms": _percentile(subset_latencies, 95),
            "p99_ms": _percentile(subset_latencies, 99),
        }
    return {
        "total_requests": len(samples),
        "elapsed_seconds": elapsed_seconds,
        "observed_rps": len(samples) / elapsed_seconds if elapsed_seconds else 0,
        "error_count": len(errors),
        "error_rate": len(errors) / len(samples) if samples else 0,
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
        "mean_ms": statistics.mean(latencies) if latencies else None,
        "by_scenario": by_scenario,
        "error_types": sorted({sample.error for sample in errors if sample.error}),
        "status_codes": {
            str(status_code): sum(1 for sample in samples if sample.status_code == status_code)
            for status_code in sorted({sample.status_code for sample in samples})
        },
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 4B GCP load checks.")
    parser.add_argument("--base-url", default=os.environ.get("GATEWAY_URL"))
    parser.add_argument("--api-key", default=os.environ.get("PUZZLE_API_KEY"))
    parser.add_argument("--rps", type=int, default=25)
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--wait-async", action="store_true")
    parser.add_argument(
        "--scenarios",
        default=(
            "health,core_sync,core_sync,core_async,documents_sync,"
            "documents_async,invoices_sync,invoices_async"
        ),
    )
    args = parser.parse_args()
    if not args.base_url:
        raise SystemExit("GATEWAY_URL or --base-url is required")
    if not args.api_key:
        raise SystemExit("PUZZLE_API_KEY or --api-key is required")
    scenarios = [item.strip() for item in args.scenarios.split(",") if item.strip()]
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60.0) as client:
        started = time.perf_counter()
        samples = await _run_phase(
            client,
            api_key=args.api_key,
            scenarios=scenarios,
            rps=args.rps,
            duration_seconds=args.duration_seconds,
            concurrency=args.concurrency,
            wait_async=args.wait_async,
        )
        elapsed = time.perf_counter() - started
    summary = _summarize(samples, elapsed)
    summary["target_rps"] = args.rps
    summary["target_duration_seconds"] = args.duration_seconds
    summary["scenarios"] = scenarios
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["error_rate"] > MAX_ERROR_RATE:
        raise SystemExit("error rate exceeded 1%")


if __name__ == "__main__":
    asyncio.run(main())
