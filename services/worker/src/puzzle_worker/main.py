from __future__ import annotations

import time

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.config import settings
from puzzle_gateway.db import SessionLocal, create_all
from puzzle_gateway.jobs import InMemoryJobQueue, process_job_once
from puzzle_gateway.kv import InMemoryKVStore


def run_once(job_id: str) -> None:
    kv = InMemoryKVStore()
    breaker = CircuitBreaker(
        kv,
        failure_threshold=settings.circuit_failure_threshold,
        cooldown_seconds=settings.circuit_cooldown_seconds,
    )
    with SessionLocal() as session:
        process_job_once(session, job_id=job_id, circuit_breaker=breaker)
        session.commit()


def main() -> None:
    create_all()
    queue = InMemoryJobQueue()
    while True:
        job_id = queue.pop()
        if job_id is not None:
            run_once(job_id)
        time.sleep(1)


if __name__ == "__main__":
    main()
