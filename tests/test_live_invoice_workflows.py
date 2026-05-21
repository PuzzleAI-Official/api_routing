from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from puzzle_gateway.app import app as fastapi_app
from puzzle_gateway.db import SessionLocal
from puzzle_gateway.documents.validation import validate_provider_service
from puzzle_gateway.documents.workflow_priors import seed_invoice_workflow_priors
from puzzle_gateway.storage import get_object_store

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        os.getenv("PUZZLE_LIVE_PROVIDER") != "1",
        reason="live invoice provider smoke tests are opt-in",
    ),
]

LIVE_INVOICE_PROVIDERS = [
    ("klippa", "generic", "required"),
    ("nanonets", "ocr_model", "preferred"),
]
HTTP_OK = 200


def _live_sample_path() -> Path:
    sample = os.getenv("PUZZLE_LIVE_INVOICE_SAMPLE_PATH") or os.getenv("PUZZLE_LIVE_SAMPLE_PATH")
    assert sample, "PUZZLE_LIVE_INVOICE_SAMPLE_PATH or PUZZLE_LIVE_SAMPLE_PATH is required"
    return Path(sample)


@pytest.mark.parametrize(("provider_id", "service_id", "line_items_mode"), LIVE_INVOICE_PROVIDERS)
def test_live_invoice_extract_smoke(
    provider_id: str,
    service_id: str,
    line_items_mode: str,
) -> None:
    tenant_id = os.getenv("PUZZLE_LIVE_TENANT_ID")
    api_key = os.getenv("PUZZLE_LIVE_API_KEY")
    assert tenant_id, f"PUZZLE_LIVE_TENANT_ID is required for {provider_id}:{service_id}"
    assert api_key, f"PUZZLE_LIVE_API_KEY is required for {provider_id}:{service_id}"
    sample_path = _live_sample_path()

    with SessionLocal() as session:
        seed_invoice_workflow_priors(session, tenant_id=tenant_id)
        validation = validate_provider_service(
            session,
            tenant_id=tenant_id,
            provider_id=provider_id,
            service_id=service_id,
            sample_path=sample_path,
            object_store=get_object_store(),
            write=True,
            check_async=False,
            actor="live-invoice-smoke",
        )
        session.commit()

    assert validation.status in {"succeeded", "warning"}
    assert "documents.text" in validation.verified_capabilities
    assert "documents.fields" in validation.verified_capabilities
    if provider_id == "klippa":
        assert "documents.tables" in validation.verified_capabilities

    client = TestClient(fastapi_app)
    response = client.post(
        "/v1/documents/invoices:extract",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Idempotency-Key": f"live-invoice-{provider_id}-{uuid4()}",
        },
        data={
            "metadata": json.dumps(
                {
                    "provider": provider_id,
                    "service_id": service_id,
                    "line_items_mode": line_items_mode,
                    "required_fields": ["total"],
                }
            )
        },
        files={
            "file": (
                sample_path.name,
                sample_path.read_bytes(),
                "application/pdf" if sample_path.suffix.lower() == ".pdf" else "image/png",
            )
        },
    )
    assert response.status_code == HTTP_OK, response.text
    payload = response.json()
    assert payload["schema_version"] == "documents.invoice.extract.v1"
    assert payload["provider_id"] == provider_id
    assert payload["quality"]["accepted"] is True
    assert payload["invoice"]["total"]["value"]
    if provider_id == "klippa":
        assert payload["invoice"]["line_items"]
    assert "secret" not in json.dumps(payload).lower()
