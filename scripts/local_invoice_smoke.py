from __future__ import annotations

import argparse
import json
import time
from typing import Any

import httpx
from puzzle_gateway.db import SessionLocal
from puzzle_gateway.documents.manifests import (
    create_documents_provider_set,
    fake_document_provider_spec,
    upsert_provider_service_manifest,
)
from puzzle_gateway.documents.workflow_priors import seed_invoice_workflow_priors


def _post_json(
    client: httpx.Client,
    path: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(path, headers=headers, json=json_body)
    response.raise_for_status()
    return response.json()


def _post_invoice(
    client: httpx.Client,
    path: str,
    *,
    api_key: str,
    idempotency_key: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        path,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": idempotency_key,
        },
        data={"metadata": json.dumps(metadata)},
        files={"file": ("invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    response.raise_for_status()
    return response.json()


def _seed_fake_invoice_providers(tenant_id: str) -> None:
    with SessionLocal() as session:
        specs = [
            fake_document_provider_spec(
                provider_id="fake-doc-invoice-primary",
                behavior="invoice_missing_total",
                quality_score=95,
                cost_units=5,
            ),
            fake_document_provider_spec(
                provider_id="fake-doc-invoice-secondary",
                behavior="invoice",
                quality_score=90,
                cost_units=6,
            ),
        ]
        for spec in specs:
            upsert_provider_service_manifest(session, tenant_id=tenant_id, spec=spec)
        create_documents_provider_set(session, tenant_id=tenant_id, specs=specs)
        seed_invoice_workflow_priors(session, tenant_id=tenant_id)
        session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local invoice extraction smoke tests.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--admin-token", default="local-admin-token")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        health = client.get("/healthz")
        health.raise_for_status()

        admin_headers = {"X-Admin-Token": args.admin_token}
        tenant = _post_json(
            client,
            "/v1/admin/tenants",
            headers=admin_headers,
            json_body={"name": f"local-invoice-smoke-{int(time.time())}"},
        )
        tenant_id = tenant["tenant_id"]
        api_key = _post_json(
            client,
            "/v1/admin/api-keys",
            headers=admin_headers,
            json_body={"tenant_id": tenant_id},
        )["api_key"]
        _seed_fake_invoice_providers(tenant_id)

        status = client.get(
            f"/v1/admin/tenants/{tenant_id}/workflows/invoice.extract/provider-services",
            headers=admin_headers,
        )
        status.raise_for_status()
        workflow_status = status.json()
        if not any(item["selection_status"] == "eligible" for item in workflow_status):
            raise RuntimeError(f"No eligible invoice provider service: {workflow_status}")

        sync_result = _post_invoice(
            client,
            "/v1/documents/invoices:extract",
            api_key=api_key,
            idempotency_key=f"invoice-smoke-sync-{int(time.time())}",
            metadata={"provider_set": "documents-default", "line_items_mode": "preferred"},
        )
        if sync_result["schema_version"] != "documents.invoice.extract.v1":
            raise RuntimeError(f"Unexpected sync invoice response: {sync_result}")
        if sync_result["quality"]["accepted"] is not True:
            raise RuntimeError(f"Invoice sync quality rejected: {sync_result['quality']}")

        submit = _post_invoice(
            client,
            "/v1/documents/invoices:submit",
            api_key=api_key,
            idempotency_key=f"invoice-smoke-async-{int(time.time())}",
            metadata={"provider_set": "documents-default", "line_items_mode": "preferred"},
        )
        deadline = time.monotonic() + args.timeout
        job_result: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            response = client.get(
                f"/v1/jobs/{submit['job_id']}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            job_result = response.json()
            if job_result["status"] in {"succeeded", "failed"}:
                break
            time.sleep(1)
        if job_result is None or job_result["status"] != "succeeded":
            raise RuntimeError(f"Async invoice job did not succeed: {job_result}")

        print(f"tenant_id={tenant_id}")
        print(f"sync_provider={sync_result['provider_id']}:{sync_result['service_id']}")
        print(f"sync_quality_accepted={sync_result['quality']['accepted']}")
        print(f"job_id={submit['job_id']}")
        print(f"job_status={job_result['status']}")


if __name__ == "__main__":
    main()
