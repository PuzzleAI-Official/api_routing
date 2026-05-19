from __future__ import annotations

import time

from puzzle_gateway.db import SessionLocal, create_all
from puzzle_gateway.telemetry import InMemoryTelemetryQueue, ingest_events


def run_once(queue: InMemoryTelemetryQueue) -> int:
    with SessionLocal() as session:
        count = ingest_events(session, queue)
        session.commit()
        return count


def main() -> None:
    create_all()
    queue = InMemoryTelemetryQueue()
    while True:
        run_once(queue)
        time.sleep(1)


if __name__ == "__main__":
    main()
