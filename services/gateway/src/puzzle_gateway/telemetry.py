from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.models import TelemetryEvent, TelemetryOutboxEvent, now_utc


class TelemetryPublisher(Protocol):
    def publish(self, event_type: str, tenant_id: str, payload: dict[str, Any]) -> None: ...


@dataclass
class InMemoryTelemetryQueue:
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, event_type: str, tenant_id: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, tenant_id, payload))

    def drain(self) -> list[tuple[str, str, dict[str, Any]]]:
        events = list(self.events)
        self.events.clear()
        return events


def ingest_events(session: Session, queue: InMemoryTelemetryQueue) -> int:
    count = 0
    for event_type, tenant_id, payload in queue.drain():
        session.add(
            TelemetryEvent(
                tenant_id=tenant_id,
                event_type=event_type,
                payload_json=payload,
            )
        )
        count += 1
    session.flush()
    return count


def publish_telemetry_outbox(
    session: Session,
    *,
    event_type: str,
    tenant_id: str,
    payload: dict[str, Any],
) -> TelemetryOutboxEvent:
    row = TelemetryOutboxEvent(
        tenant_id=tenant_id,
        event_type=event_type,
        payload_json=payload,
        status="pending",
    )
    session.add(row)
    session.flush()
    return row


def drain_telemetry_outbox(session: Session, *, limit: int = 100) -> int:
    rows = list(
        session.scalars(
            select(TelemetryOutboxEvent)
            .where(TelemetryOutboxEvent.status == "pending")
            .order_by(TelemetryOutboxEvent.created_at)
            .limit(limit)
        )
    )
    for row in rows:
        session.add(
            TelemetryEvent(
                tenant_id=row.tenant_id,
                event_type=row.event_type,
                payload_json=row.payload_json,
            )
        )
        row.status = "processed"
        row.processed_at = now_utc()
        session.add(row)
    session.flush()
    return len(rows)
