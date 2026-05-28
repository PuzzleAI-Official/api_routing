from __future__ import annotations

from pathlib import Path

import pytest
from puzzle_gateway.config import settings
from puzzle_gateway.errors import ValidationError
from puzzle_gateway.storage import GCSObjectStore, LocalObjectStore, get_object_store


class _FakeBlob:
    def __init__(self, objects: dict[str, bytes], name: str) -> None:
        self.objects = objects
        self.name = name

    def upload_from_string(self, data: bytes) -> None:
        self.objects[self.name] = data

    def download_as_bytes(self) -> bytes:
        return self.objects[self.name]

    def delete(self) -> None:
        self.objects.pop(self.name, None)


class _FakeBucket:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self.objects, name)


class _FakeGCSClient:
    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, bytes]] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self.buckets.setdefault(name, {}))


class NotFound(Exception):
    pass


class _MissingBlob:
    def delete(self) -> None:
        raise NotFound("missing")


class _MissingBucket:
    def blob(self, _name: str) -> _MissingBlob:
        return _MissingBlob()


class _MissingGCSClient:
    def bucket(self, _name: str) -> _MissingBucket:
        return _MissingBucket()


def test_local_object_store_put_get_delete(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)

    ref = store.put_bytes(tenant_id="tenant-a", name="sample/file.txt", data=b"hello")

    assert store.get_bytes(ref) == b"hello"
    store.delete(ref)
    assert list(tmp_path.joinpath("tenant-a").glob("*")) == []


def test_local_object_store_uses_unique_keys_for_same_content(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)

    first_ref = store.put_bytes(tenant_id="tenant-a", name="sample/file.txt", data=b"hello")
    second_ref = store.put_bytes(tenant_id="tenant-a", name="sample/file.txt", data=b"hello")

    assert first_ref != second_ref
    assert store.get_bytes(first_ref) == b"hello"
    assert store.get_bytes(second_ref) == b"hello"


def test_gcs_object_store_put_get_delete_routes_documents_and_artifacts() -> None:
    client = _FakeGCSClient()
    store = GCSObjectStore(
        documents_bucket="documents-bucket",
        artifacts_bucket="artifacts-bucket",
        client=client,
    )

    document_ref = store.put_bytes(
        tenant_id="tenant-a",
        name="documents/invoice.pdf",
        data=b"%PDF-1.4",
    )
    raw_ref = store.put_bytes(
        tenant_id="tenant-a",
        name="raw/provider.json",
        data=b'{"ok":true}',
    )

    assert document_ref.startswith("gs://documents-bucket/tenants/tenant-a/")
    assert raw_ref.startswith("gs://artifacts-bucket/tenants/tenant-a/")
    assert store.get_bytes(document_ref) == b"%PDF-1.4"
    assert store.get_bytes(raw_ref) == b'{"ok":true}'

    store.delete(document_ref)
    store.delete(raw_ref)

    assert client.buckets["documents-bucket"] == {}
    assert client.buckets["artifacts-bucket"] == {}


def test_gcs_object_store_uses_unique_keys_for_same_content() -> None:
    client = _FakeGCSClient()
    store = GCSObjectStore(documents_bucket="documents-bucket", client=client)

    first_ref = store.put_bytes(
        tenant_id="tenant-a",
        name="documents/invoice.pdf",
        data=b"%PDF-1.4",
    )
    second_ref = store.put_bytes(
        tenant_id="tenant-a",
        name="documents/invoice.pdf",
        data=b"%PDF-1.4",
    )

    assert first_ref != second_ref
    assert store.get_bytes(first_ref) == b"%PDF-1.4"
    assert store.get_bytes(second_ref) == b"%PDF-1.4"


def test_gcs_object_store_delete_ignores_missing_object() -> None:
    store = GCSObjectStore(documents_bucket="documents-bucket", client=_MissingGCSClient())

    store.delete("gs://documents-bucket/tenants/tenant-a/missing.pdf")


def test_gcs_object_store_rejects_invalid_ref() -> None:
    store = GCSObjectStore(documents_bucket="documents-bucket", client=_FakeGCSClient())

    with pytest.raises(ValidationError):
        store.get_bytes("not-a-gcs-ref")


def test_get_object_store_uses_gcs_when_configured() -> None:
    original_backend = settings.object_store_backend
    original_documents_bucket = settings.documents_bucket
    original_artifacts_bucket = settings.artifacts_bucket
    object.__setattr__(settings, "object_store_backend", "gcs")
    object.__setattr__(settings, "documents_bucket", "documents-bucket")
    object.__setattr__(settings, "artifacts_bucket", "artifacts-bucket")
    try:
        assert isinstance(get_object_store(), GCSObjectStore)
    finally:
        object.__setattr__(settings, "object_store_backend", original_backend)
        object.__setattr__(settings, "documents_bucket", original_documents_bucket)
        object.__setattr__(settings, "artifacts_bucket", original_artifacts_bucket)


def test_get_object_store_requires_documents_bucket_for_gcs() -> None:
    original_backend = settings.object_store_backend
    original_documents_bucket = settings.documents_bucket
    object.__setattr__(settings, "object_store_backend", "gcs")
    object.__setattr__(settings, "documents_bucket", None)
    try:
        with pytest.raises(ValidationError):
            get_object_store()
    finally:
        object.__setattr__(settings, "object_store_backend", original_backend)
        object.__setattr__(settings, "documents_bucket", original_documents_bucket)
