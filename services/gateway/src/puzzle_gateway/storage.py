from __future__ import annotations

import hashlib
from uuid import uuid4
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote, unquote, urlparse

from puzzle_gateway.config import settings
from puzzle_gateway.errors import ValidationError


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
        path = self.root / tenant_id / f"{uuid4().hex}-{digest}-{safe_name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get_bytes(self, ref: str) -> bytes:
        return Path(ref).read_bytes()

    def delete(self, ref: str) -> None:
        Path(ref).unlink(missing_ok=True)


class GCSObjectStore:
    def __init__(
        self,
        *,
        documents_bucket: str,
        artifacts_bucket: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.documents_bucket = documents_bucket
        self.artifacts_bucket = artifacts_bucket or documents_bucket
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            storage = import_module("google.cloud.storage")
            self._client = storage.Client()
        return self._client

    def put_bytes(self, *, tenant_id: str, name: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        safe_name = _safe_object_name(name)
        bucket_name = self._bucket_for_name(safe_name)
        key = f"tenants/{tenant_id}/{uuid4().hex}-{digest}-{safe_name}"
        blob = self.client.bucket(bucket_name).blob(key)
        blob.upload_from_string(data)
        return f"gs://{bucket_name}/{quote(key)}"

    def get_bytes(self, ref: str) -> bytes:
        bucket_name, key = _parse_gcs_ref(ref)
        return cast(bytes, self.client.bucket(bucket_name).blob(key).download_as_bytes())

    def delete(self, ref: str) -> None:
        bucket_name, key = _parse_gcs_ref(ref)
        try:
            self.client.bucket(bucket_name).blob(key).delete()
        except Exception as exc:  # noqa: BLE001 - optional google client type at runtime
            if exc.__class__.__name__ != "NotFound":
                raise

    def _bucket_for_name(self, name: str) -> str:
        if name.startswith(("raw/", "validation/")):
            return self.artifacts_bucket
        return self.documents_bucket


def _safe_object_name(name: str) -> str:
    parts = [
        part.strip().replace("\\", "_").replace("/", "_")
        for part in name.replace("\\", "/").split("/")
        if part.strip()
    ]
    return "/".join(parts) or "object"


def _parse_gcs_ref(ref: str) -> tuple[str, str]:
    parsed = urlparse(ref)
    if parsed.scheme != "gs" or not parsed.netloc or not parsed.path:
        raise ValidationError("Invalid GCS object reference")
    return parsed.netloc, unquote(parsed.path.lstrip("/"))


def get_object_store() -> ObjectStore:
    if settings.object_store_backend == "gcs":
        if not settings.documents_bucket:
            raise ValidationError("PUZZLE_DOCUMENTS_BUCKET is required for GCS object storage")
        return GCSObjectStore(
            documents_bucket=settings.documents_bucket,
            artifacts_bucket=settings.artifacts_bucket,
        )
    return LocalObjectStore(settings.object_store_root)
