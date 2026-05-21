from __future__ import annotations

import os
from typing import Any

from puzzle_shared import (
    DocumentCapability,
    InvoiceLineItemsMode,
    WorkflowProviderPriorSpec,
    WorkflowProviderServiceStatusView,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.config import settings
from puzzle_gateway.documents.manifests import (
    DEFAULT_DOCUMENT_PROVIDER_SET,
    DOCUMENT_OPERATION,
    spec_from_row,
)
from puzzle_gateway.models import (
    ProviderCredential,
    ProviderServiceManifest,
    WorkflowProviderPrior,
    now_utc,
)
from puzzle_gateway.provider_sets import get_provider_set

INVOICE_WORKFLOW = "invoice.extract"
INVOICE_WORKFLOW_VERSION = "invoice.extract.v1"
WORKFLOW_POLICY_VERSION = "workflow_rules_v1"

DEFAULT_INVOICE_PRIORS = [
    WorkflowProviderPriorSpec(
        provider_id="klippa",
        service_id="generic",
        active=True,
        quality_prior=0.93,
        fallback_priority=1,
        notes="Primary invoice candidate after live validation for text, fields, and tables.",
    ),
    WorkflowProviderPriorSpec(
        provider_id="nanonets",
        service_id="ocr_model",
        active=True,
        quality_prior=0.76,
        fallback_priority=2,
        notes="Secondary invoice candidate; text and fields validated, tables optional.",
    ),
    WorkflowProviderPriorSpec(
        provider_id="veryfi",
        service_id="documents",
        active=False,
        quality_prior=0.90,
        fallback_priority=50,
        notes="High-potential invoice provider; inactive until account access is refreshed.",
    ),
    WorkflowProviderPriorSpec(
        provider_id="mindee",
        service_id="model_inference",
        active=False,
        quality_prior=0.72,
        fallback_priority=60,
        notes="Model-dependent provider; inactive until model access and validation pass.",
    ),
]


def workflow_prior_spec_from_row(row: WorkflowProviderPrior) -> WorkflowProviderPriorSpec:
    return WorkflowProviderPriorSpec(
        workflow=row.workflow,
        workflow_version=row.workflow_version,
        provider_id=row.provider_id,
        service_id=row.service_id,
        active=row.active,
        quality_prior=row.quality_prior,
        fallback_priority=row.fallback_priority,
        notes=row.notes,
        metadata=row.metadata_json,
    )


def default_invoice_prior(provider_id: str, service_id: str) -> WorkflowProviderPriorSpec | None:
    for prior in DEFAULT_INVOICE_PRIORS:
        if prior.provider_id == provider_id and prior.service_id == service_id:
            return prior
    return None


def upsert_workflow_prior(
    session: Session,
    *,
    tenant_id: str,
    spec: WorkflowProviderPriorSpec,
) -> WorkflowProviderPrior:
    row = session.scalars(
        select(WorkflowProviderPrior).where(
            WorkflowProviderPrior.tenant_id == tenant_id,
            WorkflowProviderPrior.workflow == spec.workflow,
            WorkflowProviderPrior.provider_id == spec.provider_id,
            WorkflowProviderPrior.service_id == spec.service_id,
        )
    ).first()
    if row is None:
        row = WorkflowProviderPrior(
            tenant_id=tenant_id,
            workflow=spec.workflow,
            provider_id=spec.provider_id,
            service_id=spec.service_id,
        )
    row.workflow_version = spec.workflow_version
    row.active = spec.active
    row.quality_prior = spec.quality_prior
    row.fallback_priority = spec.fallback_priority
    row.notes = spec.notes
    row.metadata_json = spec.metadata
    row.updated_at = now_utc()
    session.add(row)
    session.flush()
    return row


def seed_invoice_workflow_priors(
    session: Session,
    *,
    tenant_id: str,
) -> list[WorkflowProviderPrior]:
    return [
        upsert_workflow_prior(session, tenant_id=tenant_id, spec=spec)
        for spec in DEFAULT_INVOICE_PRIORS
    ]


def list_workflow_priors(
    session: Session,
    *,
    tenant_id: str,
    workflow: str = INVOICE_WORKFLOW,
) -> list[WorkflowProviderPriorSpec]:
    rows = session.scalars(
        select(WorkflowProviderPrior)
        .where(
            WorkflowProviderPrior.tenant_id == tenant_id,
            WorkflowProviderPrior.workflow == workflow,
        )
        .order_by(
            WorkflowProviderPrior.fallback_priority,
            WorkflowProviderPrior.provider_id,
            WorkflowProviderPrior.service_id,
        )
    ).all()
    return [workflow_prior_spec_from_row(row) for row in rows]


def workflow_priors_by_service(
    session: Session,
    *,
    tenant_id: str,
    workflow: str = INVOICE_WORKFLOW,
) -> dict[str, WorkflowProviderPriorSpec]:
    return {
        f"{spec.provider_id}:{spec.service_id}": spec
        for spec in list_workflow_priors(session, tenant_id=tenant_id, workflow=workflow)
    }


def _capability_mode() -> str:
    mode = os.getenv("PUZZLE_DOCUMENT_CAPABILITY_MODE", settings.document_capability_mode)
    return "verified" if mode == "verified" else "configured"


def _invoice_capabilities(
    line_items_mode: InvoiceLineItemsMode,
) -> tuple[list[str], list[str]]:
    required = [DocumentCapability.TEXT.value, DocumentCapability.FIELDS.value]
    optional: list[str] = []
    if line_items_mode == InvoiceLineItemsMode.REQUIRED:
        required.append(DocumentCapability.TABLES.value)
    elif line_items_mode == InvoiceLineItemsMode.PREFERRED:
        optional.append(DocumentCapability.TABLES.value)
    return required, optional


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


def _provider_set_service_keys(
    session: Session,
    *,
    tenant_id: str,
    provider_set_name: str,
) -> dict[str, Any]:
    provider_set = get_provider_set(session, tenant_id=tenant_id, name=provider_set_name)
    return {
        f"{provider.provider}:{provider.service_id or '*'}": provider
        for provider in provider_set.providers
        if DOCUMENT_OPERATION in provider.capabilities
    }


def workflow_provider_service_statuses(
    session: Session,
    *,
    tenant_id: str,
    workflow: str = INVOICE_WORKFLOW,
    provider_set_name: str = DEFAULT_DOCUMENT_PROVIDER_SET,
    line_items_mode: InvoiceLineItemsMode = InvoiceLineItemsMode.PREFERRED,
) -> list[WorkflowProviderServiceStatusView]:
    if workflow != INVOICE_WORKFLOW:
        return []
    required, optional = _invoice_capabilities(line_items_mode)
    capability_mode = _capability_mode()
    priors = workflow_priors_by_service(session, tenant_id=tenant_id, workflow=workflow)
    provider_set_services = _provider_set_service_keys(
        session,
        tenant_id=tenant_id,
        provider_set_name=provider_set_name,
    )
    rows = session.scalars(
        select(ProviderServiceManifest)
        .where(ProviderServiceManifest.tenant_id == tenant_id)
        .order_by(ProviderServiceManifest.provider_id, ProviderServiceManifest.service_id)
    ).all()
    views: list[WorkflowProviderServiceStatusView] = []
    for row in rows:
        spec = spec_from_row(row)
        service_key = f"{spec.provider_id}:{spec.service_id}"
        provider_set_key = service_key
        provider_wildcard_key = f"{spec.provider_id}:*"
        provider_spec = provider_set_services.get(
            provider_set_key,
            provider_set_services.get(provider_wildcard_key),
        )
        prior = priors.get(service_key) or default_invoice_prior(spec.provider_id, spec.service_id)
        active = True if prior is None else prior.active
        reasons: list[str] = []
        if provider_spec is None:
            reasons.append("not_in_provider_set")
        elif (
            not provider_spec.enabled
            or provider_spec.hard_deny
            or not provider_spec.rollout_enabled
        ):
            reasons.append("provider_set_inactive")
        if not active:
            reasons.append("workflow_inactive")
        missing = sorted(set(required) - set(spec.capabilities))
        if missing:
            reasons.append(f"capability_missing:{','.join(missing)}")
        if capability_mode == "verified":
            unverified = sorted(set(required) - set(spec.verified_capabilities))
            if unverified:
                reasons.append(f"capability_not_verified:{','.join(unverified)}")
        if bool(spec.service_config.get("requires_credentials", True)) and not (
            _has_active_credential(
                session,
                tenant_id=tenant_id,
                provider_id=spec.provider_id,
            )
        ):
            reasons.append("credential_missing")
        selection_status = "eligible" if not reasons else "inactive" if not active else "blocked"
        views.append(
            WorkflowProviderServiceStatusView(
                tenant_id=tenant_id,
                workflow=workflow,
                workflow_version=INVOICE_WORKFLOW_VERSION,
                provider_id=spec.provider_id,
                service_id=spec.service_id,
                active=active,
                selection_status=selection_status,
                reasons=reasons,
                quality_prior=prior.quality_prior if prior is not None else None,
                fallback_priority=prior.fallback_priority if prior is not None else None,
                required_capabilities=required,
                optional_capabilities=optional,
                declared_capabilities=spec.capabilities,
                verified_capabilities=spec.verified_capabilities,
                capability_mode=capability_mode,
                readiness_status=spec.readiness_status,
                validation_status=spec.validation_status,
                validation_error_code=spec.validation_error_code,
                last_validated_at=spec.last_validated_at,
            )
        )
    return views
