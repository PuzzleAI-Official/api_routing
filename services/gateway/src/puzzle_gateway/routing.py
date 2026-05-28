from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from puzzle_shared import ProviderSetSpec, RoutingDecisionView, RoutingStrategy
from puzzle_shared.schemas import ScoreComponent
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.errors import ProviderUnavailableError, ValidationError
from puzzle_gateway.models import MockProviderConfig, RoutingDecision
from puzzle_gateway.provider_sets import get_provider_set


@dataclass(frozen=True)
class Candidate:
    provider: str
    quality_score: float
    cost_units: int
    latency_ms: int
    fallback_priority: int


def _score(candidate: Candidate, strategy: RoutingStrategy) -> tuple[float, str]:
    if strategy == RoutingStrategy.CHEAPEST:
        return -float(candidate.cost_units), "lowest_cost"
    if strategy == RoutingStrategy.HIGHEST_QUALITY:
        return candidate.quality_score, "highest_quality"
    if strategy == RoutingStrategy.REGULATED:
        return candidate.quality_score - (candidate.cost_units * 0.01), "regulated_constraints"
    return (
        candidate.quality_score - (candidate.cost_units * 0.005) - (candidate.latency_ms * 0.0001),
        "balanced_quality_cost_latency",
    )


def _provider_config(session: Session, *, tenant_id: str, provider: str) -> MockProviderConfig:
    config = session.scalars(
        select(MockProviderConfig).where(
            MockProviderConfig.tenant_id == tenant_id,
            MockProviderConfig.provider == provider,
        )
    ).first()
    if config is None:
        raise ValidationError(f"Mock provider '{provider}' is not configured")
    return config


def _candidates_from_provider_set(
    session: Session,
    *,
    tenant_id: str,
    operation: str,
    provider_set: ProviderSetSpec,
    circuit_breaker: CircuitBreaker,
    explicit_provider: str | None,
) -> tuple[list[Candidate], list[str]]:
    candidates: list[Candidate] = []
    skipped: list[str] = []
    for spec in provider_set.providers:
        if explicit_provider is not None and spec.provider != explicit_provider:
            skipped.append(spec.provider)
            continue
        if not spec.enabled or spec.hard_deny or not spec.rollout_enabled:
            skipped.append(spec.provider)
            continue
        if operation not in spec.capabilities:
            skipped.append(spec.provider)
            continue
        if not circuit_breaker.is_available(tenant_id, spec.provider, operation):
            skipped.append(spec.provider)
            continue
        config = _provider_config(session, tenant_id=tenant_id, provider=spec.provider)
        candidates.append(
            Candidate(
                provider=spec.provider,
                quality_score=config.quality_score / 100.0,
                cost_units=config.cost_units,
                latency_ms=config.latency_ms,
                fallback_priority=spec.fallback_priority,
            )
        )
    explicit_unavailable = explicit_provider is not None and not any(
        c.provider == explicit_provider for c in candidates
    )
    if explicit_unavailable:
        raise ProviderUnavailableError(f"Explicit provider '{explicit_provider}' is unavailable")
    return candidates, skipped


def create_routing_decision(
    session: Session,
    *,
    tenant_id: str,
    operation: str,
    strategy: RoutingStrategy,
    provider_set_name: str,
    circuit_breaker: CircuitBreaker,
    explicit_provider: str | None = None,
    request_id: str | None = None,
    constraints: dict[str, Any] | None = None,
) -> RoutingDecision:
    started_at = perf_counter()
    request_id = request_id or str(uuid4())
    provider_set = get_provider_set(session, tenant_id=tenant_id, name=provider_set_name)
    candidates, skipped = _candidates_from_provider_set(
        session,
        tenant_id=tenant_id,
        operation=operation,
        provider_set=provider_set,
        circuit_breaker=circuit_breaker,
        explicit_provider=explicit_provider,
    )
    if not candidates:
        raise ProviderUnavailableError("No providers are available for this request")
    scored = []
    for candidate in candidates:
        score, reason = _score(candidate, strategy)
        scored.append((candidate, score, reason))
    scored.sort(key=lambda item: (-item[1], item[0].fallback_priority, item[0].provider))
    view = RoutingDecisionView(
        request_id=request_id,
        operation=operation,
        strategy=strategy,
        ordered_providers=[candidate.provider for candidate, _, _ in scored],
        skipped_providers=skipped,
        score_components=[
            ScoreComponent(
                provider=candidate.provider,
                score=score,
                quality_score=candidate.quality_score,
                cost_units=candidate.cost_units,
                latency_ms=candidate.latency_ms,
                reason=reason,
            )
            for candidate, score, reason in scored
        ],
        constraints=constraints or {},
    )
    decision_json = view.model_dump(mode="json")
    decision_json["metrics"] = {
        "routing_overhead_ms": round((perf_counter() - started_at) * 1000, 3)
    }
    row = RoutingDecision(
        tenant_id=tenant_id,
        request_id=request_id,
        operation=operation,
        strategy=strategy.value,
        decision_json=decision_json,
    )
    session.add(row)
    session.flush()
    return row
