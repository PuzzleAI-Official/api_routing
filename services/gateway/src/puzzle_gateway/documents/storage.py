from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from puzzle_gateway.models import DocumentObject
from puzzle_gateway.storage import ObjectStore


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_document_object(
    session: Session,
    *,
    tenant_id: str,
    filename: str,
    mime_type: str,
    data: bytes,
    object_store: ObjectStore,
) -> DocumentObject:
    digest = sha256_bytes(data)
    object_key = object_store.put_bytes(
        tenant_id=tenant_id,
        name=f"documents/{filename}",
        data=data,
    )
    row = DocumentObject(
        tenant_id=tenant_id,
        filename=filename,
        mime_type=mime_type,
        byte_size=len(data),
        sha256=digest,
        object_key=object_key,
    )
    session.add(row)
    session.flush()
    return row
