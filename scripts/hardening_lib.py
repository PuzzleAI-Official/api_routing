from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://puzzle:puzzle@localhost:5432/puzzle")
os.environ.setdefault("PUZZLE_ENV", "local")
os.environ.setdefault("PUZZLE_DOCUMENT_CAPABILITY_MODE", "configured")

from puzzle_gateway.db import SessionLocal  # noqa: E402
from puzzle_gateway.documents.manifests import (  # noqa: E402
    create_documents_provider_set,
    fake_document_provider_spec,
    upsert_provider_service_manifest,
)
from puzzle_gateway.documents.workflow_priors import upsert_workflow_prior  # noqa: E402
from puzzle_shared import WorkflowProviderPriorSpec  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_ADMIN_TOKEN = "local-admin-token"
REPORT_ROOT = Path(".local/hardening-reports")


@dataclass(frozen=True)
class HardeningTenant:
    tenant_id: str
    api_key: str


def utc_slug() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def create_report_dir(name: str) -> Path:
    path = REPORT_ROOT / f"{utc_slug()}-{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_report(report_dir: Path, name: str, payload: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"{name}.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    status = "PASS" if payload.get("passed") else "FAIL"
    lines = [f"# {name} hardening report", "", f"Status: {status}", ""]
    for key, value in payload.items():
        if key in {"samples", "errors"}:
            continue
        lines.append(f"- {key}: `{value}`")
    (report_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def post_json(
    client: httpx.Client,
    path: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(path, headers=headers, json=json_body)
    response.raise_for_status()
    return response.json()


def seed_hardening_tenant(
    client: httpx.Client,
    *,
    admin_token: str,
    name: str,
) -> HardeningTenant:
    admin_headers = {"X-Admin-Token": admin_token}
    tenant_id = post_json(
        client,
        "/v1/admin/tenants",
        headers=admin_headers,
        json_body={"name": name},
    )["tenant_id"]
    api_key = post_json(
        client,
        "/v1/admin/api-keys",
        headers=admin_headers,
        json_body={"tenant_id": tenant_id},
    )["api_key"]
    post_json(
        client,
        "/v1/admin/provider-sets/default",
        headers=admin_headers,
        json_body={"tenant_id": tenant_id},
    )
    for provider, behavior, quality, cost in (
        ("mock-primary", "success", 95, 20),
        ("mock-secondary", "success", 80, 8),
    ):
        post_json(
            client,
            "/v1/admin/mock-providers",
            headers=admin_headers,
            json_body={
                "tenant_id": tenant_id,
                "provider": provider,
                "behavior": behavior,
                "quality_score": quality,
                "cost_units": cost,
                "latency_ms": 25,
            },
        )
    seed_fake_document_services(tenant_id)
    return HardeningTenant(tenant_id=tenant_id, api_key=api_key)


def seed_fake_document_services(
    tenant_id: str,
    *,
    primary_behavior: str = "success",
    secondary_behavior: str = "success",
    invoice_primary_behavior: str = "invoice",
    invoice_secondary_behavior: str = "invoice",
    include_invoice: bool = True,
) -> None:
    specs = [
        fake_document_provider_spec(
            provider_id="fake-doc-primary",
            behavior=primary_behavior,
            cost_units=5,
            quality_score=80,
        ),
        fake_document_provider_spec(
            provider_id="fake-doc-secondary",
            behavior=secondary_behavior,
            cost_units=7,
            quality_score=75,
        ),
    ]
    if include_invoice:
        specs.extend(
            [
                fake_document_provider_spec(
                    provider_id="fake-doc-invoice-primary",
                    behavior=invoice_primary_behavior,
                    cost_units=6,
                    quality_score=92,
                ),
                fake_document_provider_spec(
                    provider_id="fake-doc-invoice-secondary",
                    behavior=invoice_secondary_behavior,
                    cost_units=8,
                    quality_score=88,
                ),
            ]
        )
    with SessionLocal() as session:
        for spec in specs:
            upsert_provider_service_manifest(session, tenant_id=tenant_id, spec=spec)
        create_documents_provider_set(session, tenant_id=tenant_id, specs=specs)
        if include_invoice:
            for spec, priority, quality in (
                (specs[2], 1, 0.92),
                (specs[3], 2, 0.88),
            ):
                upsert_workflow_prior(
                    session,
                    tenant_id=tenant_id,
                    spec=WorkflowProviderPriorSpec(
                        provider_id=spec.provider_id,
                        service_id=spec.service_id,
                        active=True,
                        quality_prior=quality,
                        fallback_priority=priority,
                        notes="Local Phase 4A hardening fake invoice prior.",
                    ),
                )
        session.commit()


def tiny_pdf_bytes() -> bytes:
    return b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF"
