from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from puzzle_shared import ProviderServiceStatusView
from puzzle_shared.schemas import (
    DocumentCapability,
    ProviderCapabilityStatus,
    ProviderValidationStatus,
)
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from puzzle_gateway.documents.adapters import (
    DocumentAdapterResult,
    DocumentFile,
    DocumentProviderError,
    UnsupportedCapability,
    get_document_adapter,
)
from puzzle_gateway.documents.manifests import ASYNC_PROVIDER_NATIVE, spec_from_row
from puzzle_gateway.documents.storage import sha256_bytes
from puzzle_gateway.errors import ValidationError
from puzzle_gateway.models import (
    AuditLogEntry,
    ProviderCredential,
    ProviderServiceManifest,
    ProviderServiceValidationHistory,
    now_utc,
)
from puzzle_gateway.storage import ObjectStore
from puzzle_gateway.vault import Vault

PHASE25_CAPABILITIES: dict[str, set[str]] = {
    "mindee": {
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.ASYNC.value,
    },
    "veryfi": {
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.TABLES.value,
    },
    "nanonets": {
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.TABLES.value,
    },
    "klippa": {
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.TABLES.value,
        DocumentCapability.ASYNC.value,
    },
}


@dataclass(frozen=True)
class ProviderValidationResult:
    tenant_id: str
    provider_id: str
    service_id: str
    status: str
    checked_capabilities: list[str]
    verified_capabilities: list[str]
    capability_status: dict[str, str]
    summary: dict[str, Any]
    raw_result_ref: str | None = None
    error_code: str | None = None
    written: bool = False

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "provider_id": self.provider_id,
            "service_id": self.service_id,
            "status": self.status,
            "checked_capabilities": self.checked_capabilities,
            "verified_capabilities": self.verified_capabilities,
            "capability_status": self.capability_status,
            "summary": self.summary,
            "raw_result_ref": self.raw_result_ref,
            "error_code": self.error_code,
            "written": self.written,
        }


def _active_credential(
    session: Session,
    *,
    tenant_id: str,
    provider_id: str,
) -> ProviderCredential | None:
    return session.scalars(
        select(ProviderCredential).where(
            ProviderCredential.tenant_id == tenant_id,
            ProviderCredential.provider == provider_id,
            ProviderCredential.active.is_(True),
        )
    ).first()


def _manifest_for(
    session: Session,
    *,
    tenant_id: str,
    provider_id: str,
    service_id: str,
) -> ProviderServiceManifest:
    row = session.scalars(
        select(ProviderServiceManifest).where(
            ProviderServiceManifest.tenant_id == tenant_id,
            ProviderServiceManifest.provider_id == provider_id,
            ProviderServiceManifest.service_id == service_id,
        )
    ).first()
    if row is None:
        raise ValidationError(f"Provider service '{provider_id}:{service_id}' is not configured")
    return row


def _credential_json(
    session: Session,
    *,
    tenant_id: str,
    provider_id: str,
    required_keys: list[str],
    requires_credentials: bool,
) -> dict[str, Any]:
    if not requires_credentials:
        return {}
    credential = _active_credential(session, tenant_id=tenant_id, provider_id=provider_id)
    if credential is None:
        raise DocumentProviderError(
            "Provider credential is missing",
            error_code="credential_missing",
        )
    secret = Vault().retrieve(
        session,
        tenant_id=tenant_id,
        credential_id=credential.id,
        actor="provider-validation",
    )
    parsed = json.loads(secret)
    if not isinstance(parsed, dict):
        raise DocumentProviderError(
            "Provider credential is malformed",
            error_code="credential_malformed",
        )
    missing = [key for key in required_keys if not parsed.get(key)]
    if missing:
        raise DocumentProviderError(
            "Provider credential is incomplete",
            error_code="credential_incomplete",
        )
    return parsed


