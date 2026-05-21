from __future__ import annotations

import time

from puzzle_gateway.db import SessionLocal, create_all
from puzzle_gateway.telemetry import drain_telemetry_outbox


def run_once() -> int:
    with SessionLocal() as session:
        count = drain_telemetry_outbox(session)
        session.commit()
        return count


def main() -> None:
    create_all()
    while True:
        run_once()
        time.sleep(1)


if __name__ == "__main__":
    main()
