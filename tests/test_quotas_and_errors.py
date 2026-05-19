from __future__ import annotations

import pytest
from puzzle_gateway.errors import QuotaExceededError, RateLimitError
from puzzle_gateway.kv import InMemoryKVStore
from puzzle_gateway.models import BillingLedgerEntry, Tenant
from puzzle_gateway.quotas import check_platform_spend, check_rate_limit, check_tenant_quota
from sqlalchemy.orm import Session


def test_rate_limit_rejects_over_limit(seeded_tenant: tuple[Tenant, str]) -> None:
    tenant, _api_key = seeded_tenant
    kv = InMemoryKVStore()

    check_rate_limit(kv, tenant_id=tenant.id, limit_per_minute=1)

    with pytest.raises(RateLimitError):
        check_rate_limit(kv, tenant_id=tenant.id, limit_per_minute=1)


def test_tenant_quota_rejects_when_charge_units_exhausted(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    tenant.monthly_quota_units = 10
    session.add(
        BillingLedgerEntry(
            tenant_id=tenant.id,
            request_id="req-quota",
            operation="core.mock",
            provider="mock-primary",
            cost_units=5,
            charge_units=10,
        )
    )
    session.flush()

    with pytest.raises(QuotaExceededError):
        check_tenant_quota(session, tenant)


def test_platform_spend_breaker_rejects_when_cost_exhausted(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    session.add(
        BillingLedgerEntry(
            tenant_id=tenant.id,
            request_id="req-platform",
            operation="core.mock",
            provider="mock-primary",
            cost_units=5,
            charge_units=10,
        )
    )
    session.flush()

    with pytest.raises(QuotaExceededError):
        check_platform_spend(session, platform_limit_units=5)
