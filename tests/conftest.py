from __future__ import annotations

from collections.abc import Generator

import pytest
from puzzle_gateway.models import Base, Tenant
from puzzle_gateway.seed import (
    create_api_key,
    create_default_provider_set,
    create_tenant,
    upsert_mock_provider,
)
from puzzle_shared import MockProviderBehavior
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        yield db
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded_tenant(session: Session) -> tuple[Tenant, str]:
    tenant = create_tenant(session, name="tenant-a")
    _key, api_key = create_api_key(session, tenant_id=tenant.id)
    create_default_provider_set(session, tenant_id=tenant.id)
    upsert_mock_provider(
        session,
        tenant_id=tenant.id,
        provider="mock-primary",
        behavior=MockProviderBehavior.SUCCESS,
        quality_score=95,
        cost_units=20,
        latency_ms=100,
    )
    upsert_mock_provider(
        session,
        tenant_id=tenant.id,
        provider="mock-secondary",
        behavior=MockProviderBehavior.SUCCESS,
        quality_score=80,
        cost_units=8,
        latency_ms=150,
    )
    session.commit()
    return tenant, api_key
