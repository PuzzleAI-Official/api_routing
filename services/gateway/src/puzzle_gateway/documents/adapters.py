from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from puzzle_shared import DocumentBlock, DocumentPage, DocumentTable
from puzzle_shared.schemas import ProviderServiceManifestSpec

from puzzle_gateway.documents.manifests import fake_document_provider_spec, phase2_provider_specs

HTTP_OK = 200
HTTP_ERROR = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_RATE_LIMITED = 429
HTTP_SERVER_ERROR = 500


class UnsupportedCapability(Exception):
    pass


class DocumentProviderError(Exception):
    def __init__(self, message: str, *, error_code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


@dataclass(frozen=True)
class DocumentFile:
    document_id: str
    filename: str
    mime_type: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class DocumentAdapterResult:
    provider_request_id: str | None
    raw: dict[str, Any]
    full_text: str
    pages: list[DocumentPage] = field(default_factory=list)
    blocks: list[DocumentBlock] = field(default_factory=list)
    tables: list[DocumentTable] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    classification: dict[str, Any] | None = None
    chunks: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    confidence: float | None = None
    warnings: list[str] = field(default_factory=list)
    capabilities_satisfied: list[str] = field(default_factory=list)


class DocumentProviderAdapter(Protocol):
    def manifest(self) -> ProviderServiceManifestSpec: ...

    def validate_credentials(
        self,
        *,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
    ) -> bool: ...

    def estimate_cost(
        self,
        *,
        manifest: ProviderServiceManifestSpec,
        page_count: int,
    ) -> int: ...

    def execute_sync(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> DocumentAdapterResult: ...

    def submit_async(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> str: ...

    def poll_async(
        self,
        *,
        external_job_id: str,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult | None: ...

    def classify_error(self, response: httpx.Response | Exception) -> DocumentProviderError: ...


class BaseDocumentAdapter:
    provider_id: str
    service_id: str

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def manifest(self) -> ProviderServiceManifestSpec:
        raise NotImplementedError

    def validate_credentials(
        self,
        *,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
    ) -> bool:
        required = manifest.credential_schema.get("required", [])
        return all(bool(credentials.get(key)) for key in required)

    def estimate_cost(self, *, manifest: ProviderServiceManifestSpec, page_count: int) -> int:
        base = int(manifest.cost_model.get("base_cost_units", 10))
        return max(base, 0) * max(page_count, 1)

    def submit_async(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> str:
        raise UnsupportedCapability(
            f"{manifest.provider_id}:{manifest.service_id} has no async API"
        )

    def poll_async(
        self,
        *,
        external_job_id: str,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult | None:
        raise UnsupportedCapability(
            f"{manifest.provider_id}:{manifest.service_id} has no async API"
        )

    def classify_error(  # noqa: PLR0911
        self,
        response: httpx.Response | Exception,
    ) -> DocumentProviderError:
        if isinstance(response, httpx.TimeoutException):
            return DocumentProviderError("Provider timed out", error_code="timeout", retryable=True)
        if isinstance(response, httpx.HTTPError):
            return DocumentProviderError(
                "Provider network error",
                error_code="network",
                retryable=True,
            )
        if not isinstance(response, httpx.Response):
            return DocumentProviderError(
                "Provider failed",
                error_code="provider_error",
                retryable=True,
            )
        if response.status_code in {HTTP_UNAUTHORIZED, HTTP_FORBIDDEN}:
            return DocumentProviderError("Provider authentication failed", error_code="auth")
        if response.status_code == HTTP_RATE_LIMITED:
            return DocumentProviderError(
                "Provider rate limited",
                error_code="rate_limited",
                retryable=True,
            )
        if response.status_code >= HTTP_SERVER_ERROR:
            return DocumentProviderError(
                "Provider server error",
                error_code="server",
                retryable=True,
            )
        return DocumentProviderError("Provider rejected request", error_code="provider_rejected")

    def _raise_for_response(self, response: httpx.Response) -> None:
        if response.status_code < HTTP_ERROR:
            return
        raise self.classify_error(response)


def _page_from_text(text: str) -> list[DocumentPage]:
    return [DocumentPage(page_number=1, text=text, confidence=None)]


def _rows_from_line_items(line_items: Any) -> list[DocumentTable]:
    if not isinstance(line_items, list) or not line_items:
        return []
    headers = sorted({key for row in line_items if isinstance(row, dict) for key in row})
    rows = [headers]
    for row in line_items:
        if isinstance(row, dict):
            rows.append([str(row.get(header, "")) for header in headers])
    return [DocumentTable(page_number=None, rows=rows)]


def _content_text(value: Any) -> str:
    if isinstance(value, dict):
        raw = value.get("content", value.get("text", value.get("value", "")))
        return str(raw) if raw is not None else ""
    return str(value) if value is not None else ""


def _klippa_component_fields(components: Any) -> dict[str, Any]:
    if not isinstance(components, dict):
        return {}
    raw_pairs = components.get("key_value_pairs", {})
    if isinstance(raw_pairs, dict):
        raw_pairs = raw_pairs.get("key_value_pairs", [])
    if not isinstance(raw_pairs, list):
        return {}
    fields: dict[str, Any] = {}
    for index, pair in enumerate(raw_pairs):
        if not isinstance(pair, dict):
            continue
        key = _content_text(pair.get("key")).strip() or f"field_{index + 1}"
        value = _content_text(pair.get("value")).strip()
        if key and value:
            fields[key] = value
    return fields


def _klippa_component_tables(components: Any) -> list[DocumentTable]:
    if not isinstance(components, dict):
        return []
    raw_tables = components.get("tables", {})
    if isinstance(raw_tables, dict):
        raw_tables = raw_tables.get("tables", [])
    if not isinstance(raw_tables, list):
        return []
    tables: list[DocumentTable] = []
    for table in raw_tables:
        if not isinstance(table, dict):
            continue
        raw_rows = table.get("rows")
        if isinstance(raw_rows, list) and raw_rows:
            tables.append(DocumentTable(rows=raw_rows, markdown=table.get("markdown")))
            continue
        cells = table.get("cells", [])
        if not isinstance(cells, list) or not cells:
            continue
        max_row = max(
            (cell.get("row_index", 0) for cell in cells if isinstance(cell, dict)),
            default=0,
        )
        max_col = max(
            (cell.get("column_index", 0) for cell in cells if isinstance(cell, dict)),
            default=0,
        )
        rows = [["" for _col in range(max_col + 1)] for _row in range(max_row + 1)]
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            row_index = int(cell.get("row_index", 0))
            column_index = int(cell.get("column_index", 0))
            rows[row_index][column_index] = _content_text(cell)
        tables.append(DocumentTable(rows=rows, markdown=table.get("markdown")))
    return tables


class FakeDocumentAdapter(BaseDocumentAdapter):
    provider_id = "fake-doc"
    service_id = "parse"

    def manifest(self) -> ProviderServiceManifestSpec:
        return fake_document_provider_spec(provider_id=self.provider_id, service_id=self.service_id)

    def execute_sync(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> DocumentAdapterResult:
        behavior = str(manifest.service_config.get("behavior", "success"))
        if behavior == "timeout":
            raise DocumentProviderError(
                "Fake document provider timeout",
                error_code="timeout",
                retryable=True,
            )
        if behavior == "transient_failure":
            raise DocumentProviderError(
                "Fake document provider transient failure",
                error_code="transient_failure",
                retryable=True,
            )
        if behavior == "permanent_failure":
            raise DocumentProviderError(
                "Fake document provider permanent failure",
                error_code="permanent_failure",
            )
        if behavior == "malformed_response":
            raise DocumentProviderError(
                "Fake document provider malformed response",
                error_code="malformed_response",
                retryable=True,
            )
        if behavior in {"invoice", "invoice_missing_total"}:
            text = (
                "Acme Supplies invoice INV-1001 issued 2026-05-01. "
                "Subtotal USD 100.00, tax USD 8.25, total USD 108.25."
            )
            fields: dict[str, Any] = {
                "vendor_name": {"value": "Acme Supplies", "confidence": 0.98},
                "invoice_number": {"value": "INV-1001", "confidence": 0.97},
                "invoice_date": {"value": "2026-05-01", "confidence": 0.96},
                "due_date": {"value": "2026-05-31", "confidence": 0.94},
                "currency": {"value": "USD", "confidence": 0.98},
                "subtotal": {"value": "USD 100.00", "confidence": 0.96},
                "tax": {"value": "USD 8.25", "confidence": 0.95},
            }
            if behavior == "invoice":
                fields["total"] = {"value": "USD 108.25", "confidence": 0.99}
            return DocumentAdapterResult(
                provider_request_id=f"fake-invoice-{document.sha256[:12]}",
                raw={
                    "provider": manifest.provider_id,
                    "service": manifest.service_id,
                    "fields": fields,
                },
                full_text=text,
                pages=_page_from_text(text),
                tables=[
                    DocumentTable(
                        page_number=1,
                        rows=[
                            ["description", "quantity", "unit_price", "amount"],
                            ["Widget", "2", "50.00", "100.00"],
                        ],
                    )
                ],
                fields=fields,
                confidence=0.97,
                capabilities_satisfied=manifest.capabilities,
            )
        text = f"Parsed {document.filename} with {manifest.provider_id}:{manifest.service_id}"
        if behavior == "text_only":
            return DocumentAdapterResult(
                provider_request_id=f"fake-{document.sha256[:12]}",
                raw={"provider": manifest.provider_id, "service": manifest.service_id},
                full_text=text,
                pages=_page_from_text(text),
                capabilities_satisfied=[
                    cap for cap in manifest.capabilities if cap != "documents.tables"
                ],
            )
        return DocumentAdapterResult(
            provider_request_id=f"fake-{document.sha256[:12]}",
            raw={
                "provider": manifest.provider_id,
                "service": manifest.service_id,
                "sha256": document.sha256,
            },
            full_text=text,
            pages=_page_from_text(text),
            blocks=[
                DocumentBlock(
                    page_number=1,
                    block_type="paragraph",
                    text=text,
                    confidence=0.99,
                )
            ],
            tables=[
                DocumentTable(
                    page_number=1,
                    rows=[["key", "value"], ["filename", document.filename]],
                )
            ],
            fields={"filename": document.filename, "sha256": document.sha256},
            confidence=0.99,
            capabilities_satisfied=manifest.capabilities,
        )

    def submit_async(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> str:
        return f"fake-job-{document.sha256[:12]}"

    def poll_async(
        self,
        *,
        external_job_id: str,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult | None:
        text = f"Completed fake async job {external_job_id}"
        return DocumentAdapterResult(
            provider_request_id=external_job_id,
            raw={"job_id": external_job_id, "status": "completed"},
            full_text=text,
            pages=_page_from_text(text),
            fields={"job_id": external_job_id},
            tables=[DocumentTable(rows=[["job_id"], [external_job_id]])],
            capabilities_satisfied=manifest.capabilities,
        )


class MindeeModelInferenceAdapter(BaseDocumentAdapter):
    provider_id = "mindee"
    service_id = "model_inference"

    def _auth_headers(self, credentials: dict[str, Any]) -> dict[str, str]:
        api_key = str(credentials["api_key"])
        token = api_key if api_key.lower().startswith("token ") else f"Token {api_key}"
        return {"Authorization": token}

    def manifest(self) -> ProviderServiceManifestSpec:
        return next(
            spec for spec in phase2_provider_specs() if spec.provider_id == self.provider_id
        )

    def execute_sync(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> DocumentAdapterResult:
        model_id = str(manifest.service_config.get("model_id", ""))
        if not model_id:
            raise DocumentProviderError("Mindee model_id is missing", error_code="provider_config")
        headers = self._auth_headers(credentials)
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(
                    "https://api-v2.mindee.net/v2/inferences/enqueue",
                    headers=headers,
                    data={"model_id": model_id},
                    files={"file": (document.filename, document.data, document.mime_type)},
                )
            except httpx.HTTPError as exc:
                raise self.classify_error(exc) from exc
        self._raise_for_response(response)
        return self.normalize(response.json(), document=document, manifest=manifest)

    def submit_async(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> str:
        result = self.execute_sync(
            document=document,
            credentials=credentials,
            manifest=manifest,
            provider_options=provider_options,
        )
        raw_id = result.raw.get("job", {}).get("id") or result.provider_request_id
        if raw_id is None:
            raise DocumentProviderError(
                "Mindee did not return a job id",
                error_code="malformed_response",
            )
        return str(raw_id)

    def poll_async(
        self,
        *,
        external_job_id: str,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult | None:
        headers = self._auth_headers(credentials)
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            try:
                response = client.get(
                    f"https://api-v2.mindee.net/v2/jobs/{external_job_id}",
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise self.classify_error(exc) from exc
        if (
            response.status_code == HTTP_OK
            and response.json().get("job", {}).get("status") != "Processed"
        ):
            return None
        self._raise_for_response(response)
        return self.normalize(response.json(), document=None, manifest=manifest)

    def normalize(
        self,
        raw: dict[str, Any],
        *,
        document: DocumentFile | None,
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult:
        inference = raw.get("inference", raw)
        result = inference.get("result", {}) if isinstance(inference, dict) else {}
        fields = result.get("fields", {}) if isinstance(result, dict) else {}
        field_values = (
            [
                value.get("value", value) if isinstance(value, dict) else value
                for value in fields.values()
            ]
            if isinstance(fields, dict)
            else []
        )
        text = (
            " ".join(str(value) for value in field_values)
            if isinstance(fields, dict)
            else ""
        )
        return DocumentAdapterResult(
            provider_request_id=str(inference.get("id", raw.get("id", ""))) or None,
            raw=raw,
            full_text=text,
            pages=_page_from_text(text) if text else [],
            fields=fields if isinstance(fields, dict) else {},
            capabilities_satisfied=[
                cap for cap in manifest.capabilities if cap != "documents.async"
            ],
        )


class VeryfiDocumentsAdapter(BaseDocumentAdapter):
    provider_id = "veryfi"
    service_id = "documents"

    def manifest(self) -> ProviderServiceManifestSpec:
        return next(
            spec for spec in phase2_provider_specs() if spec.provider_id == self.provider_id
        )

    def build_headers(
        self,
        *,
        credentials: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        payload_to_sign = "timestamp:" + timestamp
        for key, value in payload.items():
            payload_to_sign = f"{payload_to_sign},{key}:{value}"
        signature = base64.b64encode(
            hmac.new(
                str(credentials["client_secret"]).encode("utf-8"),
                payload_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "CLIENT-ID": str(credentials["client_id"]),
            "AUTHORIZATION": f"apikey {credentials['username']}:{credentials['api_key']}",
            "X-VERYFI-REQUEST-TIMESTAMP": timestamp,
            "X-VERYFI-REQUEST-SIGNATURE": signature,
        }

    def execute_sync(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> DocumentAdapterResult:
        encoded = base64.b64encode(document.data).decode("ascii")
        payload = {
            "file_data": encoded,
            "file_name": document.filename,
            "bounding_boxes": True,
            "confidence_details": True,
        }
        headers = self.build_headers(credentials=credentials, payload=payload)
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(
                    "https://api.veryfi.com/api/v8/partner/documents",
                    headers=headers,
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise self.classify_error(exc) from exc
        self._raise_for_response(response)
        return self.normalize(response.json(), manifest=manifest)

    def normalize(
        self,
        raw: dict[str, Any],
        *,
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult:
        text = str(raw.get("ocr_text") or raw.get("text") or "")
        line_items = raw.get("line_items", [])
        fields = {
            key: value
            for key, value in raw.items()
            if key
            in {
                "id",
                "vendor",
                "total",
                "subtotal",
                "tax",
                "currency_code",
                "invoice_number",
                "date",
                "due_date",
                "category",
            }
        }
        total_pages = (
            raw.get("meta", {}).get("total_pages")
            if isinstance(raw.get("meta"), dict)
            else None
        )
        return DocumentAdapterResult(
            provider_request_id=str(raw.get("id", "")) or None,
            raw=raw,
            full_text=text,
            pages=[
                DocumentPage(page_number=page, text=text if page == 1 else "")
                for page in range(1, int(total_pages or 1) + 1)
            ],
            tables=_rows_from_line_items(line_items),
            fields=fields,
            confidence=raw.get("score") if isinstance(raw.get("score"), float) else None,
            warnings=[str(item) for item in raw.get("warnings", []) if item],
            capabilities_satisfied=manifest.capabilities,
        )


class NanonetsOcrModelAdapter(BaseDocumentAdapter):
    provider_id = "nanonets"
    service_id = "ocr_model"

    def manifest(self) -> ProviderServiceManifestSpec:
        return next(
            spec for spec in phase2_provider_specs() if spec.provider_id == self.provider_id
        )

    def execute_sync(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> DocumentAdapterResult:
        model_id = str(manifest.service_config.get("model_id", ""))
        if not model_id:
            raise DocumentProviderError(
                "Nanonets model_id is missing",
                error_code="provider_config",
            )
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(
                    f"https://app.nanonets.com/api/v2/OCR/Model/{model_id}/LabelFile/",
                    auth=(str(credentials["api_key"]), ""),
                    files={"file": (document.filename, document.data, document.mime_type)},
                )
            except httpx.HTTPError as exc:
                raise self.classify_error(exc) from exc
        self._raise_for_response(response)
        return self.normalize(response.json(), manifest=manifest)

    def normalize(
        self,
        raw: dict[str, Any],
        *,
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult:
        result = raw.get("result", [])
        fields: dict[str, Any] = {}
        text_parts: list[str] = []
        if isinstance(result, list):
            for page in result:
                if isinstance(page, dict):
                    predictions = page.get("prediction", [])
                    if isinstance(predictions, list):
                        for item in predictions:
                            if isinstance(item, dict):
                                label = str(item.get("label", "field"))
                                value = item.get(
                                    "ocr_text",
                                    item.get("text", item.get("value", "")),
                                )
                                fields[label] = value
                                text_parts.append(str(value))
        text = "\n".join(part for part in text_parts if part)
        return DocumentAdapterResult(
            provider_request_id=str(raw.get("message", "")) or None,
            raw=raw,
            full_text=text,
            pages=_page_from_text(text) if text else [],
            fields=fields,
            capabilities_satisfied=manifest.capabilities,
        )


class KlippaGenericAdapter(BaseDocumentAdapter):
    provider_id = "klippa"
    service_id = "generic"

    def manifest(self) -> ProviderServiceManifestSpec:
        return next(
            spec for spec in phase2_provider_specs() if spec.provider_id == self.provider_id
        )

    def validate_credentials(
        self,
        *,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
    ) -> bool:
        if not super().validate_credentials(credentials=credentials, manifest=manifest):
            return False
        with httpx.Client(timeout=10.0) as client:
            try:
                response = client.get(
                    "https://dochorizon.klippa.com/api/services/auth/v1/info",
                    headers={"x-api-key": str(credentials["api_key"])},
                )
            except httpx.HTTPError:
                return False
        return response.status_code < HTTP_ERROR

    def execute_sync(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> DocumentAdapterResult:
        payload = {
            "documents": [
                {
                    "filename": document.filename,
                    "content_type": document.mime_type,
                    "data": base64.b64encode(document.data).decode("ascii"),
                }
            ]
        }
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(
                    "https://dochorizon.klippa.com/api/services/document_capturing/v1/generic",
                    headers={"x-api-key": str(credentials["api_key"])},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise self.classify_error(exc) from exc
        self._raise_for_response(response)
        return self.normalize(response.json(), manifest=manifest)

    def submit_async(
        self,
        *,
        document: DocumentFile,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
        provider_options: dict[str, Any],
    ) -> str:
        payload = {
            "documents": [
                {
                    "filename": document.filename,
                    "content_type": document.mime_type,
                    "data": base64.b64encode(document.data).decode("ascii"),
                }
            ]
        }
        with httpx.Client(timeout=self.timeout) as client:
            try:
                response = client.post(
                    "https://dochorizon.klippa.com/api/services/document_capturing/v1/generic/async",
                    headers={"x-api-key": str(credentials["api_key"])},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                raise self.classify_error(exc) from exc
        self._raise_for_response(response)
        raw = response.json()
        job_id = raw.get("id") or raw.get("job_id") or raw.get("data", {}).get("id")
        if not job_id:
            raise DocumentProviderError(
                "Klippa did not return a job id",
                error_code="malformed_response",
            )
        return str(job_id)

    def poll_async(
        self,
        *,
        external_job_id: str,
        credentials: dict[str, Any],
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult | None:
        headers = {"x-api-key": str(credentials["api_key"])}
        with httpx.Client(timeout=self.timeout) as client:
            status_response = client.get(
                f"https://dochorizon.klippa.com/api/services/document_capturing/v1/generic/async/{external_job_id}",
                headers=headers,
            )
            self._raise_for_response(status_response)
            status = str(status_response.json().get("status", "")).lower()
            if status not in {"done", "completed", "complete"}:
                return None
            result_response = client.get(
                f"https://dochorizon.klippa.com/api/services/document_capturing/v1/generic/async/{external_job_id}/result",
                headers=headers,
            )
        self._raise_for_response(result_response)
        return self.normalize(result_response.json(), manifest=manifest)

    def normalize(
        self,
        raw: dict[str, Any],
        *,
        manifest: ProviderServiceManifestSpec,
    ) -> DocumentAdapterResult:
        data = raw.get("data", raw)
        components = data.get("components", {}) if isinstance(data, dict) else {}
        text = str(data.get("text", data.get("full_text", ""))) if isinstance(data, dict) else ""
        fields = {}
        if isinstance(data, dict) and isinstance(data.get("fields"), dict):
            fields = data["fields"]
        elif isinstance(components, dict):
            fields = _klippa_component_fields(components)
        tables = data.get("tables", []) if isinstance(data, dict) else []
        normalized_tables = [
            DocumentTable(rows=table.get("rows", []), markdown=table.get("markdown"))
            for table in tables
            if isinstance(table, dict)
        ]
        if not normalized_tables and isinstance(components, dict):
            normalized_tables = _klippa_component_tables(components)
        if not text and fields:
            text = "\n".join(str(value) for value in fields.values() if value)
        return DocumentAdapterResult(
            provider_request_id=(
                str(data.get("id", raw.get("request_id", raw.get("requestid", ""))))
                if isinstance(data, dict)
                else None
            ),
            raw=raw,
            full_text=text,
            pages=_page_from_text(text) if text else [],
            tables=normalized_tables,
            fields=fields,
            capabilities_satisfied=manifest.capabilities,
        )


def get_document_adapter(provider_id: str, service_id: str) -> DocumentProviderAdapter:
    if provider_id.startswith("fake-doc"):
        adapter = FakeDocumentAdapter()
        adapter.provider_id = provider_id
        adapter.service_id = service_id
        return adapter
    registry: dict[tuple[str, str], DocumentProviderAdapter] = {
        ("mindee", "model_inference"): MindeeModelInferenceAdapter(),
        ("veryfi", "documents"): VeryfiDocumentsAdapter(),
        ("nanonets", "ocr_model"): NanonetsOcrModelAdapter(),
        ("klippa", "generic"): KlippaGenericAdapter(),
    }
    try:
        return registry[(provider_id, service_id)]
    except KeyError as exc:
        raise UnsupportedCapability(f"No adapter for {provider_id}:{service_id}") from exc
