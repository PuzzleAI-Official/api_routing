from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from puzzle_gateway.models import (
    AuditLogEntry,
    BillingLedgerEntry,
    DataDeletionRequest,
    DocumentObject,
    DocumentResult,
    DocumentWorkflowResult,
    IdempotencyRecord,
    Job,
    ProviderAttempt,
    ProviderExternalJob,
    RoutingDecision,
    TelemetryEvent,
    TelemetryOutboxEvent,
    now_utc,
)
from puzzle_gateway.storage import ObjectStore

DELETION_TARGET_TYPES = {"document", "request", "job"}


class DataDeletionTargetNotFoundError(Exception):
    pass


@dataclass
class DeletionScope:
    document_ids: set[str] = field(default_factory=set)
    request_ids: set[str] = field(default_factory=set)
    job_ids: set[str] = field(default_factory=set)

    @property
    def values(self) -> set[str]:
        return self.document_ids | self.request_ids | self.job_ids


def _payload_contains(value: Any, targets: set[str]) -> bool:
    if not targets:
        return False
    if isinstance(value, str):
        return value in targets
    if isinstance(value, dict):
        return any(_payload_contains(item, targets) for item in value.values())
    if isinstance(value, list):
        return any(_payload_contains(item, targets) for item in value)
    return False


def _redacted_payload(deletion_request_id: str) -> dict[str, Any]:
    return {"redacted": True, "deletion_request_id": deletion_request_id}


def _redacted_ref(deletion_request_id: str, row_id: str) -> str:
    return f"deleted:{deletion_request_id}:{row_id}"


def _delete_object_ref(
    object_store: ObjectStore,
    ref: str | None,
    summary: dict[str, int],
) -> None:
    if not ref or ref.startswith("deleted:"):
        return
    object_store.delete(ref)
    summary["object_refs_deleted"] += 1


def _tenant_jobs(session: Session, tenant_id: str) -> list[Job]:
    return list(session.scalars(select(Job).where(Job.tenant_id == tenant_id)))


def _build_scope(  # noqa: PLR0912
    session: Session,
    *,
    tenant_id: str,
    target_type: str,
    target_id: str,
) -> DeletionScope:
    if target_type not in DELETION_TARGET_TYPES:
        raise DataDeletionTargetNotFoundError(f"Unsupported deletion target type: {target_type}")

    scope = DeletionScope()
    if target_type == "document":
        document = session.get(DocumentObject, target_id)
        if document is None or document.tenant_id != tenant_id:
            raise DataDeletionTargetNotFoundError("Deletion target not found")
        scope.document_ids.add(document.id)
    elif target_type == "request":
        scope.request_ids.add(target_id)
    elif target_type == "job":
        job = session.get(Job, target_id)
        if job is None or job.tenant_id != tenant_id:
            raise DataDeletionTargetNotFoundError("Deletion target not found")
        scope.job_ids.add(job.id)
        scope.request_ids.add(job.request_id)
        document_id = job.payload_json.get("document_id")
        if isinstance(document_id, str):
            scope.document_ids.add(document_id)

    for document_result in session.scalars(
        select(DocumentResult).where(DocumentResult.tenant_id == tenant_id)
    ):
        if (
            document_result.document_id in scope.document_ids
            or document_result.request_id in scope.request_ids
        ):
            scope.document_ids.add(document_result.document_id)
            scope.request_ids.add(document_result.request_id)
            if document_result.job_id:
                scope.job_ids.add(document_result.job_id)

    for workflow_result in session.scalars(
        select(DocumentWorkflowResult).where(DocumentWorkflowResult.tenant_id == tenant_id)
    ):
        if (
            workflow_result.document_id in scope.document_ids
            or workflow_result.request_id in scope.request_ids
        ):
            scope.document_ids.add(workflow_result.document_id)
            scope.request_ids.add(workflow_result.request_id)
            if workflow_result.job_id:
                scope.job_ids.add(workflow_result.job_id)

    for job in _tenant_jobs(session, tenant_id):
        if (
            job.id in scope.job_ids
            or job.request_id in scope.request_ids
            or _payload_contains(job.payload_json, scope.document_ids)
        ):
            scope.job_ids.add(job.id)
            scope.request_ids.add(job.request_id)
            document_id = job.payload_json.get("document_id")
            if isinstance(document_id, str):
                scope.document_ids.add(document_id)

    if target_type == "request":
        exists = bool(scope.document_ids or scope.job_ids)
        exists = exists or session.scalars(
            select(RoutingDecision).where(
                RoutingDecision.tenant_id == tenant_id,
                RoutingDecision.request_id == target_id,
            )
        ).first() is not None
        exists = exists or session.scalars(
            select(ProviderAttempt).where(
                ProviderAttempt.tenant_id == tenant_id,
                ProviderAttempt.request_id == target_id,
            )
        ).first() is not None
        exists = exists or session.scalars(
            select(BillingLedgerEntry).where(
                BillingLedgerEntry.tenant_id == tenant_id,
                BillingLedgerEntry.request_id == target_id,
            )
        ).first() is not None
        exists = exists or any(
            _payload_contains(idempotency.response_json, {target_id})
            for idempotency in session.scalars(
                select(IdempotencyRecord).where(IdempotencyRecord.tenant_id == tenant_id)
            )
        )
        if not exists:
            raise DataDeletionTargetNotFoundError("Deletion target not found")

    return scope


