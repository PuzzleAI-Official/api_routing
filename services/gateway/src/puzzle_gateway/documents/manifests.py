from __future__ import annotations

from typing import Any

from puzzle_shared import (
    CredentialMode,
    ProviderServiceManifestSpec,
    ProviderSetSpec,
    ProviderSpec,
)
from puzzle_shared.schemas import DocumentCapability, ShadowPolicy
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.models import ProviderServiceManifest, now_utc
from puzzle_gateway.provider_sets import upsert_provider_set

DOCUMENT_OPERATION = "documents.process"
DEFAULT_DOCUMENT_PROVIDER_SET = "documents-default"
SUPPORTED_DOCUMENT_MIME_TYPES = ["application/pdf", "image/png", "image/jpeg", "image/tiff"]
SYNC_MAX_BYTES = 20 * 1024 * 1024
ASYNC_MAX_BYTES = 100 * 1024 * 1024
ASYNC_PROVIDER_NATIVE = "provider_native"
ASYNC_WORKER_SYNC = "worker_sync"


def _capability_status(capabilities: list[str]) -> dict[str, str]:
    return {capability: "configured" for capability in capabilities}


def _base_cost_model(*, cost_units: int, quality_score: int, latency_ms: int) -> dict[str, int]:
    return {
        "base_cost_units": cost_units,
        "quality_score": quality_score,
        "latency_ms": latency_ms,
    }


def phase2_provider_specs(
    *,
    provider_configs: dict[str, dict[str, Any]] | None = None,
) -> list[ProviderServiceManifestSpec]:
    configs = provider_configs or {}
    mindee_capabilities = [
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.ASYNC.value,
    ]
    veryfi_capabilities = [
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.TABLES.value,
    ]
    nanonets_capabilities = [
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.TABLES.value,
    ]
    klippa_capabilities = [
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.TABLES.value,
        DocumentCapability.ASYNC.value,
    ]
    return [
        ProviderServiceManifestSpec(
            provider_id="mindee",
            service_id="model_inference",
            display_name="Mindee Model Inference",
            capabilities=mindee_capabilities,
            supported_mime_types=SUPPORTED_DOCUMENT_MIME_TYPES,
            max_sync_bytes=SYNC_MAX_BYTES,
            max_async_bytes=ASYNC_MAX_BYTES,
            supports_sync=True,
            supports_async=True,
            credential_schema={"required": ["api_key"]},
            service_config={
                "async_execution_mode": ASYNC_PROVIDER_NATIVE,
                **configs.get("mindee", {}),
            },
            cost_model=_base_cost_model(cost_units=15, quality_score=82, latency_ms=1400),
            retry_policy={"max_attempts": 2, "retry_statuses": [429, 500, 502, 503, 504]},
            readiness_status="configured",
            capability_status=_capability_status(mindee_capabilities),
        ),
        ProviderServiceManifestSpec(
            provider_id="veryfi",
            service_id="documents",
            display_name="Veryfi Documents",
            capabilities=veryfi_capabilities,
            supported_mime_types=SUPPORTED_DOCUMENT_MIME_TYPES,
            max_sync_bytes=SYNC_MAX_BYTES,
            max_async_bytes=SYNC_MAX_BYTES,
            supports_sync=True,
            supports_async=True,
            credential_schema={"required": ["client_id", "client_secret", "username", "api_key"]},
            service_config={
                "async_execution_mode": ASYNC_WORKER_SYNC,
                **configs.get("veryfi", {}),
            },
            cost_model=_base_cost_model(cost_units=18, quality_score=86, latency_ms=1800),
            retry_policy={"max_attempts": 2, "retry_statuses": [429, 500, 502, 503, 504]},
            readiness_status="configured",
            capability_status=_capability_status(veryfi_capabilities),
        ),
        ProviderServiceManifestSpec(
            provider_id="nanonets",
            service_id="ocr_model",
            display_name="Nanonets OCR Model",
            capabilities=nanonets_capabilities,
            supported_mime_types=SUPPORTED_DOCUMENT_MIME_TYPES,
            max_sync_bytes=SYNC_MAX_BYTES,
            max_async_bytes=SYNC_MAX_BYTES,
            supports_sync=True,
            supports_async=True,
            credential_schema={"required": ["api_key"]},
            service_config={
                "async_execution_mode": ASYNC_WORKER_SYNC,
                **configs.get("nanonets", {}),
            },
            cost_model=_base_cost_model(cost_units=12, quality_score=80, latency_ms=1700),
            retry_policy={"max_attempts": 2, "retry_statuses": [429, 500, 502, 503, 504]},
            readiness_status="configured",
            capability_status=_capability_status(nanonets_capabilities),
        ),
        ProviderServiceManifestSpec(
            provider_id="klippa",
            service_id="generic",
            display_name="Klippa DocHorizon Generic",
            capabilities=klippa_capabilities,
            supported_mime_types=SUPPORTED_DOCUMENT_MIME_TYPES,
            max_sync_bytes=SYNC_MAX_BYTES,
            max_async_bytes=ASYNC_MAX_BYTES,
            supports_sync=True,
            supports_async=True,
            credential_schema={"required": ["api_key"]},
            service_config={
                "async_execution_mode": ASYNC_PROVIDER_NATIVE,
                **configs.get("klippa", {}),
            },
            cost_model=_base_cost_model(cost_units=14, quality_score=83, latency_ms=1600),
            retry_policy={"max_attempts": 2, "retry_statuses": [429, 500, 502, 503, 504]},
            readiness_status="configured",
            capability_status=_capability_status(klippa_capabilities),
        ),
    ]


