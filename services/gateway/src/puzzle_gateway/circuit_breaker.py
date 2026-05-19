from __future__ import annotations

import json
import time
from dataclasses import dataclass

from puzzle_gateway.kv import KVStore


@dataclass(frozen=True)
class CircuitState:
    state: str
    failures: int
    opened_at: float | None = None


class CircuitBreaker:
    def __init__(
        self,
        kv: KVStore,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: int = 60,
    ) -> None:
        self.kv = kv
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

    def _key(self, tenant_id: str, provider: str, operation: str) -> str:
        return f"circuit:{tenant_id}:{provider}:{operation}"

    def get_state(self, tenant_id: str, provider: str, operation: str) -> CircuitState:
        raw = self.kv.get(self._key(tenant_id, provider, operation))
        if raw is None:
            return CircuitState(state="closed", failures=0)
        payload = json.loads(raw)
        return CircuitState(
            state=payload["state"],
            failures=int(payload["failures"]),
            opened_at=payload.get("opened_at"),
        )

    def is_available(self, tenant_id: str, provider: str, operation: str) -> bool:
        state = self.get_state(tenant_id, provider, operation)
        if state.state != "open":
            return True
        if state.opened_at is None:
            return False
        return (time.time() - state.opened_at) >= self.cooldown_seconds

    def record_success(self, tenant_id: str, provider: str, operation: str) -> None:
        payload = {"state": "closed", "failures": 0, "opened_at": None}
        self.kv.set(self._key(tenant_id, provider, operation), json.dumps(payload))

    def record_failure(self, tenant_id: str, provider: str, operation: str) -> CircuitState:
        current = self.get_state(tenant_id, provider, operation)
        failures = current.failures + 1
        state = "open" if failures >= self.failure_threshold else "closed"
        opened_at = time.time() if state == "open" else None
        payload = {
            "state": state,
            "failures": failures,
            "opened_at": opened_at,
        }
        self.kv.set(self._key(tenant_id, provider, operation), json.dumps(payload))
        return CircuitState(
            state=state,
            failures=failures,
            opened_at=opened_at,
        )
