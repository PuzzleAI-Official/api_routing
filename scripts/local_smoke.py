from __future__ import annotations

import argparse
import time
from typing import Any

import httpx


def _post(
    client: httpx.Client,
    path: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(path, headers=headers, json=json)
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local Puzzle gateway smoke test.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-token", default="local-admin-token")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        health = client.get("/healthz")
        health.raise_for_status()

        admin_headers = {"X-Admin-Token": args.admin_token}
        tenant = _post(
            client,
            "/v1/admin/tenants",
            headers=admin_headers,
            json={"name": f"local-smoke-{int(time.time())}"},
        )
        tenant_id = tenant["tenant_id"]
        api_key = _post(
            client,
            "/v1/admin/api-keys",
            headers=admin_headers,
            json={"tenant_id": tenant_id},
        )["api_key"]
        _post(
            client,
            "/v1/admin/provider-sets/default",
            headers=admin_headers,
            json={"tenant_id": tenant_id},
        )
        for provider, quality, cost in [
            ("mock-primary", 95, 20),
            ("mock-secondary", 80, 8),
        ]:
            _post(
                client,
                "/v1/admin/mock-providers",
                headers=admin_headers,
                json={
                    "tenant_id": tenant_id,
                    "provider": provider,
                    "behavior": "success",
                    "quality_score": quality,
                    "cost_units": cost,
                    "latency_ms": 100,
                },
            )

        api_headers = {
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": f"smoke-{int(time.time())}",
        }
        sync_result = _post(
            client,
            "/v1/core/mock:run",
            headers=api_headers,
            json={"payload": {"smoke": True}, "strategy": "balanced"},
        )
        async_headers = {
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": f"smoke-async-{int(time.time())}",
        }
        async_submit = _post(
            client,
            "/v1/core/mock:submit",
            headers=async_headers,
            json={"payload": {"smoke": "async"}, "strategy": "balanced"},
        )
        job_id = async_submit["job_id"]
        job_result: dict[str, Any] | None = None
        for _ in range(30):
            job_response = client.get(
                f"/v1/jobs/{job_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            job_response.raise_for_status()
            job_result = job_response.json()
            if job_result["status"] in {"succeeded", "failed"}:
                break
            time.sleep(1)
        if job_result is None or job_result["status"] != "succeeded":
            raise RuntimeError(f"Async smoke job did not succeed: {job_result}")
        print(f"tenant_id={tenant_id}")
        print(f"request_id={sync_result['request_id']}")
        print(f"provider={sync_result['usage']['provider']}")
        print(f"job_id={job_id}")
        print(f"job_status={job_result['status']}")


if __name__ == "__main__":
    main()
