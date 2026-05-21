from __future__ import annotations

import httpx
import pytest
from puzzle import Client, PuzzleAuthenticationError


def test_sdk_core_request_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["idempotency-key"] == "sdk-idem"
        return httpx.Response(200, json={"request_id": "req-sdk", "ok": True})

    client = Client("pzl_live_test", base_url="https://api.example.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    try:
        response = client.request(payload={"x": 1}, idempotency_key="sdk-idem")
    finally:
        client.close()

    assert response == {"request_id": "req-sdk", "ok": True}


def test_sdk_maps_canonical_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "authentication_failed",
                    "message": "Invalid API key",
                    "request_id": "req-error",
                }
            },
        )

    client = Client("bad", base_url="https://api.example.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    with pytest.raises(PuzzleAuthenticationError) as error:
        client.request(payload={})

    assert error.value.request_id == "req-error"


def test_sdk_documents_process_sends_multipart_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/documents:process"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["idempotency-key"] == "doc-sdk"
        body = request.read()
        assert b'name="metadata"' in body
        assert b'"provider":"fake-doc-1"' in body.replace(b" ", b"")
        assert b"tiny.pdf" in body
        return httpx.Response(
            200,
            json={
                "schema_version": "documents.process.v1",
                "request_id": "req-doc",
                "document_id": "doc-1",
                "provider_id": "fake-doc-1",
                "service_id": "parse",
                "raw_result_ref": "raw-ref",
            },
        )

    client = Client("pzl_live_test", base_url="https://api.example.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    try:
        response = client.documents.process(
            file=b"%PDF-1.4\n%%EOF",
            filename="tiny.pdf",
            provider="fake-doc-1",
            service_id="parse",
            idempotency_key="doc-sdk",
        )
    finally:
        client.close()

    assert response["schema_version"] == "documents.process.v1"


def test_sdk_invoice_extract_sends_workflow_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/documents/invoices:extract"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["idempotency-key"] == "invoice-sdk"
        body = request.read()
        compact = body.replace(b" ", b"")
        assert b'name="metadata"' in body
        assert b'"provider":"klippa"' in compact
        assert b'"line_items_mode":"required"' in compact
        assert b'"required_fields":["total","invoice_number"]' in compact
        return httpx.Response(
            200,
            json={
                "schema_version": "documents.invoice.extract.v1",
                "request_id": "req-invoice",
                "document_id": "doc-1",
                "workflow": "invoice.extract",
                "workflow_version": "invoice.extract.v1",
                "provider_id": "klippa",
                "service_id": "generic",
                "execution_plan": {
                    "mode": "single_service",
                    "workflow": "invoice.extract",
                    "segments": [],
                    "policy_version": "workflow_rules_v1",
                },
                "routing_summary": {},
                "invoice": {},
                "quality": {
                    "accepted": True,
                    "completeness_score": 1.0,
                    "line_item_score": 1.0,
                },
                "raw_result_ref": "raw-ref",
            },
        )

    client = Client("pzl_live_test", base_url="https://api.example.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    try:
        response = client.documents.invoices.extract(
            file=b"%PDF-1.4\n%%EOF",
            filename="invoice.pdf",
            provider="klippa",
            service_id="generic",
            line_items_mode="required",
            required_fields=["total", "invoice_number"],
            idempotency_key="invoice-sdk",
        )
    finally:
        client.close()

    assert response["schema_version"] == "documents.invoice.extract.v1"
