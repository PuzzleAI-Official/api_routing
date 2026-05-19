from __future__ import annotations

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.idempotency import begin_operation, complete_operation, request_hash
from puzzle_gateway.kv import InMemoryKVStore
from puzzle_gateway.models import BillingLedgerEntry, ProviderAttempt, Tenant
from puzzle_gateway.providers import execute_routing_decision
from puzzle_gateway.routing import create_routing_decision
from puzzle_gateway.seed import upsert_mock_provider
from puzzle_shared import MockProviderBehavior, RoutingStrategy
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def _breaker() -> CircuitBreaker:
    return CircuitBreaker(InMemoryKVStore(), failure_threshold=3, cooldown_seconds=60)


def test_strategy_routing_prefers_cheapest_provider(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant

    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.CHEAPEST,
        provider_set_name="default",
        circuit_breaker=_breaker(),
        request_id="req-cheapest",
    )

    assert decision.decision_json["ordered_providers"][0] == "mock-secondary"


def test_explicit_provider_routing(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant

    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.BALANCED,
        provider_set_name="default",
        circuit_breaker=_breaker(),
        explicit_provider="mock-secondary",
        request_id="req-explicit",
    )

    assert decision.decision_json["ordered_providers"] == ["mock-secondary"]


def test_primary_failure_falls_back_to_secondary(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    breaker = _breaker()
    upsert_mock_provider(
        session,
        tenant_id=tenant.id,
        provider="mock-primary",
        behavior=MockProviderBehavior.TRANSIENT_FAILURE,
        quality_score=95,
        cost_units=20,
        latency_ms=100,
    )
    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.BALANCED,
        provider_set_name="default",
        circuit_breaker=breaker,
        request_id="req-fallback",
    )

    result = execute_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        payload={"hello": "world"},
        decision=decision,
        circuit_breaker=breaker,
    )

    assert result.attempted_providers == ["mock-primary", "mock-secondary"]
    assert result.result["normalized"]["provider"] == "mock-secondary"


def test_circuit_breaker_opens_and_router_skips_provider(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    breaker = _breaker()
    upsert_mock_provider(
        session,
        tenant_id=tenant.id,
        provider="mock-primary",
        behavior=MockProviderBehavior.TRANSIENT_FAILURE,
        quality_score=95,
        cost_units=20,
        latency_ms=100,
    )

    for index in range(3):
        decision = create_routing_decision(
            session,
            tenant_id=tenant.id,
            operation="core.mock",
            strategy=RoutingStrategy.BALANCED,
            provider_set_name="default",
            circuit_breaker=breaker,
            request_id=f"req-breaker-{index}",
        )
        execute_routing_decision(
            session,
            tenant_id=tenant.id,
            operation="core.mock",
            payload={},
            decision=decision,
            circuit_breaker=breaker,
        )

    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.BALANCED,
        provider_set_name="default",
        circuit_breaker=breaker,
        request_id="req-breaker-open",
    )

    assert decision.decision_json["ordered_providers"] == ["mock-secondary"]
    assert "mock-primary" in decision.decision_json["skipped_providers"]


def test_idempotency_replay_does_not_create_second_attempt_or_bill(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    breaker = _breaker()
    payload = {"payload": {"stable": True}}
    payload_hash = request_hash(payload)

    start = begin_operation(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        idempotency_key="idem-1",
        payload_hash=payload_hash,
    )
    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.BALANCED,
        provider_set_name="default",
        circuit_breaker=breaker,
        request_id="req-idem",
    )
    result = execute_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        payload=payload,
        decision=decision,
        circuit_breaker=breaker,
        idempotency_key="idem-1",
    )
    response = {"request_id": "req-idem", "result": result.result}
    complete_operation(
        session,
        start.record,
        response_json=response,
        billing_entry_id=result.billing_entry_id,
    )
    session.commit()

    attempts_before = session.scalar(select(func.count()).select_from(ProviderAttempt))
    bills_before = session.scalar(select(func.count()).select_from(BillingLedgerEntry))
    replay = begin_operation(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        idempotency_key="idem-1",
        payload_hash=payload_hash,
    )

    assert replay.replay_response == response
    assert session.scalar(select(func.count()).select_from(ProviderAttempt)) == attempts_before
    assert session.scalar(select(func.count()).select_from(BillingLedgerEntry)) == bills_before
