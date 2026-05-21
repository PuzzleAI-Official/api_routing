from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from puzzle_shared import BillingUsage, DocumentProcessRequest, DocumentProcessResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.cost import charge_units_for_cost
from puzzle_gateway.documents.adapters import (
    DocumentAdapterResult,
    DocumentFile,
    DocumentProviderError,
    UnsupportedCapability,
    get_document_adapter,
)
from puzzle_gateway.documents.manifests import DOCUMENT_OPERATION, spec_from_row
from puzzle_gateway.errors import ProviderTimeoutError, ProviderUnavailableError, ValidationError
from puzzle_gateway.models import (
    BillingLedgerEntry,
    DocumentObject,
    DocumentResult,
    ProviderAttempt,
    ProviderCredential,
    ProviderExternalJob,
    ProviderServiceManifest,
    RoutingDecision,
)
from puzzle_gateway.storage import ObjectStore
from puzzle_gateway.vault import Vault


@dataclass(frozen=True)
class DocumentExecutionResult:
    response: DocumentProcessResponse
    attempted_provider_services: list[dict[str, str]]
    billing_entry_id: str


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


def _credential_json(
    session: Session,
    *,
    tenant_id: str,
    provider_id: str,
    requires_credentials: bool,
) -> dict[str, Any]:
    if not requires_credentials:
        return {}
    credential = _active_credential(session, tenant_id=tenant_id, provider_id=provider_id)
    if credential is None:
        raise ValidationError(f"Credential for provider '{provider_id}' does not exist")
    secret = Vault().retrieve(
        session,
        tenant_id=tenant_id,
        credential_id=credential.id,
        actor="provider-executor",
    )
    parsed = json.loads(secret)
    if not isinstance(parsed, dict):
        raise ValidationError(f"Credential for provider '{provider_id}' is malformed")
    return parsed


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


def _page_count(result: DocumentAdapterResult) -> int:
    if result.pages:
        return len(result.pages)
    return 1


def _response_from_adapter_result(
    *,
    request_id: str,
    document_id: str,
    provider_id: str,
    service_id: str,
    adapter_result: DocumentAdapterResult,
    requested: DocumentProcessRequest,
    raw_result_ref: str,
    usage: BillingUsage,
) -> DocumentProcessResponse:
    satisfied = sorted(set(adapter_result.capabilities_satisfied))
    missing = sorted(set(requested.optional_capabilities) - set(satisfied))
    return DocumentProcessResponse(
        request_id=request_id,
        document_id=document_id,
        provider_id=provider_id,
        service_id=service_id,
        provider_request_id=adapter_result.provider_request_id,
        capabilities_satisfied=satisfied,
        capabilities_missing=missing,
        full_text=adapter_result.full_text,
        pages=adapter_result.pages,
        blocks=adapter_result.blocks,
        tables=adapter_result.tables,
        fields=adapter_result.fields,
        classification=adapter_result.classification,
        chunks=adapter_result.chunks,
        citations=adapter_result.citations,
        confidence=adapter_result.confidence,
        warnings=adapter_result.warnings,
        raw_result_ref=raw_result_ref,
        usage=usage,
    )


def _record_failed_attempt(
    session: Session,
    *,
    tenant_id: str,
    request_id: str,
    job_id: str | None,
    routing_decision_id: str,
    provider_id: str,
    service_id: str,
    error_code: str,
) -> None:
    session.add(
        ProviderAttempt(
            tenant_id=tenant_id,
            request_id=request_id,
            job_id=job_id,
            routing_decision_id=routing_decision_id,
            provider=provider_id,
            service_id=service_id,
            status="failed",
            error_code=error_code,
            latency_ms=0,
            cost_units=0,
            capability_metadata_json={},
        )
    )
    session.flush()


