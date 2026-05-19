from __future__ import annotations

from puzzle_shared import CredentialMode, MockProviderBehavior, ProviderSetSpec
from puzzle_shared.schemas import ProviderSpec, ShadowPolicy
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.models import ApiKey, MockProviderConfig, Tenant
from puzzle_gateway.provider_sets import upsert_provider_set
from puzzle_gateway.security import api_key_prefix, create_salt, generate_api_key, hash_api_key


def create_tenant(session: Session, *, name: str, region: str = "us-central1") -> Tenant:
    tenant = Tenant(name=name, region=region)
    session.add(tenant)
    session.flush()
    return tenant


def create_api_key(session: Session, *, tenant_id: str) -> tuple[ApiKey, str]:
    raw_key = generate_api_key()
    salt = create_salt()
    row = ApiKey(
        tenant_id=tenant_id,
        key_prefix=api_key_prefix(raw_key),
        key_hash=hash_api_key(raw_key, salt),
        salt=salt,
    )
    session.add(row)
    session.flush()
    return row, raw_key


def create_default_provider_set(session: Session, *, tenant_id: str) -> None:
    spec = ProviderSetSpec(
        name="default",
        providers=[
            ProviderSpec(
                provider="mock-primary",
                credential_mode=CredentialMode.MANAGED,
                fallback_priority=1,
                capabilities=["core.mock"],
            ),
            ProviderSpec(
                provider="mock-secondary",
                credential_mode=CredentialMode.MANAGED,
                fallback_priority=2,
                capabilities=["core.mock"],
            ),
        ],
        shadow_policy=ShadowPolicy(enabled=False),
    )
    upsert_provider_set(session, tenant_id=tenant_id, spec=spec)


def upsert_mock_provider(
    session: Session,
    *,
    tenant_id: str,
    provider: str,
    behavior: MockProviderBehavior = MockProviderBehavior.SUCCESS,
    quality_score: int = 90,
    cost_units: int = 10,
    latency_ms: int = 100,
) -> MockProviderConfig:
    row = session.scalars(
        select(MockProviderConfig).where(
            MockProviderConfig.tenant_id == tenant_id,
            MockProviderConfig.provider == provider,
        )
    ).first()
    if row is None:
        row = MockProviderConfig(tenant_id=tenant_id, provider=provider)
    row.behavior = behavior.value
    row.quality_score = quality_score
    row.cost_units = cost_units
    row.latency_ms = latency_ms
    session.add(row)
    session.flush()
    return row


def seed_default_tenant(session: Session, *, name: str = "local-dev") -> tuple[Tenant, str]:
    tenant = create_tenant(session, name=name)
    _api_key, raw_key = create_api_key(session, tenant_id=tenant.id)
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
    return tenant, raw_key
