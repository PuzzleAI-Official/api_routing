from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from puzzle_shared import DocumentProcessRequest, RoutingDecisionView, RoutingStrategy
from puzzle_shared.schemas import ScoreComponent
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.config import settings
from puzzle_gateway.documents.manifests import DOCUMENT_OPERATION, spec_from_row
from puzzle_gateway.errors import ProviderUnavailableError
from puzzle_gateway.models import ProviderCredential, ProviderServiceManifest, RoutingDecision
from puzzle_gateway.provider_sets import get_provider_set


@dataclass(frozen=True)
class DocumentServiceCandidate:
    provider_id: str
    service_id: str
    quality_score: float
    cost_units: int
    latency_ms: int
    fallback_priority: int
    manifest: dict[str, Any]


def _score(candidate: DocumentServiceCandidate, strategy: RoutingStrategy) -> tuple[float, str]:
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


def _has_active_credential(session: Session, *, tenant_id: str, provider_id: str) -> bool:
    return (
        session.scalars(
            select(ProviderCredential).where(
                ProviderCredential.tenant_id == tenant_id,
                ProviderCredential.provider == provider_id,
                ProviderCredential.active.is_(True),
            )
        ).first()
        is not None
    )


def _capability_mode() -> str:
    mode = os.getenv("PUZZLE_DOCUMENT_CAPABILITY_MODE", settings.document_capability_mode)
    return "verified" if mode == "verified" else "configured"


def _skip(
    skipped: list[dict[str, str]],
    provider_id: str,
    service_id: str,
    reason: str,
    capabilities: list[str] | None = None,
) -> None:
    item = {"provider_id": provider_id, "service_id": service_id, "reason": reason}
    if capabilities:
        item["capabilities"] = ",".join(capabilities)
    skipped.append(item)


