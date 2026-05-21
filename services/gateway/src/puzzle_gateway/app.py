from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from puzzle_shared import (
    ApiError,
    DocumentProcessRequest,
    InvoiceExtractRequest,
    InvoiceLineItemsMode,
    JobStatus,
    MockProviderBehavior,
)
from puzzle_shared.schemas import CoreMockRunRequest, CoreMockRunResponse, JobView, ProviderSetSpec
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway import __version__
from puzzle_gateway.auth import authenticate_api_key
from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.config import settings
from puzzle_gateway.db import create_all, get_session
from puzzle_gateway.documents.adapters import DocumentFile
from puzzle_gateway.documents.execution import execute_document_routing_decision
from puzzle_gateway.documents.manifests import DOCUMENT_OPERATION
from puzzle_gateway.documents.routing import create_document_routing_decision
from puzzle_gateway.documents.storage import sha256_bytes, store_document_object
from puzzle_gateway.documents.validation import (
    list_provider_service_statuses,
    provider_service_status_view,
)
from puzzle_gateway.documents.workflow_priors import workflow_provider_service_statuses
from puzzle_gateway.documents.workflows import (
    INVOICE_OPERATION,
    create_invoice_routing_decision,
    execute_invoice_workflow_decision,
)
from puzzle_gateway.errors import GatewayError
from puzzle_gateway.idempotency import begin_operation, complete_operation, request_hash
from puzzle_gateway.jobs import submit_job
from puzzle_gateway.kv import InMemoryKVStore
from puzzle_gateway.models import Job, ProviderServiceManifest, Tenant
from puzzle_gateway.provider_sets import upsert_provider_set
from puzzle_gateway.providers import execute_routing_decision
from puzzle_gateway.quotas import check_platform_spend, check_rate_limit, check_tenant_quota
from puzzle_gateway.routing import create_routing_decision
from puzzle_gateway.seed import (
    create_api_key,
    create_default_provider_set,
    create_tenant,
    upsert_mock_provider,
)
from puzzle_gateway.storage import get_object_store
from puzzle_gateway.telemetry import publish_telemetry_outbox


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    create_all()
    yield


app = FastAPI(title="Puzzle Gateway", version=__version__, lifespan=lifespan)
kv = InMemoryKVStore()
circuit_breaker = CircuitBreaker(
    kv,
    failure_threshold=settings.circuit_failure_threshold,
    cooldown_seconds=settings.circuit_cooldown_seconds,
)


class TenantCreateRequest(BaseModel):
    name: str
    region: str = "us-central1"


class ApiKeyCreateRequest(BaseModel):
    tenant_id: str


class MockProviderCreateRequest(BaseModel):
    tenant_id: str
    provider: str
    behavior: MockProviderBehavior = MockProviderBehavior.SUCCESS
    quality_score: int = 90
    cost_units: int = 10
    latency_ms: int = 100


@app.exception_handler(GatewayError)
async def _gateway_error_handler(_request: Request, exc: GatewayError) -> JSONResponse:
    request_id = getattr(_request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": ApiError(
                code=exc.code,
                message=exc.message,
                request_id=request_id,
                details=exc.details,
            ).model_dump(mode="json")
        },
        headers={"x-request-id": request_id or ""},
    )


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next: Any) -> Any:
    request.state.request_id = request.headers.get("x-request-id", str(uuid4()))
    response = await call_next(request)
    response.headers["x-request-id"] = request.state.request_id
    return response


def _api_key_from_header(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> str:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1]
    raise HTTPException(status_code=401, detail="Missing API key")


def current_tenant(
    api_key: Annotated[str, Depends(_api_key_from_header)],
    session: Annotated[Session, Depends(get_session)],
) -> Tenant:
    return authenticate_api_key(session, api_key)


