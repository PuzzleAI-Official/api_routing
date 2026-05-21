from __future__ import annotations

from pathlib import Path

from puzzle_gateway.storage import LocalObjectStore


def test_local_object_store_put_get_delete(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)

    ref = store.put_bytes(tenant_id="tenant-a", name="sample/file.txt", data=b"hello")

    assert store.get_bytes(ref) == b"hello"
    store.delete(ref)
    assert list(tmp_path.joinpath("tenant-a").glob("*")) == []
