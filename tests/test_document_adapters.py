from __future__ import annotations

from typing import Any

import httpx
import pytest
from puzzle_gateway.documents.adapters import (
    BaseDocumentAdapter,
    DocumentFile,
    DocumentProviderError,
    FakeDocumentAdapter,
    KlippaGenericAdapter,
    MindeeModelInferenceAdapter,
    NanonetsOcrModelAdapter,
    UnsupportedCapability,
    VeryfiDocumentsAdapter,
    get_document_adapter,
)
from puzzle_gateway.documents.manifests import (
    fake_document_provider_spec,
    phase2_provider_specs,
)
from puzzle_gateway.documents.routing import DocumentServiceCandidate, _score
from puzzle_shared import ProviderServiceManifestSpec, RoutingStrategy

HTTP_UNAUTHORIZED = 401
HTTP_RATE_LIMITED = 429
HTTP_SERVER_ERROR = 503
EXPECTED_VERYFI_PAGES = 2


def _manifest(provider_id: str) -> ProviderServiceManifestSpec:
    return next(spec for spec in phase2_provider_specs() if spec.provider_id == provider_id)


def test_base_adapter_error_classification() -> None:
    adapter = BaseDocumentAdapter()
    assert adapter.classify_error(httpx.TimeoutException("slow")).error_code == "timeout"
    assert adapter.classify_error(httpx.ConnectError("network")).retryable is True
    assert (
        adapter.classify_error(httpx.Response(HTTP_UNAUTHORIZED)).error_code
        == "auth"
    )
    assert adapter.classify_error(httpx.Response(HTTP_RATE_LIMITED)).retryable is True
    assert adapter.classify_error(httpx.Response(HTTP_SERVER_ERROR)).error_code == "server"
    assert adapter.classify_error(Exception("boom")).error_code == "provider_error"


def test_base_adapter_unsupported_async_methods() -> None:
    adapter = BaseDocumentAdapter()
    manifest = fake_document_provider_spec(provider_id="fake-doc")
    document = DocumentFile(
        document_id="doc",
        filename="tiny.pdf",
        mime_type="application/pdf",
        data=b"%PDF",
        sha256="abc",
    )
    with pytest.raises(UnsupportedCapability):
        adapter.submit_async(
            document=document,
            credentials={},
            manifest=manifest,
            provider_options={},
        )
    with pytest.raises(UnsupportedCapability):
        adapter.poll_async(external_job_id="job", credentials={}, manifest=manifest)


def test_adapter_manifest_and_credential_validation() -> None:
    adapter = MindeeModelInferenceAdapter()
    manifest = adapter.manifest()
    assert manifest.provider_id == "mindee"
    assert adapter.validate_credentials(credentials={"api_key": "key"}, manifest=manifest) is True
    assert adapter.validate_credentials(credentials={}, manifest=manifest) is False
    with pytest.raises(DocumentProviderError):
        adapter._raise_for_response(httpx.Response(HTTP_UNAUTHORIZED))


def test_document_score_strategies() -> None:
    candidate = DocumentServiceCandidate(
        provider_id="fake-doc-1",
        service_id="parse",
        quality_score=0.9,
        cost_units=10,
        latency_ms=100,
        fallback_priority=1,
        manifest={},
    )
    assert _score(candidate, RoutingStrategy.CHEAPEST)[1] == "lowest_cost"
    assert _score(candidate, RoutingStrategy.HIGHEST_QUALITY)[1] == "highest_quality"
    assert _score(candidate, RoutingStrategy.REGULATED)[1] == "regulated_constraints"
    assert _score(candidate, RoutingStrategy.BALANCED)[1] == "balanced_quality_cost_latency"


def test_provider_config_validation_happens_before_network() -> None:
    document = DocumentFile(
        document_id="doc",
        filename="tiny.pdf",
        mime_type="application/pdf",
        data=b"%PDF",
        sha256="abc",
    )
    mindee_manifest = _manifest("mindee").model_copy(update={"service_config": {}})
    with pytest.raises(DocumentProviderError) as mindee_error:
        MindeeModelInferenceAdapter().execute_sync(
            document=document,
            credentials={"api_key": "key"},
            manifest=mindee_manifest,
            provider_options={},
        )
    assert mindee_error.value.error_code == "provider_config"

    nanonets_manifest = _manifest("nanonets").model_copy(update={"service_config": {}})
    with pytest.raises(DocumentProviderError) as nanonets_error:
        NanonetsOcrModelAdapter().execute_sync(
            document=document,
            credentials={"api_key": "key"},
            manifest=nanonets_manifest,
            provider_options={},
        )
    assert nanonets_error.value.error_code == "provider_config"