def require_admin(
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    if not settings.admin_api_enabled:
        raise HTTPException(status_code=404, detail="Admin API is disabled")
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")


def _parse_document_metadata(metadata: str) -> DocumentProcessRequest:
    try:
        parsed = json.loads(metadata or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Document metadata must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Document metadata must be a JSON object")
    return DocumentProcessRequest.model_validate(parsed)


def _parse_invoice_metadata(metadata: str) -> InvoiceExtractRequest:
    try:
        parsed = json.loads(metadata or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Invoice metadata must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Invoice metadata must be a JSON object")
    return InvoiceExtractRequest.model_validate(parsed)


def _document_payload_hash(
    *,
    document_request: DocumentProcessRequest,
    file_sha256: str,
    filename: str,
    mime_type: str,
    byte_size: int,
) -> str:
    return request_hash(
        {
            "request": document_request.model_dump(mode="json"),
            "file_sha256": file_sha256,
            "filename": filename,
            "mime_type": mime_type,
            "byte_size": byte_size,
        }
    )


def _invoice_payload_hash(
    *,
    invoice_request: InvoiceExtractRequest,
    file_sha256: str,
    filename: str,
    mime_type: str,
    byte_size: int,
) -> str:
    return request_hash(
        {
            "request": invoice_request.model_dump(mode="json"),
            "file_sha256": file_sha256,
            "filename": filename,
            "mime_type": mime_type,
            "byte_size": byte_size,
        }
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/version")
def version() -> dict[str, str]:
    return {"version": __version__}


@app.post("/v1/admin/tenants", dependencies=[Depends(require_admin)])
def admin_create_tenant(
    body: TenantCreateRequest,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, str]:
    tenant = create_tenant(session, name=body.name, region=body.region)
    session.commit()
    return {"tenant_id": tenant.id}


@app.post("/v1/admin/api-keys", dependencies=[Depends(require_admin)])
def admin_create_api_key(
    body: ApiKeyCreateRequest,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, str]:
    _row, raw_key = create_api_key(session, tenant_id=body.tenant_id)
    session.commit()
    return {"api_key": raw_key}


@app.post("/v1/admin/provider-sets", dependencies=[Depends(require_admin)])
def admin_create_provider_set(
    body: ProviderSetSpec,
    tenant_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, str]:
    row = upsert_provider_set(session, tenant_id=tenant_id, spec=body)
    session.commit()
    return {"provider_set_id": row.id}


@app.post("/v1/admin/mock-providers", dependencies=[Depends(require_admin)])
def admin_create_mock_provider(
    body: MockProviderCreateRequest,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, str]:
    row = upsert_mock_provider(
        session,
        tenant_id=body.tenant_id,
        provider=body.provider,
        behavior=body.behavior,
        quality_score=body.quality_score,
        cost_units=body.cost_units,
        latency_ms=body.latency_ms,
    )
    session.commit()
    return {"mock_provider_id": row.id}


@app.post("/v1/admin/provider-sets/default", dependencies=[Depends(require_admin)])
def admin_create_default_provider_set(
    body: ApiKeyCreateRequest,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, str]:
    create_default_provider_set(session, tenant_id=body.tenant_id)
    session.commit()
    return {"status": "created"}


@app.get("/v1/admin/tenants/{tenant_id}/provider-services", dependencies=[Depends(require_admin)])
def admin_list_provider_services(
    tenant_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> list[dict[str, Any]]:
    return [
        item.model_dump(mode="json")
        for item in list_provider_service_statuses(session, tenant_id=tenant_id)
    ]


@app.get(
    "/v1/admin/tenants/{tenant_id}/provider-services/{provider_id}/{service_id}",
    dependencies=[Depends(require_admin)],
)
def admin_get_provider_service(
    tenant_id: str,
    provider_id: str,
    service_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    row = session.scalars(
        select(ProviderServiceManifest).where(
            ProviderServiceManifest.tenant_id == tenant_id,
            ProviderServiceManifest.provider_id == provider_id,
            ProviderServiceManifest.service_id == service_id,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Provider service not found")
    return provider_service_status_view(session, row).model_dump(mode="json")


@app.get(
    "/v1/admin/tenants/{tenant_id}/workflows/{workflow}/provider-services",
    dependencies=[Depends(require_admin)],
)
def admin_list_workflow_provider_services(
    tenant_id: str,
    workflow: str,
    session: Annotated[Session, Depends(get_session)],
    provider_set: str = "documents-default",
    line_items_mode: InvoiceLineItemsMode = InvoiceLineItemsMode.PREFERRED,
) -> list[dict[str, Any]]:
    return [
        item.model_dump(mode="json")
        for item in workflow_provider_service_statuses(
            session,
            tenant_id=tenant_id,
            workflow=workflow,
            provider_set_name=provider_set,
            line_items_mode=line_items_mode,
        )
    ]


@app.post("/v1/core/mock:run")
def run_mock(
    body: CoreMockRunRequest,
    request: Request,
    tenant: Annotated[Tenant, Depends(current_tenant)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    idempotency_key = idempotency_key_header or body.idempotency_key
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency key is required")

    payload_for_hash = body.model_dump(mode="json")
    start = begin_operation(
        session,
        tenant_id=tenant.id,
        operation=body.operation,
        idempotency_key=idempotency_key,
        payload_hash=request_hash(payload_for_hash),
    )
    if start.replay_response is not None:
        replay_response = dict(start.replay_response)
        replay_response["replayed"] = True
        return replay_response

    check_rate_limit(
        kv,
        tenant_id=tenant.id,
        limit_per_minute=settings.default_rate_limit_per_minute,
    )
    check_tenant_quota(session, tenant)
    check_platform_spend(session, platform_limit_units=settings.platform_spend_limit_units)
    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation=body.operation,
        strategy=body.strategy,
        provider_set_name=body.provider_set,
        circuit_breaker=circuit_breaker,
        explicit_provider=body.provider,
        request_id=request.state.request_id,
        constraints=body.features,
    )
    executed = execute_routing_decision(
        session,
        tenant_id=tenant.id,
        operation=body.operation,
        payload=body.payload,
        decision=decision,
        circuit_breaker=circuit_breaker,
        idempotency_key=idempotency_key,
    )
    response = CoreMockRunResponse(
        request_id=request.state.request_id,
        result=executed.result,
        route_decision=decision.decision_json,
        attempted_providers=executed.attempted_providers,
        usage=executed.usage,
    ).model_dump(mode="json")
    complete_operation(
        session,
        start.record,
        response_json=response,
        billing_entry_id=executed.billing_entry_id,
    )
    publish_telemetry_outbox(
        session,
        event_type="request.completed",
        tenant_id=tenant.id,
        payload={"request_id": request.state.request_id, "operation": body.operation},
    )
    session.commit()
    return response


@app.post("/v1/core/mock:submit")
def submit_mock(
    body: CoreMockRunRequest,
    request: Request,
    tenant: Annotated[Tenant, Depends(current_tenant)],
    session: Annotated[Session, Depends(get_session)],
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, str]:
    idempotency_key = idempotency_key_header or body.idempotency_key
    check_rate_limit(
        kv,
        tenant_id=tenant.id,
        limit_per_minute=settings.default_rate_limit_per_minute,
    )
    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation=body.operation,
        strategy=body.strategy,
        provider_set_name=body.provider_set,
        circuit_breaker=circuit_breaker,
        explicit_provider=body.provider,
        request_id=request.state.request_id,
        constraints=body.features,
    )
    job = submit_job(
        session,
        tenant_id=tenant.id,
        operation=body.operation,
        request_id=request.state.request_id,
        decision=decision,
        payload=body.payload,
        idempotency_key=idempotency_key,
    )
    publish_telemetry_outbox(
        session,
        event_type="job.queued",
        tenant_id=tenant.id,
        payload={
            "request_id": request.state.request_id,
            "job_id": job.id,
            "operation": body.operation,
        },
    )
    session.commit()
    return {"job_id": job.id, "status": job.status, "request_id": request.state.request_id}


@app.post("/v1/documents:process")
async def process_document(
    request: Request,
    tenant: Annotated[Tenant, Depends(current_tenant)],
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    metadata: Annotated[str, Form()] = "{}",
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    document_request = _parse_document_metadata(metadata)
    idempotency_key = idempotency_key_header or document_request.idempotency_key
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency key is required")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Document file is empty")
    filename = file.filename or "document"
    mime_type = file.content_type or "application/octet-stream"
    file_sha256 = sha256_bytes(data)
    start = begin_operation(
        session,
        tenant_id=tenant.id,
        operation=DOCUMENT_OPERATION,
        idempotency_key=idempotency_key,
        payload_hash=_document_payload_hash(
            document_request=document_request,
            file_sha256=file_sha256,
            filename=filename,
            mime_type=mime_type,
            byte_size=len(data),
        ),
    )
    if start.replay_response is not None:
        replay_response = dict(start.replay_response)
        replay_response["replayed"] = True
        return replay_response

    check_rate_limit(
        kv,
        tenant_id=tenant.id,
        limit_per_minute=settings.default_rate_limit_per_minute,
    )
    check_tenant_quota(session, tenant)
    check_platform_spend(session, platform_limit_units=settings.platform_spend_limit_units)
    object_store = get_object_store()
    document_row = store_document_object(
        session,
        tenant_id=tenant.id,
        filename=filename,
        mime_type=mime_type,
        data=data,
        object_store=object_store,
    )
    decision = create_document_routing_decision(
        session,
        tenant_id=tenant.id,
        request=document_request,
        request_id=request.state.request_id,
        mime_type=mime_type,
        byte_size=len(data),
        circuit_breaker=circuit_breaker,
        sync=True,
    )
    result = execute_document_routing_decision(
        session,
        tenant_id=tenant.id,
        document=DocumentFile(
            document_id=document_row.id,
            filename=filename,
            mime_type=mime_type,
            data=data,
            sha256=file_sha256,
        ),
        request=document_request,
        decision=decision,
        object_store=object_store,
        circuit_breaker=circuit_breaker,
        idempotency_key=idempotency_key,
    )
    response = result.response.model_dump(mode="json")
    complete_operation(
        session,
        start.record,
        response_json=response,
        billing_entry_id=result.billing_entry_id,
    )
    publish_telemetry_outbox(
        session,
        event_type="document.completed",
        tenant_id=tenant.id,
        payload={
            "request_id": request.state.request_id,
            "document_id": document_row.id,
            "provider_id": result.response.provider_id,
            "service_id": result.response.service_id,
            "byte_size": len(data),
        },
    )
    session.commit()
    return response


@app.post("/v1/documents:submit")
async def submit_document(
    request: Request,
    tenant: Annotated[Tenant, Depends(current_tenant)],
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    metadata: Annotated[str, Form()] = "{}",
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    document_request = _parse_document_metadata(metadata)
    idempotency_key = idempotency_key_header or document_request.idempotency_key
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency key is required")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Document file is empty")
    filename = file.filename or "document"
    mime_type = file.content_type or "application/octet-stream"
    file_sha256 = sha256_bytes(data)
    start = begin_operation(
        session,
        tenant_id=tenant.id,
        operation=DOCUMENT_OPERATION,
        idempotency_key=idempotency_key,
        payload_hash=_document_payload_hash(
            document_request=document_request,
            file_sha256=file_sha256,
            filename=filename,
            mime_type=mime_type,
            byte_size=len(data),
        ),
    )
    if start.replay_response is not None:
        replay_response = dict(start.replay_response)
        replay_response["replayed"] = True
        return replay_response

    check_rate_limit(
        kv,
        tenant_id=tenant.id,
        limit_per_minute=settings.default_rate_limit_per_minute,
    )
    object_store = get_object_store()
    document_row = store_document_object(
        session,
        tenant_id=tenant.id,
        filename=filename,
        mime_type=mime_type,
        data=data,
        object_store=object_store,
    )
    decision = create_document_routing_decision(
        session,
        tenant_id=tenant.id,
        request=document_request,
        request_id=request.state.request_id,
        mime_type=mime_type,
        byte_size=len(data),
        circuit_breaker=circuit_breaker,
        sync=False,
    )
    job = submit_job(
        session,
        tenant_id=tenant.id,
        operation=DOCUMENT_OPERATION,
        request_id=request.state.request_id,
        decision=decision,
        payload={
            "request": document_request.model_dump(mode="json"),
            "document_id": document_row.id,
        },
        idempotency_key=idempotency_key,
    )
    response: dict[str, Any] = {
        "job_id": job.id,
        "status": job.status,
        "request_id": request.state.request_id,
        "document_id": document_row.id,
        "replayed": False,
    }
    complete_operation(
        session,
        start.record,
        response_json=response,
        billing_entry_id=None,
    )
    publish_telemetry_outbox(
        session,
        event_type="document.job_queued",
        tenant_id=tenant.id,
        payload={
            "request_id": request.state.request_id,
            "job_id": job.id,
            "document_id": document_row.id,
            "byte_size": len(data),
        },
    )
    session.commit()
    return response


@app.post("/v1/documents/invoices:extract")
async def extract_invoice(
    request: Request,
    tenant: Annotated[Tenant, Depends(current_tenant)],
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    metadata: Annotated[str, Form()] = "{}",
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    invoice_request = _parse_invoice_metadata(metadata)
    idempotency_key = idempotency_key_header or invoice_request.idempotency_key
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency key is required")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Document file is empty")
    filename = file.filename or "document"
    mime_type = file.content_type or "application/octet-stream"
    file_sha256 = sha256_bytes(data)
    start = begin_operation(
        session,
        tenant_id=tenant.id,
        operation=INVOICE_OPERATION,
        idempotency_key=idempotency_key,
        payload_hash=_invoice_payload_hash(
            invoice_request=invoice_request,
            file_sha256=file_sha256,
            filename=filename,
            mime_type=mime_type,
            byte_size=len(data),
        ),
    )
    if start.replay_response is not None:
        replay_response = dict(start.replay_response)
        replay_response["replayed"] = True
        return replay_response

    check_rate_limit(
        kv,
        tenant_id=tenant.id,
        limit_per_minute=settings.default_rate_limit_per_minute,
    )
    check_tenant_quota(session, tenant)
    check_platform_spend(session, platform_limit_units=settings.platform_spend_limit_units)
    object_store = get_object_store()
    document_row = store_document_object(
        session,
        tenant_id=tenant.id,
        filename=filename,
        mime_type=mime_type,
        data=data,
        object_store=object_store,
    )
    decision = create_invoice_routing_decision(
        session,
        tenant_id=tenant.id,
        request=invoice_request,
        request_id=request.state.request_id,
        mime_type=mime_type,
        byte_size=len(data),
        circuit_breaker=circuit_breaker,
        sync=True,
    )
    result = execute_invoice_workflow_decision(
        session,
        tenant_id=tenant.id,
        document=DocumentFile(
            document_id=document_row.id,
            filename=filename,
            mime_type=mime_type,
            data=data,
            sha256=file_sha256,
        ),
        request=invoice_request,
        decision=decision,
        object_store=object_store,
        circuit_breaker=circuit_breaker,
        idempotency_key=idempotency_key,
    )
    response = result.response.model_dump(mode="json")
    complete_operation(
        session,
        start.record,
        response_json=response,
        billing_entry_id=result.billing_entry_id,
    )
    publish_telemetry_outbox(
        session,
        event_type="workflow.completed",
        tenant_id=tenant.id,
        payload={
            "request_id": request.state.request_id,
            "document_id": document_row.id,
            "workflow": result.response.workflow,
            "provider_id": result.response.provider_id,
            "service_id": result.response.service_id,
            "accepted": result.response.quality.accepted,
            "byte_size": len(data),
        },
    )
    session.commit()
    return response


@app.post("/v1/documents/invoices:submit")
async def submit_invoice(
    request: Request,
    tenant: Annotated[Tenant, Depends(current_tenant)],
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    metadata: Annotated[str, Form()] = "{}",
    idempotency_key_header: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    invoice_request = _parse_invoice_metadata(metadata)
    idempotency_key = idempotency_key_header or invoice_request.idempotency_key
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="Idempotency key is required")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Document file is empty")
    filename = file.filename or "document"
    mime_type = file.content_type or "application/octet-stream"
    file_sha256 = sha256_bytes(data)
    start = begin_operation(
        session,
        tenant_id=tenant.id,
        operation=INVOICE_OPERATION,
        idempotency_key=idempotency_key,
        payload_hash=_invoice_payload_hash(
            invoice_request=invoice_request,
            file_sha256=file_sha256,
            filename=filename,
            mime_type=mime_type,
            byte_size=len(data),
        ),
    )
    if start.replay_response is not None:
        replay_response = dict(start.replay_response)
        replay_response["replayed"] = True
        return replay_response

    check_rate_limit(
        kv,
        tenant_id=tenant.id,
        limit_per_minute=settings.default_rate_limit_per_minute,
    )
    check_tenant_quota(session, tenant)
    check_platform_spend(session, platform_limit_units=settings.platform_spend_limit_units)
    object_store = get_object_store()
    document_row = store_document_object(
        session,
        tenant_id=tenant.id,
        filename=filename,
        mime_type=mime_type,
        data=data,
        object_store=object_store,
    )
    decision = create_invoice_routing_decision(
        session,
        tenant_id=tenant.id,
        request=invoice_request,
        request_id=request.state.request_id,
        mime_type=mime_type,
        byte_size=len(data),
        circuit_breaker=circuit_breaker,
        sync=False,
    )
    job = submit_job(
        session,
        tenant_id=tenant.id,
        operation=INVOICE_OPERATION,
        request_id=request.state.request_id,
        decision=decision,
        payload={
            "request": invoice_request.model_dump(mode="json"),
            "document_id": document_row.id,
        },
        idempotency_key=idempotency_key,
    )
    response: dict[str, Any] = {
        "job_id": job.id,
        "status": job.status,
        "request_id": request.state.request_id,
        "document_id": document_row.id,
        "workflow": "invoice.extract",
        "replayed": False,
    }
    complete_operation(
        session,
        start.record,
        response_json=response,
        billing_entry_id=None,
    )
    publish_telemetry_outbox(
        session,
        event_type="workflow.job_queued",
        tenant_id=tenant.id,
        payload={
            "request_id": request.state.request_id,
            "job_id": job.id,
            "document_id": document_row.id,
            "workflow": "invoice.extract",
            "byte_size": len(data),
        },
    )
    session.commit()
    return response


@app.get("/v1/jobs/{job_id}")
def get_job(
    job_id: str,
    tenant: Annotated[Tenant, Depends(current_tenant)],
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    job = session.get(Job, job_id)
    if job is None or job.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobView(
        job_id=job.id,
        status=JobStatus(job.status),
        request_id=job.request_id,
        result=job.result_json,
    ).model_dump(mode="json")
