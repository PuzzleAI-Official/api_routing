from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class KVStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, *, ex: int | None = None) -> None: ...

    def delete(self, key: str) -> None: ...

    def incr(self, key: str, *, ex: int | None = None) -> int: ...


@dataclass
class _Entry:
    value: str
    expires_at: float | None


class InMemoryKVStore:
    def __init__(self) -> None:
        self._values: dict[str, _Entry] = {}

    def get(self, key: str) -> str | None:
        entry = self._values.get(key)
        if entry is None:
            return None
        if entry.expires_at is not None and entry.expires_at <= time.time():
            self._values.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        expires_at = time.time() + ex if ex is not None else None
        self._values[key] = _Entry(value=value, expires_at=expires_at)

    def delete(self, key: str) -> None:
        self._values.pop(key, None)

    def incr(self, key: str, *, ex: int | None = None) -> int:
        current = int(self.get(key) or "0") + 1
        self.set(key, str(current), ex=ex)
        return current

    def clear(self) -> None:
        self._values.clear()