def create_document_routing_decision(  # noqa: PLR0912, PLR0915
    session: Session,
    *,
    tenant_id: str,
    request: DocumentProcessRequest,
    request_id: str | None,
    mime_type: str,
    byte_size: int,
    circuit_breaker: CircuitBreaker,
    sync: bool,
) -> RoutingDecision:
    resolved_request_id = request_id or str(uuid4())
    provider_set = get_provider_set(session, tenant_id=tenant_id, name=request.provider_set)
    manifests = session.scalars(
        select(ProviderServiceManifest).where(ProviderServiceManifest.tenant_id == tenant_id)
    ).all()
    manifest_by_key = {(row.provider_id, row.service_id): row for row in manifests}
    skipped: list[dict[str, str]] = []
    candidates: list[DocumentServiceCandidate] = []
    capability_mode = _capability_mode()

    for provider_spec in provider_set.providers:
        if (
            not provider_spec.enabled
            or provider_spec.hard_deny
            or not provider_spec.rollout_enabled
        ):
            _skip(
                skipped,
                provider_spec.provider,
                provider_spec.service_id or "*",
                "provider_disabled",
            )
            continue
        if request.provider and provider_spec.provider != request.provider:
            _skip(
                skipped,
                provider_spec.provider,
                provider_spec.service_id or "*",
                "explicit_provider",
            )
            continue
        if request.service_id and provider_spec.service_id != request.service_id:
            _skip(
                skipped,
                provider_spec.provider,
                provider_spec.service_id or "*",
                "explicit_service",
            )
            continue
        if DOCUMENT_OPERATION not in provider_spec.capabilities:
            _skip(
                skipped,
                provider_spec.provider,
                provider_spec.service_id or "*",
                "operation_not_allowed",
            )
            continue

        provider_manifests = [
            row
            for (provider_id, _service_id), row in manifest_by_key.items()
            if provider_id == provider_spec.provider
            and (provider_spec.service_id is None or row.service_id == provider_spec.service_id)
        ]
        if not provider_manifests:
            _skip(
                skipped,
                provider_spec.provider,
                provider_spec.service_id or "*",
                "manifest_missing",
            )
            continue

        for manifest_row in provider_manifests:
            spec = spec_from_row(manifest_row)
            provider_key = f"{spec.provider_id}:{spec.service_id}"
            if sync and not spec.supports_sync:
                _skip(skipped, spec.provider_id, spec.service_id, "sync_unsupported")
                continue
            if not sync and not spec.supports_async:
                _skip(skipped, spec.provider_id, spec.service_id, "async_unsupported")
                continue
            if mime_type not in spec.supported_mime_types:
                _skip(skipped, spec.provider_id, spec.service_id, "mime_type_unsupported")
                continue
            max_bytes = spec.max_sync_bytes if sync else spec.max_async_bytes
            if byte_size > max_bytes:
                _skip(skipped, spec.provider_id, spec.service_id, "file_too_large")
                continue
            missing = sorted(set(request.required_capabilities) - set(spec.capabilities))
            if missing:
                _skip(
                    skipped,
                    spec.provider_id,
                    spec.service_id,
                    "capability_missing",
                    missing,
                )
                continue
            if capability_mode == "verified":
                unverified = sorted(
                    set(request.required_capabilities) - set(spec.verified_capabilities)
                )
                if unverified:
                    _skip(
                        skipped,
                        spec.provider_id,
                        spec.service_id,
                        "capability_not_verified",
                        unverified,
                    )
                    continue
            requires_credentials = bool(spec.service_config.get("requires_credentials", True))
            if requires_credentials and not _has_active_credential(
                session,
                tenant_id=tenant_id,
                provider_id=spec.provider_id,
            ):
                _skip(skipped, spec.provider_id, spec.service_id, "credential_missing")
                continue
            if not circuit_breaker.is_available(tenant_id, provider_key, DOCUMENT_OPERATION):
                _skip(skipped, spec.provider_id, spec.service_id, "circuit_open")
                continue
            cost_model = spec.cost_model
            candidates.append(
                DocumentServiceCandidate(
                    provider_id=spec.provider_id,
                    service_id=spec.service_id,
                    quality_score=float(cost_model.get("quality_score", 75)) / 100.0,
                    cost_units=int(cost_model.get("base_cost_units", 10)),
                    latency_ms=int(cost_model.get("latency_ms", 1000)),
                    fallback_priority=provider_spec.fallback_priority,
                    manifest=spec.model_dump(mode="json"),
                )
            )

    explicit_unavailable = (
        request.provider is not None or request.service_id is not None
    ) and not candidates
    if explicit_unavailable:
        raise ProviderUnavailableError("Explicit document provider service is unavailable")
    if not candidates:
        raise ProviderUnavailableError("No document provider services are available")

    scored = [(candidate, *_score(candidate, request.strategy)) for candidate in candidates]
    scored.sort(key=lambda item: (-item[1], item[0].fallback_priority, item[0].provider_id))
    ordered_services = [
        {"provider_id": candidate.provider_id, "service_id": candidate.service_id}
        for candidate, _score_value, _reason in scored
    ]
    view = RoutingDecisionView(
        request_id=resolved_request_id,
        operation=DOCUMENT_OPERATION,
        strategy=request.strategy,
        ordered_providers=[item["provider_id"] for item in ordered_services],
        skipped_providers=[item["provider_id"] for item in skipped],
        score_components=[
            ScoreComponent(
                provider=candidate.provider_id,
                service_id=candidate.service_id,
                score=score,
                quality_score=candidate.quality_score,
                cost_units=candidate.cost_units,
                latency_ms=candidate.latency_ms,
                reason=reason,
            )
            for candidate, score, reason in scored
        ],
        constraints={
            "task": request.task,
            "required_capabilities": request.required_capabilities,
            "optional_capabilities": request.optional_capabilities,
            "mime_type": mime_type,
            "byte_size": byte_size,
            "capability_mode": capability_mode,
        },
    )
    decision_json = view.model_dump(mode="json")
    decision_json["ordered_provider_services"] = ordered_services
    decision_json["skipped_provider_services"] = skipped
    decision_json["service_manifests"] = {
        f"{candidate.provider_id}:{candidate.service_id}": candidate.manifest
        for candidate, _score_value, _reason in scored
    }
    row = RoutingDecision(
        tenant_id=tenant_id,
        request_id=resolved_request_id,
        operation=DOCUMENT_OPERATION,
        strategy=request.strategy.value,
        decision_json=decision_json,
    )
    session.add(row)
    session.flush()
    return row