def execute_document_routing_decision(
    session: Session,
    *,
    tenant_id: str,
    document: DocumentFile,
    request: DocumentProcessRequest,
    decision: RoutingDecision,
    object_store: ObjectStore,
    circuit_breaker: CircuitBreaker,
    idempotency_key: str | None,
    job_id: str | None = None,
) -> DocumentExecutionResult:
    decision_payload = decision.decision_json
    attempted: list[dict[str, str]] = []
    last_error: str | None = None

    for item in decision_payload["ordered_provider_services"]:
        provider_id = str(item["provider_id"])
        service_id = str(item["service_id"])
        provider_key = f"{provider_id}:{service_id}"
        if not circuit_breaker.is_available(tenant_id, provider_key, DOCUMENT_OPERATION):
            continue
        attempted.append({"provider_id": provider_id, "service_id": service_id})
        manifest_row = _manifest_for(
            session,
            tenant_id=tenant_id,
            provider_id=provider_id,
            service_id=service_id,
        )
        manifest = spec_from_row(manifest_row)
        adapter = get_document_adapter(provider_id, service_id)
        try:
            credentials = _credential_json(
                session,
                tenant_id=tenant_id,
                provider_id=provider_id,
                requires_credentials=bool(
                    manifest.service_config.get("requires_credentials", True)
                ),
            )
            adapter_result = adapter.execute_sync(
                document=document,
                credentials=credentials,
                manifest=manifest,
                provider_options=request.provider_options.get(provider_id, {}),
            )
        except UnsupportedCapability as exc:
            last_error = "unsupported_capability"
            _record_failed_attempt(
                session,
                tenant_id=tenant_id,
                request_id=decision.request_id,
                job_id=job_id,
                routing_decision_id=decision.id,
                provider_id=provider_id,
                service_id=service_id,
                error_code=last_error,
            )
            circuit_breaker.record_failure(tenant_id, provider_key, DOCUMENT_OPERATION)
            if not str(exc):
                continue
            continue
        except DocumentProviderError as exc:
            last_error = exc.error_code
            _record_failed_attempt(
                session,
                tenant_id=tenant_id,
                request_id=decision.request_id,
                job_id=job_id,
                routing_decision_id=decision.id,
                provider_id=provider_id,
                service_id=service_id,
                error_code=exc.error_code,
            )
            circuit_breaker.record_failure(tenant_id, provider_key, DOCUMENT_OPERATION)
            continue

        page_count = _page_count(adapter_result)
        cost_units = adapter.estimate_cost(manifest=manifest, page_count=page_count)
        charge_units = charge_units_for_cost(cost_units)
        raw_ref = object_store.put_bytes(
            tenant_id=tenant_id,
            name=f"raw/{decision.request_id}-{provider_id}-{service_id}.json",
            data=json.dumps(adapter_result.raw, sort_keys=True).encode("utf-8"),
        )
        usage = BillingUsage(
            cost_units=cost_units,
            charge_units=charge_units,
            provider=provider_id,
            service_id=service_id,
        )
        response = _response_from_adapter_result(
            request_id=decision.request_id,
            document_id=document.document_id,
            provider_id=provider_id,
            service_id=service_id,
            adapter_result=adapter_result,
            requested=request,
            raw_result_ref=raw_ref,
            usage=usage,
        )
        attempt = ProviderAttempt(
            tenant_id=tenant_id,
            request_id=decision.request_id,
            job_id=job_id,
            routing_decision_id=decision.id,
            provider=provider_id,
            service_id=service_id,
            provider_request_id=adapter_result.provider_request_id,
            status="succeeded",
            latency_ms=int(manifest.cost_model.get("latency_ms", 0)),
            cost_units=cost_units,
            capability_metadata_json={
                "satisfied": response.capabilities_satisfied,
                "missing": response.capabilities_missing,
            },
        )
        ledger = BillingLedgerEntry(
            tenant_id=tenant_id,
            request_id=decision.request_id,
            operation=DOCUMENT_OPERATION,
            provider=provider_id,
            service_id=service_id,
            cost_units=cost_units,
            charge_units=charge_units,
            idempotency_key=idempotency_key,
        )
        result = DocumentResult(
            tenant_id=tenant_id,
            document_id=document.document_id,
            request_id=decision.request_id,
            job_id=job_id,
            provider_id=provider_id,
            service_id=service_id,
            normalized_result_json=response.model_dump(mode="json"),
            raw_result_ref=raw_ref,
        )
        session.add_all([attempt, ledger, result])
        circuit_breaker.record_success(tenant_id, provider_key, DOCUMENT_OPERATION)
        session.flush()
        return DocumentExecutionResult(
            response=response,
            attempted_provider_services=attempted,
            billing_entry_id=ledger.id,
        )

    if last_error == "timeout":
        raise ProviderTimeoutError("All document provider services timed out or were unavailable")
    raise ProviderUnavailableError("All document provider services failed or were unavailable")


def submit_provider_async_job(
    session: Session,
    *,
    tenant_id: str,
    job_id: str,
    provider_id: str,
    service_id: str,
    external_job_id: str,
    metadata: dict[str, Any] | None = None,
) -> ProviderExternalJob:
    row = ProviderExternalJob(
        tenant_id=tenant_id,
        job_id=job_id,
        provider_id=provider_id,
        service_id=service_id,
        external_job_id=external_job_id,
        status="submitted",
        metadata_json=metadata or {},
    )
    session.add(row)
    session.flush()
    return row


def document_file_from_object(
    session: Session,
    *,
    document_id: str,
    object_store: ObjectStore,
) -> DocumentFile:
    document = session.get(DocumentObject, document_id)
    if document is None:
        raise ValidationError("Document object does not exist")
    data = object_store.get_bytes(document.object_key)
    return DocumentFile(
        document_id=document.id,
        filename=document.filename,
        mime_type=document.mime_type,
        data=data,
        sha256=document.sha256,
    )
