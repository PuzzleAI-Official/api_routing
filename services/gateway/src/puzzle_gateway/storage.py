from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from puzzle_gateway.config import settings


class ObjectStore(Protocol):
    def put_bytes(self, *, tenant_id: str, name: str, data: bytes) -> str: ...

    def get_bytes(self, ref: str) -> bytes: ...

    def delete(self, ref: str) -> None: ...


class LocalObjectStore:
    def __init__(self, root: Path | str = ".local/object-store") -> None:
        self.root = Path(root)

    def put_bytes(self, *, tenant_id: str, name: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        safe_name = name.replace("/", "_").replace("\\", "_")
        path = self.root / tenant_id / f"{digest}-{safe_name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get_bytes(self, ref: str) -> bytes:
        return Path(ref).read_bytes()

    def delete(self, ref: str) -> None:
        Path(ref).unlink(missing_ok=True)


def get_object_store() -> ObjectStore:
    return LocalObjectStore(settings.object_store_root)
