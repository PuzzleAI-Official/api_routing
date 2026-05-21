from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from puzzle_shared import (
    ApiError,
    ApiErrorCode,
    DocumentProcessRequest,
    InvoiceExtractRequest,
    JobStatus,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.documents.execution import (
    document_file_from_object,
    execute_document_routing_decision,
)
from puzzle_gateway.documents.manifests import DOCUMENT_OPERATION
from puzzle_gateway.documents.workflows import (
    INVOICE_OPERATION,
    execute_invoice_workflow_decision,
)
from puzzle_gateway.models import Job, RoutingDecision, now_utc
from puzzle_gateway.providers import execute_routing_decision
from puzzle_gateway.storage import get_object_store


class JobPublisher(Protocol):
    def publish_job(self, job_id: str) -> None: ...


@dataclass
class InMemoryJobQueue:
    job_ids: list[str] = field(default_factory=list)

    def publish_job(self, job_id: str) -> None:
        self.job_ids.append(job_id)

    def pop(self) -> str | None:
        if not self.job_ids:
            return None
        return self.job_ids.pop(0)


def submit_job(
    session: Session,
    *,
    tenant_id: str,
    operation: str,
    request_id: str,
    decision: RoutingDecision,
    payload: dict[str, Any],
    publisher: JobPublisher | None = None,
    idempotency_key: str | None = None,
) -> Job:
    job = Job(
        tenant_id=tenant_id,
        operation=operation,
        request_id=request_id,
        routing_decision_id=decision.id,
        payload_json=payload,
        idempotency_key=idempotency_key,
        status=JobStatus.QUEUED.value,
    )
    session.add(job)
    session.flush()
    if publisher is not None:
        publisher.publish_job(job.id)
    return job


def claim_next_queued_job(session: Session) -> Job | None:
    job = session.scalars(
        select(Job).where(Job.status == JobStatus.QUEUED.value).order_by(Job.created_at).limit(1)
    ).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING.value
    job.updated_at = now_utc()
    session.add(job)
    session.flush()
    return job


def process_job_once(
    session: Session,
    *,
    job_id: str,
    circuit_breaker: CircuitBreaker,
) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} does not exist")
    if job.status == JobStatus.SUCCEEDED.value:
        return job
    if job.status == JobStatus.QUEUED.value:
        job.status = JobStatus.RUNNING.value
        job.updated_at = now_utc()
        session.add(job)
        session.flush()

    decision = session.get(RoutingDecision, job.routing_decision_id)
    if decision is None:
        job.status = JobStatus.FAILED.value
        job.error_json = ApiError(
            code=ApiErrorCode.INTERNAL_ERROR,
            message="Routing decision is missing",
            request_id=job.request_id,
        ).model_dump(mode="json")
        session.add(job)
        session.flush()
        return job

    try:
        if job.operation == DOCUMENT_OPERATION:
            document_request = DocumentProcessRequest.model_validate(job.payload_json["request"])
            document = document_file_from_object(
                session,
                document_id=str(job.payload_json["document_id"]),
                object_store=get_object_store(),
            )
            document_result = execute_document_routing_decision(
                session,
                tenant_id=job.tenant_id,
                document=document,
                request=document_request,
                decision=decision,
                object_store=get_object_store(),
                circuit_breaker=circuit_breaker,
                idempotency_key=job.idempotency_key,
                job_id=job.id,
            )
            job.status = JobStatus.SUCCEEDED.value
            job.result_json = document_result.response.model_dump(mode="json")
            job.updated_at = now_utc()
            session.add(job)
            session.flush()
            return job
        if job.operation == INVOICE_OPERATION:
            invoice_request = InvoiceExtractRequest.model_validate(job.payload_json["request"])
            object_store = get_object_store()
            document = document_file_from_object(
                session,
                document_id=str(job.payload_json["document_id"]),
                object_store=object_store,
            )
            invoice_result = execute_invoice_workflow_decision(
                session,
                tenant_id=job.tenant_id,
                document=document,
                request=invoice_request,
                decision=decision,
                object_store=object_store,
                circuit_breaker=circuit_breaker,
                idempotency_key=job.idempotency_key,
                job_id=job.id,
            )
            job.status = JobStatus.SUCCEEDED.value
            job.result_json = invoice_result.response.model_dump(mode="json")
            job.updated_at = now_utc()
            session.add(job)
            session.flush()
            return job
        result = execute_routing_decision(
            session,
            tenant_id=job.tenant_id,
            operation=job.operation,
            payload=job.payload_json,
            decision=decision,
            circuit_breaker=circuit_breaker,
            idempotency_key=job.idempotency_key,
            job_id=job.id,
        )
    except Exception as exc:  # noqa: BLE001 - persisted job failure boundary
        job.status = JobStatus.FAILED.value
        job.error_json = ApiError(
            code=ApiErrorCode.PROVIDER_UNAVAILABLE,
            message=str(exc),
            request_id=job.request_id,
        ).model_dump(mode="json")
    else:
        job.status = JobStatus.SUCCEEDED.value
        job.result_json = {
            "result": result.result,
            "attempted_providers": result.attempted_providers,
            "usage": result.usage.model_dump(mode="json"),
        }
    job.updated_at = now_utc()
    session.add(job)
    session.flush()
    return job
