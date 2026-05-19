from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from puzzle_shared import BillingUsage, MockProviderBehavior
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.cost import charge_units_for_cost, normalize_mock_cost
from puzzle_gateway.errors import ProviderTimeoutError, ProviderUnavailableError, ValidationError
from puzzle_gateway.models import (
    BillingLedgerEntry,
    MockProviderConfig,
    ProviderAttempt,
    RoutingDecision,
)


@dataclass(frozen=True)
class ExecutionResult:
    result: dict[str, Any]
    attempted_providers: list[str]
    usage: BillingUsage
    billing_entry_id: str


class ProviderExecutionError(Exception):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _mock_provider_config(
    session: Session,
    *,
    tenant_id: str,
    provider: str,
) -> MockProviderConfig:
    config = session.scalars(
        select(MockProviderConfig).where(
            MockProviderConfig.tenant_id == tenant_id,
            MockProviderConfig.provider == provider,
        )
    ).first()
    if config is None:
        raise ValidationError(f"Mock provider '{provider}' is not configured")
    return config


def _call_mock_provider(config: MockProviderConfig, payload: dict[str, Any]) -> dict[str, Any]:
    behavior = MockProviderBehavior(config.behavior)
    if behavior == MockProviderBehavior.TIMEOUT:
        raise ProviderExecutionError("Mock provider timed out", error_code="timeout")
    if behavior == MockProviderBehavior.TRANSIENT_FAILURE:
        raise ProviderExecutionError(
            "Mock provider transient failure",
            error_code="transient_failure",
        )
    if behavior == MockProviderBehavior.PERMANENT_FAILURE:
        raise ProviderExecutionError(
            "Mock provider permanent failure",
            error_code="permanent_failure",
        )
    if behavior == MockProviderBehavior.MALFORMED_RESPONSE:
        raise ProviderExecutionError(
            "Mock provider malformed response",
            error_code="malformed_response",
        )
    return {
        "normalized": {
            "schema_version": "core.mock.v1",
            "provider": config.provider,
            "echo": payload,
        },
        "raw": {
            "provider": config.provider,
            "behavior": config.behavior,
            "payload": payload,
        },
    }


def execute_routing_decision(
    session: Session,
    *,
    tenant_id: str,
    operation: str,
    payload: dict[str, Any],
    decision: RoutingDecision,
    circuit_breaker: CircuitBreaker,
    idempotency_key: str | None = None,
    job_id: str | None = None,
) -> ExecutionResult:
    decision_payload = decision.decision_json
    attempted: list[str] = []
    last_error: str | None = None

    for provider in decision_payload["ordered_providers"]:
        if not circuit_breaker.is_available(tenant_id, provider, operation):
            continue
        attempted.append(provider)
        config = _mock_provider_config(session, tenant_id=tenant_id, provider=provider)
        try:
            result = _call_mock_provider(config, payload)
        except ProviderExecutionError as exc:
            last_error = exc.error_code
            session.add(
                ProviderAttempt(
                    tenant_id=tenant_id,
                    request_id=decision.request_id,
                    job_id=job_id,
                    routing_decision_id=decision.id,
                    provider=provider,
                    status="failed",
                    error_code=exc.error_code,
                    latency_ms=config.latency_ms,
                    cost_units=0,
                )
            )
            circuit_breaker.record_failure(tenant_id, provider, operation)
            session.flush()
            continue

        cost_units = normalize_mock_cost(provider_cost_units=config.cost_units)
        charge_units = charge_units_for_cost(cost_units)
        attempt = ProviderAttempt(
            tenant_id=tenant_id,
            request_id=decision.request_id,
            job_id=job_id,
            routing_decision_id=decision.id,
            provider=provider,
            status="succeeded",
            latency_ms=config.latency_ms,
            cost_units=cost_units,
        )
        ledger = BillingLedgerEntry(
            tenant_id=tenant_id,
            request_id=decision.request_id,
            operation=operation,
            provider=provider,
            cost_units=cost_units,
            charge_units=charge_units,
            idempotency_key=idempotency_key,
        )
        session.add_all([attempt, ledger])
        circuit_breaker.record_success(tenant_id, provider, operation)
        session.flush()
        return ExecutionResult(
            result=result,
            attempted_providers=attempted,
            usage=BillingUsage(
                cost_units=cost_units,
                charge_units=charge_units,
                provider=provider,
            ),
            billing_entry_id=ledger.id,
        )

    if last_error == "timeout":
        raise ProviderTimeoutError("All providers timed out or were unavailable")
    raise ProviderUnavailableError("All providers failed or were unavailable")
