from __future__ import annotations

import io

import httpx
import pytest
from puzzle import AsyncClient, Client, PuzzleAuthenticationError, PuzzleDependencyUnavailableError

EXPECTED_RETRY_CALLS = 2


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


def test_sdk_jobs_namespace_gets_job() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/job-sdk"
        assert request.headers["authorization"] == "Bearer pzl_live_test"
        return httpx.Response(200, json={"job_id": "job-sdk", "status": "succeeded"})

    client = Client("pzl_live_test", base_url="https://api.example.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    try:
        response = client.jobs.get("job-sdk")
    finally:
        client.close()

    assert response == {"job_id": "job-sdk", "status": "succeeded"}


def test_sdk_retries_transient_idempotent_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": "0"},
                json={"error": {"code": "dependency_unavailable", "message": "try later"}},
            )
        return httpx.Response(200, json={"request_id": "req-sdk", "ok": True})

    client = Client(
        "pzl_live_test",
        base_url="https://api.example.test",
        max_retries=1,
        retry_backoff=0,
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    try:
        response = client.request(payload={"x": 1}, idempotency_key="retry-sdk")
    finally:
        client.close()

    assert response == {"request_id": "req-sdk", "ok": True}
    assert calls == EXPECTED_RETRY_CALLS


def test_sdk_does_not_retry_non_idempotent_post() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={"error": {"code": "dependency_unavailable", "message": "try later"}},
        )

    client = Client(
        "pzl_live_test",
        base_url="https://api.example.test",
        max_retries=3,
        retry_backoff=0,
    )
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    with pytest.raises(PuzzleDependencyUnavailableError):
        client.request(payload={"x": 1})

    client.close()
    assert calls == 1


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


def test_sdk_documents_process_accepts_file_like_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"from-stream.pdf" in body
        assert b"%PDF-1.4" in body
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

    file_obj = io.BytesIO(b"%PDF-1.4\n%%EOF")
    file_obj.name = "from-stream.pdf"
    client = Client("pzl_live_test", base_url="https://api.example.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    try:
        response = client.documents.process(file=file_obj, idempotency_key="doc-sdk")
    finally:
        client.close()

    assert response["document_id"] == "doc-1"


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


@pytest.mark.asyncio
async def test_async_sdk_invoice_submit_sends_workflow_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/documents/invoices:submit"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["idempotency-key"] == "invoice-async-sdk"
        body = await request.aread()
        compact = body.replace(b" ", b"")
        assert b'"provider_set":"documents-default"' in compact
        assert b'"line_items_mode":"preferred"' in compact
        return httpx.Response(200, json={"job_id": "job-async-sdk", "status": "queued"})

    client = AsyncClient("pzl_live_test", base_url="https://api.example.test")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=client.base_url,
    )

    try:
        response = await client.documents.invoices.submit(
            file=b"%PDF-1.4\n%%EOF",
            filename="invoice.pdf",
            idempotency_key="invoice-async-sdk",
        )
    finally:
        await client.aclose()

    assert response == {"job_id": "job-async-sdk", "status": "queued"}
