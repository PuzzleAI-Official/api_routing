from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
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
from puzzle_gateway.models import AuditLogEntry, DataDeletionRequest, DocumentObject
from sqlalchemy import func, select

HTTP_NOT_FOUND = 404


def _extract_invoice(
    client: httpx.Client,
    *,
    api_key: str,
    idempotency_key: str,
) -> dict[str, Any]:
    response = client.post(
        "/v1/documents/invoices:extract",
        headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": idempotency_key},
        data={
            "metadata": json.dumps(
                {"provider_set": "documents-default", "line_items_mode": "preferred"}
            )
        },
        files={"file": ("invoice.pdf", tiny_pdf_bytes(), "application/pdf")},
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Phase 4A local deletion smoke test.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--admin-token", default=DEFAULT_ADMIN_TOKEN)
    args = parser.parse_args()

    report_dir = create_report_dir("deletion")
    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        client.get("/healthz").raise_for_status()
        tenant_a = seed_hardening_tenant(
            client,
            admin_token=args.admin_token,
            name=f"phase4a-delete-a-{time.time_ns()}",
        )
        tenant_b = seed_hardening_tenant(
            client,
            admin_token=args.admin_token,
            name=f"phase4a-delete-b-{time.time_ns()}",
        )
        idempotency_key = f"delete-smoke-{time.time_ns()}"
        first = _extract_invoice(client, api_key=tenant_a.api_key, idempotency_key=idempotency_key)
        document_id = first["document_id"]
        with SessionLocal() as session:
            document = session.get(DocumentObject, document_id)
            if document is None:
                raise RuntimeError("Document row was not created")
            object_ref = document.object_key

        cross_tenant = client.post(
            f"/v1/admin/tenants/{tenant_b.tenant_id}/deletion-requests",
            headers={"X-Admin-Token": args.admin_token},
            json={
                "target_type": "document",
                "target_id": document_id,
                "reason": "phase4a-cross-tenant-check",
            },
        )
        deletion_response = client.post(
            f"/v1/admin/tenants/{tenant_a.tenant_id}/deletion-requests",
            headers={"X-Admin-Token": args.admin_token},
            json={
                "target_type": "document",
                "target_id": document_id,
                "reason": "phase4a-local-smoke",
            },
        )
        deletion_response.raise_for_status()
        deletion = deletion_response.json()
        replay = _extract_invoice(client, api_key=tenant_a.api_key, idempotency_key=idempotency_key)

    with SessionLocal() as session:
        document = session.get(DocumentObject, document_id)
        deletion_rows = int(
            session.scalar(
                select(func.count())
                .select_from(DataDeletionRequest)
                .where(DataDeletionRequest.tenant_id == tenant_a.tenant_id)
            )
            or 0
        )
        audit_rows = int(
            session.scalar(
                select(func.count())
                .select_from(AuditLogEntry)
                .where(AuditLogEntry.tenant_id == tenant_a.tenant_id)
            )
            or 0
        )

    passed = (
        cross_tenant.status_code == HTTP_NOT_FOUND
        and deletion["status"] == "completed"
        and deletion["summary"]["object_refs_deleted"] >= 1
        and document is not None
        and document.filename == "[deleted]"
        and not Path(object_ref).exists()
        and replay.get("redacted") is True
        and replay.get("replayed") is True
        and deletion_rows >= 1
        and audit_rows >= 1
    )
    payload = {
        "passed": passed,
        "tenant_id": tenant_a.tenant_id,
        "document_id": document_id,
        "cross_tenant_status_code": cross_tenant.status_code,
        "deletion_status": deletion["status"],
        "object_refs_deleted": deletion["summary"]["object_refs_deleted"],
        "replay_redacted": replay.get("redacted") is True,
        "deletion_rows": deletion_rows,
        "audit_rows": audit_rows,
    }
    write_report(report_dir, "deletion", payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