def perform_data_deletion(  # noqa: PLR0912, PLR0915
    session: Session,
    *,
    tenant_id: str,
    target_type: str,
    target_id: str,
    requested_by: str,
    reason: str | None,
    object_store: ObjectStore,
) -> DataDeletionRequest:
    scope = _build_scope(
        session,
        tenant_id=tenant_id,
        target_type=target_type,
        target_id=target_id,
    )
    request = DataDeletionRequest(
        tenant_id=tenant_id,
        target_type=target_type,
        target_id=target_id,
        status="pending",
        requested_by=requested_by,
        reason=reason,
        summary_json={},
    )
    session.add(request)
    session.flush()

    summary = {
        "object_refs_deleted": 0,
        "rows_redacted": 0,
        "billing_rows_preserved": 0,
    }
    redacted = _redacted_payload(request.id)
    targets = scope.values

    for document in session.scalars(
        select(DocumentObject).where(
            DocumentObject.tenant_id == tenant_id,
            DocumentObject.id.in_(scope.document_ids),
        )
    ):
        _delete_object_ref(object_store, document.object_key, summary)
        document.filename = "[deleted]"
        document.mime_type = "application/octet-stream"
        document.byte_size = 0
        document.sha256 = "0" * 64
        document.object_key = _redacted_ref(request.id, document.id)
        summary["rows_redacted"] += 1
        session.add(document)

    for document_result in session.scalars(
        select(DocumentResult).where(DocumentResult.tenant_id == tenant_id)
    ):
        if (
            document_result.document_id in scope.document_ids
            or document_result.request_id in scope.request_ids
            or (document_result.job_id is not None and document_result.job_id in scope.job_ids)
        ):
            _delete_object_ref(object_store, document_result.raw_result_ref, summary)
            document_result.normalized_result_json = redacted
            document_result.raw_result_ref = _redacted_ref(request.id, document_result.id)
            summary["rows_redacted"] += 1
            session.add(document_result)

    for workflow_result in session.scalars(
        select(DocumentWorkflowResult).where(DocumentWorkflowResult.tenant_id == tenant_id)
    ):
        if (
            workflow_result.document_id in scope.document_ids
            or workflow_result.request_id in scope.request_ids
            or (workflow_result.job_id is not None and workflow_result.job_id in scope.job_ids)
        ):
            _delete_object_ref(object_store, workflow_result.raw_result_ref, summary)
            workflow_result.result_json = redacted
            workflow_result.quality_json = redacted
            workflow_result.raw_result_ref = _redacted_ref(request.id, workflow_result.id)
            summary["rows_redacted"] += 1
            session.add(workflow_result)

    for job in _tenant_jobs(session, tenant_id):
        if job.id in scope.job_ids or job.request_id in scope.request_ids:
            job.payload_json = redacted
            job.result_json = redacted if job.result_json is not None else None
            job.error_json = redacted if job.error_json is not None else None
            job.last_error_json = redacted if job.last_error_json is not None else None
            summary["rows_redacted"] += 1
            session.add(job)

    for idempotency in session.scalars(
        select(IdempotencyRecord).where(IdempotencyRecord.tenant_id == tenant_id)
    ):
        if _payload_contains(idempotency.response_json, targets) or (
            idempotency.result_ref is not None and idempotency.result_ref in targets
        ):
            idempotency.response_json = redacted
            idempotency.result_ref = _redacted_ref(request.id, idempotency.id)
            summary["rows_redacted"] += 1
            session.add(idempotency)

    for telemetry_event in session.scalars(
        select(TelemetryEvent).where(TelemetryEvent.tenant_id == tenant_id)
    ):
        if _payload_contains(telemetry_event.payload_json, targets):
            telemetry_event.payload_json = redacted
            summary["rows_redacted"] += 1
            session.add(telemetry_event)

    for outbox_event in session.scalars(
        select(TelemetryOutboxEvent).where(TelemetryOutboxEvent.tenant_id == tenant_id)
    ):
        if _payload_contains(outbox_event.payload_json, targets):
            outbox_event.payload_json = redacted
            summary["rows_redacted"] += 1
            session.add(outbox_event)

    for external_job in session.scalars(
        select(ProviderExternalJob).where(ProviderExternalJob.tenant_id == tenant_id)
    ):
        if external_job.job_id in scope.job_ids:
            external_job.metadata_json = redacted
            summary["rows_redacted"] += 1
            session.add(external_job)

    summary["billing_rows_preserved"] = int(
        session.scalar(
            select(func.count())
            .select_from(BillingLedgerEntry)
            .where(
                BillingLedgerEntry.tenant_id == tenant_id,
                BillingLedgerEntry.request_id.in_(scope.request_ids or {""}),
            )
        )
        or 0
    )

    session.add(
        AuditLogEntry(
            tenant_id=tenant_id,
            actor=requested_by,
            action="data_deletion.completed",
            resource_type=target_type,
            resource_id=target_id,
            metadata_json={
                "deletion_request_id": request.id,
                "document_ids": sorted(scope.document_ids),
                "request_ids": sorted(scope.request_ids),
                "job_ids": sorted(scope.job_ids),
                "summary": summary,
            },
        )
    )

    request.status = "completed"
    request.summary_json = summary
    request.completed_at = now_utc()
    session.add(request)
    session.flush()
    return request
