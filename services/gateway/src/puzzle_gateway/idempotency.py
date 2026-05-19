from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.errors import IdempotencyConflictError
from puzzle_gateway.models import IdempotencyRecord, now_utc


def request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class IdempotencyStart:
    record: IdempotencyRecord
    replay_response: dict[str, Any] | None = None


def begin_operation(
    session: Session,
    *,
    tenant_id: str,
    operation: str,
    idempotency_key: str,
    payload_hash: str,
) -> IdempotencyStart:
    existing = session.scalars(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
    ).first()
    if existing is not None:
        if existing.request_hash != payload_hash:
            raise IdempotencyConflictError("Idempotency key was reused with a different request")
        if existing.status == "completed" and existing.response_json is not None:
            return IdempotencyStart(record=existing, replay_response=existing.response_json)
        raise IdempotencyConflictError("Idempotency key is already processing")

    record = IdempotencyRecord(
        tenant_id=tenant_id,
        operation=operation,
        idempotency_key=idempotency_key,
        request_hash=payload_hash,
        status="processing",
    )
    session.add(record)
    session.flush()
    return IdempotencyStart(record=record)


def complete_operation(
    session: Session,
    record: IdempotencyRecord,
    *,
    response_json: dict[str, Any],
    billing_entry_id: str | None,
    result_ref: str | None = None,
) -> None:
    record.status = "completed"
    record.response_json = response_json
    record.result_ref = result_ref
    record.billing_entry_id = billing_entry_id
    record.completed_at = now_utc()
    session.add(record)
    session.flush()
