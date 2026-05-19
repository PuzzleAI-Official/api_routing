from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from puzzle_gateway.models import TelemetryEvent


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