def test_fake_adapter_success_and_failure_modes() -> None:
    adapter = FakeDocumentAdapter()
    document = DocumentFile(
        document_id="doc",
        filename="tiny.pdf",
        mime_type="application/pdf",
        data=b"%PDF",
        sha256="abcdef123456",
    )
    success = adapter.execute_sync(
        document=document,
        credentials={},
        manifest=fake_document_provider_spec(provider_id="fake-doc-1"),
        provider_options={},
    )
    assert success.provider_request_id == "fake-abcdef123456"
    assert success.tables
    timeout_manifest = fake_document_provider_spec(provider_id="fake-doc-1", behavior="timeout")
    with pytest.raises(DocumentProviderError) as error:
        adapter.execute_sync(
            document=document,
            credentials={},
            manifest=timeout_manifest,
            provider_options={},
        )
    assert error.value.retryable is True


def test_mindee_normalization() -> None:
    result = MindeeModelInferenceAdapter().normalize(
        {
            "inference": {
                "id": "mindee-req",
                "result": {
                    "fields": {
                        "invoice_number": {"value": "INV-1"},
                        "total": {"value": "12.34"},
                    }
                },
            }
        },
        document=None,
        manifest=_manifest("mindee"),
    )
    assert result.provider_request_id == "mindee-req"
    assert "INV-1" in result.full_text
    assert result.fields["total"]["value"] == "12.34"


def test_veryfi_normalization() -> None:
    result = VeryfiDocumentsAdapter().normalize(
        {
            "id": "veryfi-req",
            "ocr_text": "receipt text",
            "total": 10.5,
            "line_items": [{"description": "item", "total": 10.5}],
            "meta": {"total_pages": 2},
            "warnings": ["low_contrast"],
        },
        manifest=_manifest("veryfi"),
    )
    assert result.provider_request_id == "veryfi-req"
    assert len(result.pages) == EXPECTED_VERYFI_PAGES
    assert result.tables[0].rows[1][0] == "item"
    assert result.warnings == ["low_contrast"]


def test_nanonets_normalization() -> None:
    result = NanonetsOcrModelAdapter().normalize(
        {
            "message": "nanonets-req",
            "result": [
                {
                    "prediction": [
                        {"label": "invoice_number", "ocr_text": "INV-2"},
                        {"label": "total", "value": "99.00"},
                    ]
                }
            ],
        },
        manifest=_manifest("nanonets"),
    )
    assert result.provider_request_id == "nanonets-req"
    assert result.fields["invoice_number"] == "INV-2"
    assert "99.00" in result.full_text


def test_klippa_normalization_and_adapter_registry() -> None:
    result = KlippaGenericAdapter().normalize(
        {
            "data": {
                "id": "klippa-req",
                "full_text": "document text",
                "fields": {"total": "100"},
                "tables": [{"rows": [["a", "b"]], "markdown": "|a|b|"}],
            }
        },
        manifest=_manifest("klippa"),
    )
    assert result.provider_request_id == "klippa-req"
    assert result.fields["total"] == "100"
    assert result.tables[0].markdown == "|a|b|"
    assert get_document_adapter("klippa", "generic").manifest().provider_id == "klippa"
    with pytest.raises(UnsupportedCapability):
        get_document_adapter("missing", "service")


def test_klippa_normalization_handles_component_payload() -> None:
    result = KlippaGenericAdapter().normalize(
        {
            "request_id": "klippa-live-req",
            "data": {
                "components": {
                    "key_value_pairs": {
                        "key_value_pairs": [
                            {
                                "key": {"content": "Invoice Number"},
                                "value": {"content": "INV-2026-0520"},
                            }
                        ]
                    },
                    "tables": {
                        "tables": [
                            {
                                "cells": [
                                    {
                                        "row_index": 0,
                                        "column_index": 0,
                                        "content": "Item",
                                        "header": True,
                                    },
                                    {
                                        "row_index": 1,
                                        "column_index": 0,
                                        "content": "Notebook",
                                        "header": False,
                                    },
                                ]
                            }
                        ]
                    },
                }
            },
        },
        manifest=_manifest("klippa"),
    )

    assert result.provider_request_id == "klippa-live-req"
    assert result.fields["Invoice Number"] == "INV-2026-0520"
    assert result.full_text == "INV-2026-0520"
    assert result.tables[0].rows == [["Item"], ["Notebook"]]


