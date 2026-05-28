from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol

from puzzle_shared import (
    ApiError,
    ApiErrorCode,
    DocumentProcessRequest,
    InvoiceExtractRequest,
    JobStatus,
)
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.config import settings
from puzzle_gateway.documents.execution import (
    document_file_from_object,
    execute_document_routing_decision,
)
from puzzle_gateway.documents.manifests import DOCUMENT_OPERATION
from puzzle_gateway.documents.workflows import (
    INVOICE_OPERATION,
    execute_invoice_workflow_decision,
)
from puzzle_gateway.errors import DependencyUnavailableError
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
    now = now_utc()
    stale_before = now - timedelta(seconds=settings.job_stale_lock_seconds)
    expired_jobs = list(
        session.scalars(
            select(Job).where(
                Job.status == JobStatus.RUNNING.value,
                Job.locked_at.is_not(None),
                Job.locked_at <= stale_before,
                Job.attempt_count >= settings.job_max_attempts,
            )
        )
    )
    for expired_job in expired_jobs:
        _fail_job(
            expired_job,
            code=ApiErrorCode.INTERNAL_ERROR,
            message="Job exceeded maximum retry attempts",
        )
        session.add(expired_job)
    if expired_jobs:
        session.flush()

    eligible = or_(
        and_(
            Job.status == JobStatus.QUEUED.value,
            or_(Job.next_run_at.is_(None), Job.next_run_at <= now),
        ),
        and_(
            Job.status == JobStatus.RUNNING.value,
            Job.locked_at.is_not(None),
            Job.locked_at <= stale_before,
        ),
    )
    statement = (
        select(Job)
        .where(eligible, Job.attempt_count < settings.job_max_attempts)
        .order_by(Job.created_at)
        .limit(1)
    )
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        statement = statement.with_for_update(skip_locked=True)
    job = session.scalars(statement).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING.value
    job.attempt_count += 1
    job.locked_at = now
    job.next_run_at = None
    job.updated_at = now_utc()
    session.add(job)
    session.flush()
    return job


def _mark_running(session: Session, job: Job) -> None:
    if job.status == JobStatus.QUEUED.value:
        job.attempt_count += 1
        job.locked_at = now_utc()
    elif job.locked_at is None:
        job.locked_at = now_utc()
    job.status = JobStatus.RUNNING.value
    job.updated_at = now_utc()
    session.add(job)
    session.flush()


def _succeed_job(job: Job, result_json: dict[str, Any]) -> None:
    job.status = JobStatus.SUCCEEDED.value
    job.result_json = result_json
    job.error_json = None
    job.locked_at = None
    job.next_run_at = None
    job.updated_at = now_utc()


def _fail_job(
    job: Job,
    *,
    code: ApiErrorCode,
    message: str,
) -> None:
    error_json = ApiError(
        code=code,
        message=message,
        request_id=job.request_id,
    ).model_dump(mode="json")
    job.status = JobStatus.FAILED.value
    job.error_json = error_json
    job.last_error_json = error_json
    job.locked_at = None
    job.next_run_at = None
    job.updated_at = now_utc()


def _requeue_dependency_failure(job: Job, exc: DependencyUnavailableError) -> None:
    error_json = ApiError(
        code=ApiErrorCode.DEPENDENCY_UNAVAILABLE,
        message=exc.message,
        request_id=job.request_id,
    ).model_dump(mode="json")
    job.last_error_json = error_json
    job.locked_at = None
    job.updated_at = now_utc()
    if job.attempt_count >= settings.job_max_attempts:
        job.status = JobStatus.FAILED.value
        job.error_json = error_json
        job.next_run_at = None
        return
    job.status = JobStatus.QUEUED.value
    job.error_json = None
    job.next_run_at = now_utc() + timedelta(seconds=settings.job_retry_delay_seconds)


def process_job_once(
    session: Session,
    *,
    job_id: str,
    circuit_breaker: CircuitBreaker,
) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise ValueError(f"Job {job_id} does not exist")
    if job.status in {
        JobStatus.SUCCEEDED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    }:
        return job
    _mark_running(session, job)

    decision = session.get(RoutingDecision, job.routing_decision_id)
    if decision is None:
        _fail_job(
            job,
            code=ApiErrorCode.INTERNAL_ERROR,
            message="Routing decision is missing",
        )
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
            _succeed_job(job, document_result.response.model_dump(mode="json"))
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
            _succeed_job(job, invoice_result.response.model_dump(mode="json"))
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
    except DependencyUnavailableError as exc:
        _requeue_dependency_failure(job, exc)
    except Exception as exc:  # noqa: BLE001 - persisted job failure boundary
        _fail_job(
            job,
            code=ApiErrorCode.PROVIDER_UNAVAILABLE,
            message=str(exc),
        )
    else:
        _succeed_job(
            job,
            {
                "result": result.result,
                "attempted_providers": result.attempted_providers,
                "usage": result.usage.model_dump(mode="json"),
            },
        )
    session.add(job)
    session.flush()
    return job