def _mime_type_for(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _checked_capabilities(
    *,
    provider_id: str,
    declared: list[str],
    check_async: bool,
) -> list[str]:
    supported = (
        {
            DocumentCapability.TEXT.value,
            DocumentCapability.FIELDS.value,
            DocumentCapability.TABLES.value,
            DocumentCapability.ASYNC.value,
        }
        if provider_id.startswith("fake-doc")
        else PHASE25_CAPABILITIES.get(provider_id, set(declared))
    )
    checked = sorted(set(declared) & supported)
    if not check_async and DocumentCapability.ASYNC.value in checked:
        checked.remove(DocumentCapability.ASYNC.value)
    return checked


def _has_text(result: DocumentAdapterResult) -> bool:
    return bool(result.full_text.strip()) or any(page.text.strip() for page in result.pages)


def _has_tables(result: DocumentAdapterResult) -> bool:
    return any(table.rows for table in result.tables)


def _verified_from_result(result: DocumentAdapterResult, checked: list[str]) -> list[str]:
    verified: set[str] = set()
    if DocumentCapability.TEXT.value in checked and _has_text(result):
        verified.add(DocumentCapability.TEXT.value)
    if DocumentCapability.FIELDS.value in checked and bool(result.fields):
        verified.add(DocumentCapability.FIELDS.value)
    if DocumentCapability.TABLES.value in checked and _has_tables(result):
        verified.add(DocumentCapability.TABLES.value)
    return sorted(verified)


def _summary_from_result(
    *,
    result: DocumentAdapterResult | None,
    checked: list[str],
    verified: list[str],
    sample_path: Path | None,
) -> dict[str, Any]:
    if result is None:
        return {
            "sample_filename": sample_path.name if sample_path else None,
            "checked_capabilities": checked,
            "verified_capabilities": verified,
            "has_text": False,
            "field_count": 0,
            "table_count": 0,
            "page_count": 0,
            "warning_count": 0,
        }
    return {
        "sample_filename": sample_path.name if sample_path else None,
        "provider_request_id_present": result.provider_request_id is not None,
        "checked_capabilities": checked,
        "verified_capabilities": verified,
        "has_text": _has_text(result),
        "field_count": len(result.fields),
        "table_count": len(result.tables),
        "page_count": len(result.pages),
        "warning_count": len(result.warnings),
    }


def _capability_status(
    *,
    declared: list[str],
    checked: list[str],
    verified: list[str],
    failed_validation: bool,
) -> dict[str, str]:
    status = {capability: ProviderCapabilityStatus.CONFIGURED.value for capability in declared}
    for capability in set(checked) - set(verified):
        status[capability] = ProviderCapabilityStatus.FAILED.value
    for capability in verified:
        status[capability] = ProviderCapabilityStatus.VERIFIED.value
    if failed_validation:
        for capability in checked:
            status[capability] = ProviderCapabilityStatus.FAILED.value
    return status


def _write_validation_state(
    session: Session,
    *,
    row: ProviderServiceManifest,
    result: ProviderValidationResult,
    actor: str,
) -> None:
    row.verified_capabilities_json = result.verified_capabilities
    row.capability_status_json = result.capability_status
    row.validation_status = result.status
    row.validation_error_code = result.error_code
    row.last_validated_at = now_utc()
    row.readiness_status = (
        "verified"
        if result.status == ProviderValidationStatus.SUCCEEDED.value
        else "configured"
    )
    row.updated_at = now_utc()
    session.add(row)
    history = ProviderServiceValidationHistory(
        tenant_id=result.tenant_id,
        provider_id=result.provider_id,
        service_id=result.service_id,
        status=result.status,
        checked_capabilities_json=result.checked_capabilities,
        verified_capabilities_json=result.verified_capabilities,
        summary_json=result.summary,
        raw_result_ref=result.raw_result_ref,
        error_code=result.error_code,
    )
    session.add(history)
    session.add(
        AuditLogEntry(
            tenant_id=result.tenant_id,
            actor=actor,
            action="provider_service.validate",
            resource_type="provider_service",
            resource_id=f"{result.provider_id}:{result.service_id}",
            metadata_json={
                "status": result.status,
                "checked_capabilities": result.checked_capabilities,
                "verified_capabilities": result.verified_capabilities,
                "error_code": result.error_code,
            },
        )
    )
    session.flush()


def validate_provider_service(
    session: Session,
    *,
    tenant_id: str,
    provider_id: str,
    service_id: str,
    sample_path: Path | None,
    object_store: ObjectStore,
    write: bool = False,
    check_async: bool = False,
    actor: str = "provider-validation",
) -> ProviderValidationResult:
    row = _manifest_for(
        session,
        tenant_id=tenant_id,
        provider_id=provider_id,
        service_id=service_id,
    )
    spec = spec_from_row(row)
    checked = _checked_capabilities(
        provider_id=provider_id,
        declared=spec.capabilities,
        check_async=check_async,
    )
    adapter = get_document_adapter(provider_id, service_id)
    raw_result_ref: str | None = None
    error_code: str | None = None
    async_error_code: str | None = None
    adapter_result: DocumentAdapterResult | None = None
    verified: list[str] = []
    status = ProviderValidationStatus.SUCCEEDED.value

    try:
        credentials = _credential_json(
            session,
            tenant_id=tenant_id,
            provider_id=provider_id,
            required_keys=[str(key) for key in spec.credential_schema.get("required", [])],
            requires_credentials=bool(spec.service_config.get("requires_credentials", True)),
        )
        if not adapter.validate_credentials(credentials=credentials, manifest=spec):
            raise DocumentProviderError(
                "Provider credential validation failed",
                error_code="credential_invalid",
            )
        if sample_path is not None:
            data = sample_path.read_bytes()
            document = DocumentFile(
                document_id="validation-sample",
                filename=sample_path.name,
                mime_type=_mime_type_for(sample_path),
                data=data,
                sha256=sha256_bytes(data),
            )
            adapter_result = adapter.execute_sync(
                document=document,
                credentials=credentials,
                manifest=spec,
                provider_options={},
            )
            verified = _verified_from_result(adapter_result, checked)
            if write:
                raw_result_ref = object_store.put_bytes(
                    tenant_id=tenant_id,
                    name=f"validation/{provider_id}-{service_id}-{document.sha256}.json",
                    data=json.dumps(adapter_result.raw, sort_keys=True).encode("utf-8"),
                )
            if (
                check_async
                and DocumentCapability.ASYNC.value in checked
                and spec.service_config.get("async_execution_mode") == ASYNC_PROVIDER_NATIVE
            ):
                try:
                    external_job_id = adapter.submit_async(
                        document=document,
                        credentials=credentials,
                        manifest=spec,
                        provider_options={},
                    )
                    polled = adapter.poll_async(
                        external_job_id=external_job_id,
                        credentials=credentials,
                        manifest=spec,
                    )
                    if polled is not None:
                        verified = sorted({*verified, DocumentCapability.ASYNC.value})
                except (DocumentProviderError, UnsupportedCapability) as exc:
                    async_error_code = f"async_{getattr(exc, 'error_code', 'unsupported')}"
        missing_observed = set(checked) - set(verified)
        if async_error_code is not None:
            status = ProviderValidationStatus.WARNING.value
            error_code = async_error_code
        elif missing_observed:
            status = ProviderValidationStatus.WARNING.value
            error_code = "capability_not_observed"
    except (DocumentProviderError, UnsupportedCapability) as exc:
        status = ProviderValidationStatus.FAILED.value
        error_code = getattr(exc, "error_code", "unsupported_capability")
    summary = _summary_from_result(
        result=adapter_result,
        checked=checked,
        verified=verified,
        sample_path=sample_path,
    )
    if error_code is not None:
        summary["error_code"] = error_code
    result = ProviderValidationResult(
        tenant_id=tenant_id,
        provider_id=provider_id,
        service_id=service_id,
        status=status,
        checked_capabilities=checked,
        verified_capabilities=verified,
        capability_status=_capability_status(
            declared=spec.capabilities,
            checked=checked,
            verified=verified,
            failed_validation=status == ProviderValidationStatus.FAILED.value,
        ),
        summary=summary,
        raw_result_ref=raw_result_ref,
        error_code=error_code,
        written=write,
    )
    if write:
        _write_validation_state(session, row=row, result=result, actor=actor)
    return result


def latest_validation_summary(
    session: Session,
    *,
    tenant_id: str,
    provider_id: str,
    service_id: str,
) -> dict[str, Any] | None:
    row = session.scalars(
        select(ProviderServiceValidationHistory)
        .where(
            ProviderServiceValidationHistory.tenant_id == tenant_id,
            ProviderServiceValidationHistory.provider_id == provider_id,
            ProviderServiceValidationHistory.service_id == service_id,
        )
        .order_by(desc(ProviderServiceValidationHistory.created_at))
    ).first()
    return None if row is None else row.summary_json


def provider_service_status_view(
    session: Session,
    row: ProviderServiceManifest,
) -> ProviderServiceStatusView:
    return ProviderServiceStatusView(
        tenant_id=row.tenant_id,
        provider_id=row.provider_id,
        service_id=row.service_id,
        display_name=row.display_name,
        declared_capabilities=row.capabilities_json,
        verified_capabilities=row.verified_capabilities_json,
        capability_status=row.capability_status_json,
        readiness_status=row.readiness_status,
        validation_status=row.validation_status,
        validation_error_code=row.validation_error_code,
        last_validated_at=row.last_validated_at,
        async_execution_mode=str(
            row.service_config_json.get("async_execution_mode", "unsupported")
        ),
        supported_mime_types=row.supported_mime_types_json,
        max_sync_bytes=row.max_sync_bytes,
        max_async_bytes=row.max_async_bytes,
        last_validation_summary=latest_validation_summary(
            session,
            tenant_id=row.tenant_id,
            provider_id=row.provider_id,
            service_id=row.service_id,
        ),
    )


def list_provider_service_statuses(
    session: Session,
    *,
    tenant_id: str,
) -> list[ProviderServiceStatusView]:
    rows = session.scalars(
        select(ProviderServiceManifest)
        .where(ProviderServiceManifest.tenant_id == tenant_id)
        .order_by(ProviderServiceManifest.provider_id, ProviderServiceManifest.service_id)
    ).all()
    return [provider_service_status_view(session, row) for row in rows]


def choose_sample_file(sample_dir: Path) -> Path:
    supported_suffixes = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
    candidates = [
        path
        for path in sorted(sample_dir.iterdir())
        if path.is_file() and path.suffix.lower() in supported_suffixes
    ]
    if not candidates:
        raise ValidationError("Sample directory does not contain a supported document")
    return candidates[0]