def fake_document_provider_spec(
    *,
    provider_id: str,
    service_id: str = "parse",
    behavior: str = "success",
    cost_units: int = 5,
    quality_score: int = 70,
) -> ProviderServiceManifestSpec:
    capabilities = [
        DocumentCapability.TEXT.value,
        DocumentCapability.LAYOUT.value,
        DocumentCapability.TABLES.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.ASYNC.value,
    ]
    return ProviderServiceManifestSpec(
        provider_id=provider_id,
        service_id=service_id,
        display_name=f"{provider_id} {service_id}",
        capabilities=capabilities,
        supported_mime_types=SUPPORTED_DOCUMENT_MIME_TYPES,
        max_sync_bytes=SYNC_MAX_BYTES,
        max_async_bytes=ASYNC_MAX_BYTES,
        supports_sync=True,
        supports_async=True,
        credential_schema={},
        service_config={
            "behavior": behavior,
            "requires_credentials": False,
            "async_execution_mode": ASYNC_PROVIDER_NATIVE,
        },
        cost_model=_base_cost_model(
            cost_units=cost_units,
            quality_score=quality_score,
            latency_ms=50,
        ),
        readiness_status="configured",
        capability_status=_capability_status(capabilities),
    )


def spec_from_row(row: ProviderServiceManifest) -> ProviderServiceManifestSpec:
    return ProviderServiceManifestSpec(
        provider_id=row.provider_id,
        service_id=row.service_id,
        display_name=row.display_name,
        capabilities=row.capabilities_json,
        supported_mime_types=row.supported_mime_types_json,
        max_sync_bytes=row.max_sync_bytes,
        max_async_bytes=row.max_async_bytes,
        supports_sync=row.supports_sync,
        supports_async=row.supports_async,
        credential_schema=row.credential_schema_json,
        config_schema=row.config_schema_json,
        option_schema=row.option_schema_json,
        service_config=row.service_config_json,
        cost_model=row.cost_model_json,
        retry_policy=row.retry_policy_json,
        normalizer_version=row.normalizer_version,
        readiness_status=row.readiness_status,
        verified_capabilities=row.verified_capabilities_json,
        capability_status=row.capability_status_json,
        last_validated_at=row.last_validated_at,
        validation_status=row.validation_status,
        validation_error_code=row.validation_error_code,
    )


def upsert_provider_service_manifest(
    session: Session,
    *,
    tenant_id: str,
    spec: ProviderServiceManifestSpec,
) -> ProviderServiceManifest:
    row = session.scalars(
        select(ProviderServiceManifest).where(
            ProviderServiceManifest.tenant_id == tenant_id,
            ProviderServiceManifest.provider_id == spec.provider_id,
            ProviderServiceManifest.service_id == spec.service_id,
        )
    ).first()
    if row is None:
        row = ProviderServiceManifest(
            tenant_id=tenant_id,
            provider_id=spec.provider_id,
            service_id=spec.service_id,
        )
    row.display_name = spec.display_name
    row.capabilities_json = spec.capabilities
    row.supported_mime_types_json = spec.supported_mime_types
    row.max_sync_bytes = spec.max_sync_bytes
    row.max_async_bytes = spec.max_async_bytes
    row.supports_sync = spec.supports_sync
    row.supports_async = spec.supports_async
    row.credential_schema_json = spec.credential_schema
    row.config_schema_json = spec.config_schema
    row.option_schema_json = spec.option_schema
    row.service_config_json = spec.service_config
    row.cost_model_json = spec.cost_model
    row.retry_policy_json = spec.retry_policy
    row.normalizer_version = spec.normalizer_version
    row.readiness_status = spec.readiness_status
    row.verified_capabilities_json = spec.verified_capabilities
    row.capability_status_json = spec.capability_status or _capability_status(spec.capabilities)
    row.last_validated_at = spec.last_validated_at
    row.validation_status = spec.validation_status
    row.validation_error_code = spec.validation_error_code
    row.updated_at = now_utc()
    session.add(row)
    session.flush()
    return row


def create_documents_provider_set(
    session: Session,
    *,
    tenant_id: str,
    specs: list[ProviderServiceManifestSpec],
) -> None:
    provider_specs = [
        ProviderSpec(
            provider=spec.provider_id,
            service_id=spec.service_id,
            credential_mode=CredentialMode.BYOK,
            fallback_priority=index + 1,
            capabilities=[DOCUMENT_OPERATION, *spec.capabilities],
        )
        for index, spec in enumerate(specs)
    ]
    upsert_provider_set(
        session,
        tenant_id=tenant_id,
        spec=ProviderSetSpec(
            name=DEFAULT_DOCUMENT_PROVIDER_SET,
            providers=provider_specs,
            shadow_policy=ShadowPolicy(enabled=False),
        ),
    )