def test_rest_adapters_execute_sync_with_mocked_http(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeHttpClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeHttpClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, **_kwargs: Any) -> httpx.Response:
            if "mindee" in url:
                assert _kwargs["headers"]["Authorization"] == "Token key"
                return httpx.Response(
                    200,
                    json={"inference": {"id": "mindee", "result": {"fields": {"x": "y"}}}},
                )
            if "veryfi" in url:
                return httpx.Response(200, json={"id": "veryfi", "ocr_text": "veryfi text"})
            if "nanonets" in url:
                return httpx.Response(
                    200,
                    json={
                        "message": "nanonets",
                        "result": [{"prediction": [{"label": "x", "ocr_text": "y"}]}],
                    },
                )
            return httpx.Response(
                200,
                json={"data": {"id": "klippa", "full_text": "klippa text", "fields": {}}},
            )

    monkeypatch.setattr(httpx, "Client", FakeHttpClient)
    document = DocumentFile(
        document_id="doc",
        filename="tiny.pdf",
        mime_type="application/pdf",
        data=b"%PDF",
        sha256="abc",
    )
    mindee_manifest = _manifest("mindee").model_copy(
        update={"service_config": {"model_id": "model"}}
    )
    assert MindeeModelInferenceAdapter().execute_sync(
        document=document,
        credentials={"api_key": "key"},
        manifest=mindee_manifest,
        provider_options={},
    ).provider_request_id == "mindee"
    assert VeryfiDocumentsAdapter().execute_sync(
        document=document,
        credentials={
            "client_id": "client",
            "client_secret": "secret",
            "username": "user",
            "api_key": "key",
        },
        manifest=_manifest("veryfi"),
        provider_options={},
    ).provider_request_id == "veryfi"
    nanonets_manifest = _manifest("nanonets").model_copy(
        update={"service_config": {"model_id": "model"}}
    )
    assert NanonetsOcrModelAdapter().execute_sync(
        document=document,
        credentials={"api_key": "key"},
        manifest=nanonets_manifest,
        provider_options={},
    ).provider_request_id == "nanonets"
    assert KlippaGenericAdapter().execute_sync(
        document=document,
        credentials={"api_key": "key"},
        manifest=_manifest("klippa"),
        provider_options={},
    ).provider_request_id == "klippa"


def test_async_provider_methods_with_mocked_http(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeHttpClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.get_calls = 0

        def __enter__(self) -> FakeHttpClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, **_kwargs: Any) -> httpx.Response:
            if "mindee" in url:
                assert _kwargs["headers"]["Authorization"] == "Token key"
                return httpx.Response(200, json={"job": {"id": "mindee-job"}})
            return httpx.Response(200, json={"id": "klippa-job"})

        def get(self, url: str, **_kwargs: Any) -> httpx.Response:
            if "auth" in url:
                return httpx.Response(200, json={"ok": True})
            if "mindee" in url:
                assert _kwargs["headers"]["Authorization"] == "Token key"
                return httpx.Response(
                    200,
                    json={"job": {"status": "Processed"}, "inference": {"id": "mindee-done"}},
                )
            if url.endswith("/result"):
                return httpx.Response(
                    200,
                    json={"data": {"id": "klippa-done", "full_text": "done"}},
                )
            return httpx.Response(200, json={"status": "completed"})

    monkeypatch.setattr(httpx, "Client", FakeHttpClient)
    document = DocumentFile(
        document_id="doc",
        filename="tiny.pdf",
        mime_type="application/pdf",
        data=b"%PDF",
        sha256="abc",
    )
    mindee_manifest = _manifest("mindee").model_copy(
        update={"service_config": {"model_id": "model"}}
    )
    mindee = MindeeModelInferenceAdapter()
    assert mindee.submit_async(
        document=document,
        credentials={"api_key": "key"},
        manifest=mindee_manifest,
        provider_options={},
    ) == "mindee-job"
    mindee_done = mindee.poll_async(
        external_job_id="mindee-job",
        credentials={"api_key": "key"},
        manifest=mindee_manifest,
    )
    assert mindee_done is not None
    assert mindee_done.provider_request_id == "mindee-done"

    klippa = KlippaGenericAdapter()
    assert klippa.validate_credentials(credentials={"api_key": "key"}, manifest=_manifest("klippa"))
    assert klippa.submit_async(
        document=document,
        credentials={"api_key": "key"},
        manifest=_manifest("klippa"),
        provider_options={},
    ) == "klippa-job"
    klippa_done = klippa.poll_async(
        external_job_id="klippa-job",
        credentials={"api_key": "key"},
        manifest=_manifest("klippa"),
    )
    assert klippa_done is not None
    assert klippa_done.provider_request_id == "klippa-done"
