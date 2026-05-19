from __future__ import annotations

from puzzle_shared import ProviderSetSpec
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.errors import ValidationError
from puzzle_gateway.models import ProviderSet


def get_provider_set(session: Session, *, tenant_id: str, name: str) -> ProviderSetSpec:
    row = session.scalars(
        select(ProviderSet).where(ProviderSet.tenant_id == tenant_id, ProviderSet.name == name)
    ).first()
    if row is None:
        raise ValidationError(f"ProviderSet '{name}' does not exist")
    return ProviderSetSpec.model_validate(row.spec_json)


def upsert_provider_set(session: Session, *, tenant_id: str, spec: ProviderSetSpec) -> ProviderSet:
    row = session.scalars(
        select(ProviderSet).where(ProviderSet.tenant_id == tenant_id, ProviderSet.name == spec.name)
    ).first()
    if row is None:
        row = ProviderSet(
            tenant_id=tenant_id,
            name=spec.name,
            spec_json=spec.model_dump(mode="json"),
        )
    else:
        row.spec_json = spec.model_dump(mode="json")
    session.add(row)
    session.flush()
    return row
