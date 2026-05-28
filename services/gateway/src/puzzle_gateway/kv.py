from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

import redis
from redis.exceptions import RedisError

from puzzle_gateway.config import settings
from puzzle_gateway.errors import DependencyUnavailableError


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


class RedisKVStore:
    def __init__(self, url: str) -> None:
        self._client = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> str | None:
        try:
            value = self._client.get(key)
        except RedisError as exc:
            raise DependencyUnavailableError("Redis is unavailable") from exc
        return str(value) if value is not None else None

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        try:
            self._client.set(key, value, ex=ex)
        except RedisError as exc:
            raise DependencyUnavailableError("Redis is unavailable") from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete(key)
        except RedisError as exc:
            raise DependencyUnavailableError("Redis is unavailable") from exc

    def incr(self, key: str, *, ex: int | None = None) -> int:
        try:
            value = int(cast(Any, self._client.incr(key)))
            if ex is not None:
                self._client.expire(key, ex)
        except RedisError as exc:
            raise DependencyUnavailableError("Redis is unavailable") from exc
        return value


def get_kv_store() -> KVStore:
    if settings.redis_url:
        return RedisKVStore(settings.redis_url)
    return InMemoryKVStore()
