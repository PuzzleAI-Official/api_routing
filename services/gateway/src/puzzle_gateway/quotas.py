from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from puzzle_gateway.errors import QuotaExceededError, RateLimitError
from puzzle_gateway.kv import KVStore
from puzzle_gateway.models import BillingLedgerEntry, Tenant


def check_rate_limit(
    kv: KVStore,
    *,
    tenant_id: str,
    limit_per_minute: int,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    bucket = now.strftime("%Y%m%d%H%M")
    key = f"rate:{tenant_id}:{bucket}"
    count = kv.incr(key, ex=90)
    if count > limit_per_minute:
        raise RateLimitError("Rate limit exceeded")


def check_tenant_quota(session: Session, tenant: Tenant) -> None:
    spent = session.scalar(
        select(func.coalesce(func.sum(BillingLedgerEntry.charge_units), 0)).where(
            BillingLedgerEntry.tenant_id == tenant.id
        )
    )
    if int(spent or 0) >= tenant.monthly_quota_units:
        raise QuotaExceededError("Tenant quota exceeded")


def check_platform_spend(session: Session, *, platform_limit_units: int) -> None:
    spent = session.scalar(select(func.coalesce(func.sum(BillingLedgerEntry.cost_units), 0)))
    if int(spent or 0) >= platform_limit_units:
        raise QuotaExceededError("Platform spend circuit breaker is open")
