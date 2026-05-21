from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from puzzle_shared import (
    BillingUsage,
    DocumentCapability,
    DocumentProcessRequest,
    InvoiceExtractRequest,
    InvoiceExtractResponse,
    InvoiceField,
    InvoiceLineItem,
    InvoiceLineItemsMode,
    InvoiceObject,
    InvoiceQuality,
    ProviderServiceRef,
    RoutingStrategy,
    WorkflowExecutionMode,
    WorkflowExecutionPlan,
    WorkflowPlanSegment,
)
from sqlalchemy.orm import Session

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.cost import charge_units_for_cost
from puzzle_gateway.documents.adapters import (
    DocumentAdapterResult,
    DocumentFile,
    DocumentProviderError,
    UnsupportedCapability,
    get_document_adapter,
)
from puzzle_gateway.documents.execution import _credential_json, _manifest_for
from puzzle_gateway.documents.manifests import spec_from_row
from puzzle_gateway.documents.routing import create_document_routing_decision
from puzzle_gateway.documents.workflow_priors import (
    INVOICE_WORKFLOW,
    INVOICE_WORKFLOW_VERSION,
    WORKFLOW_POLICY_VERSION,
    default_invoice_prior,
    workflow_priors_by_service,
)
from puzzle_gateway.errors import ProviderUnavailableError
from puzzle_gateway.models import (
    BillingLedgerEntry,
    DocumentWorkflowResult,
    ProviderAttempt,
    RoutingDecision,
)
from puzzle_gateway.storage import ObjectStore

INVOICE_OPERATION = "documents.invoice.extract"

MONEY_PATTERN = re.compile(r"(?P<currency>[A-Z]{3}|\$)?\s*(?P<amount>-?\d[\d,]*(?:\.\d+)?)")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y")

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "vendor_name": (
        "vendor_name",
        "vendor",
        "merchant",
        "supplier",
        "seller",
        "seller_name",
        "company",
    ),
    "customer_name": ("customer_name", "customer", "client", "bill_to", "buyer", "buyer_name"),
    "invoice_number": ("invoice_number", "invoice_no", "invoice id", "number", "id"),
    "purchase_order_number": ("purchase_order_number", "po_number", "purchase_order", "po"),
    "invoice_date": ("invoice_date", "date", "issue_date", "bill_date"),
    "due_date": ("due_date", "payment_due_date"),
    "currency": ("currency", "currency_code"),
    "subtotal": ("subtotal", "net_total", "net_amount"),
    "tax": ("tax", "total_tax", "tax_amount"),
    "total": ("total", "total_due", "grand_total", "amount_total", "invoice_amount", "amount"),
    "amount_due": ("amount_due", "balance_due", "total_due"),
    "payment_terms": ("payment_terms", "terms"),
    "notes": ("notes", "memo"),
}

MONEY_FIELDS = {"subtotal", "tax", "total", "amount_due"}
DATE_FIELDS = {"invoice_date", "due_date"}

MIN_TABLE_ROWS_WITH_HEADER = 2


@dataclass(frozen=True)
class InvoiceWorkflowResult:
    response: InvoiceExtractResponse
    attempted_provider_services: list[dict[str, str]]
    billing_entry_id: str


def invoice_document_request(request: InvoiceExtractRequest) -> DocumentProcessRequest:
    required = [DocumentCapability.TEXT.value, DocumentCapability.FIELDS.value]
    optional: list[str] = []
    if request.line_items_mode == InvoiceLineItemsMode.REQUIRED:
        required.append(DocumentCapability.TABLES.value)
    elif request.line_items_mode == InvoiceLineItemsMode.PREFERRED:
        optional.append(DocumentCapability.TABLES.value)
    return DocumentProcessRequest(
        task=INVOICE_WORKFLOW,
        required_capabilities=required,
        optional_capabilities=optional,
        provider=request.provider,
        service_id=request.service_id,
        provider_set=request.provider_set,
        strategy=request.strategy,
        idempotency_key=request.idempotency_key,
        provider_options=request.provider_options,
        features={
            **request.features,
            "workflow": INVOICE_WORKFLOW,
            "line_items_mode": request.line_items_mode.value,
            "required_fields": request.required_fields,
        },
    )


def _service_key(provider_id: str, service_id: str) -> str:
    return f"{provider_id}:{service_id}"


def _strategy_adjustment(
    *,
    strategy: RoutingStrategy,
    cost_units: int,
    latency_ms: int,
    quality: float,
) -> float:
    if strategy == RoutingStrategy.CHEAPEST:
        return -cost_units * 0.03
    if strategy == RoutingStrategy.HIGHEST_QUALITY:
        return quality * 0.6
    if strategy == RoutingStrategy.REGULATED:
        return quality * 0.4 - cost_units * 0.005
    return quality * 0.35 - cost_units * 0.006 - latency_ms * 0.0001


def _workflow_score(
    *,
    provider_id: str,
    service_id: str,
    manifest: dict[str, Any],
    strategy: RoutingStrategy,
    line_items_mode: InvoiceLineItemsMode,
    quality_prior: float | None,
) -> float:
    capabilities = set(manifest.get("verified_capabilities") or manifest.get("capabilities") or [])
    cost_model = manifest.get("cost_model", {})
    cost_units = int(cost_model.get("base_cost_units", 10))
    latency_ms = int(cost_model.get("latency_ms", 1000))
    quality = (
        quality_prior
        if quality_prior is not None
        else float(cost_model.get("quality_score", 75)) / 100
    )
    capability_fit = 0.0
    if DocumentCapability.TEXT.value in capabilities:
        capability_fit += 0.1
    if DocumentCapability.FIELDS.value in capabilities:
        capability_fit += 0.2
    if (
        line_items_mode == InvoiceLineItemsMode.PREFERRED
        and DocumentCapability.TABLES.value in capabilities
    ):
        capability_fit += 0.18
    return quality + capability_fit + _strategy_adjustment(
        strategy=strategy,
        cost_units=cost_units,
        latency_ms=latency_ms,
        quality=quality,
    )


def _workflow_plan(
    *,
    ordered_services: list[dict[str, str]],
    document_request: DocumentProcessRequest,
) -> WorkflowExecutionPlan:
    return WorkflowExecutionPlan(
        mode=WorkflowExecutionMode.SINGLE_SERVICE,
        workflow=INVOICE_WORKFLOW,
        policy_version=WORKFLOW_POLICY_VERSION,
        segments=[
            WorkflowPlanSegment(
                segment_id="document",
                page_range="all",
                required_capabilities=document_request.required_capabilities,
                optional_capabilities=document_request.optional_capabilities,
                ordered_services=[
                    ProviderServiceRef(
                        provider_id=item["provider_id"],
                        service_id=item["service_id"],
                    )
                    for item in ordered_services
                ],
            )
        ],
    )


def create_invoice_routing_decision(
    session: Session,
    *,
    tenant_id: str,
    request: InvoiceExtractRequest,
    request_id: str | None,
    mime_type: str,
    byte_size: int,
    circuit_breaker: CircuitBreaker,
    sync: bool,
) -> RoutingDecision:
    document_request = invoice_document_request(request)
    decision = create_document_routing_decision(
        session,
        tenant_id=tenant_id,
        request=document_request,
        request_id=request_id,
        mime_type=mime_type,
        byte_size=byte_size,
        circuit_breaker=circuit_breaker,
        sync=sync,
    )
    payload = dict(decision.decision_json)
    manifests = payload.get("service_manifests", {})
    ordered = list(payload.get("ordered_provider_services", []))
    priors = workflow_priors_by_service(session, tenant_id=tenant_id, workflow=INVOICE_WORKFLOW)
    scored: list[tuple[float, int, dict[str, str]]] = []
    skipped = list(payload.get("skipped_provider_services", []))
    for item in ordered:
        provider_id = str(item["provider_id"])
        service_id = str(item["service_id"])
        prior = priors.get(_service_key(provider_id, service_id)) or default_invoice_prior(
            provider_id,
            service_id,
        )
        if prior is not None and not prior.active:
            skipped.append(
                {
                    "provider_id": provider_id,
                    "service_id": service_id,
                    "reason": "workflow_inactive",
                }
            )
            continue
        score = _workflow_score(
            provider_id=provider_id,
            service_id=service_id,
            manifest=manifests.get(_service_key(provider_id, service_id), {}),
            strategy=request.strategy,
            line_items_mode=request.line_items_mode,
            quality_prior=prior.quality_prior if prior is not None else None,
        )
        fallback_priority = prior.fallback_priority if prior is not None else len(scored) + 100
        scored.append(
            (
                score,
                fallback_priority,
                {"provider_id": provider_id, "service_id": service_id},
            )
        )
    if not scored:
        payload["skipped_provider_services"] = skipped
        decision.decision_json = payload
        session.add(decision)
        session.flush()
        raise ProviderUnavailableError("No invoice provider services are active for this workflow")
    scored.sort(key=lambda item: (-item[0], item[1], item[2]["provider_id"], item[2]["service_id"]))
    ordered_services = [item for _score, _priority, item in scored]
    execution_plan = _workflow_plan(
        ordered_services=ordered_services,
        document_request=document_request,
    )
    payload["operation"] = INVOICE_OPERATION
    payload["workflow"] = INVOICE_WORKFLOW
    payload["workflow_version"] = INVOICE_WORKFLOW_VERSION
    payload["workflow_policy_version"] = WORKFLOW_POLICY_VERSION
    payload["ordered_provider_services"] = ordered_services
    payload["ordered_providers"] = [item["provider_id"] for item in ordered_services]
    payload["execution_plan"] = execution_plan.model_dump(mode="json")
    payload["routing_summary"] = {
        "line_items_mode": request.line_items_mode.value,
        "required_fields": request.required_fields,
        "workflow_scores": [
            {
                "provider_id": item["provider_id"],
                "service_id": item["service_id"],
                "score": score,
                "fallback_priority": fallback_priority,
            }
            for score, fallback_priority, item in scored
        ],
        "skipped_provider_services": skipped,
    }
    payload["skipped_provider_services"] = skipped
    decision.operation = INVOICE_OPERATION
    decision.decision_json = payload
    session.add(decision)
    session.flush()
    return decision


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _raw_field_value(raw: Any) -> tuple[Any | None, float | None]:
    if isinstance(raw, dict):
        confidence = raw.get("confidence", raw.get("score"))
        parsed_confidence = float(confidence) if isinstance(confidence, (int, float)) else None
        for key in ("value", "content", "text", "ocr_text", "raw_value"):
            if raw.get(key) not in (None, ""):
                return raw[key], parsed_confidence
        return None, parsed_confidence
    return raw, None


def _field_from_raw(raw: Any, *, field_name: str, warnings: list[str]) -> InvoiceField:
    value, confidence = _raw_field_value(raw)
    if value is None:
        return InvoiceField(raw_value=raw, confidence=confidence)
    raw_text = str(value).strip()
    parsed = raw_text
    if field_name in MONEY_FIELDS:
        parsed_money = _parse_money(raw_text, warnings=warnings, field_name=field_name)
        parsed = parsed_money if parsed_money is not None else raw_text
    elif field_name in DATE_FIELDS:
        parsed_date = _parse_date(raw_text, warnings=warnings, field_name=field_name)
        parsed = parsed_date if parsed_date is not None else raw_text
    return InvoiceField(value=parsed, raw_value=raw, confidence=confidence)


def _parse_money(value: str, *, warnings: list[str], field_name: str) -> str | None:
    match = MONEY_PATTERN.search(value.replace(",", ""))
    if match is None:
        warnings.append(f"{field_name}_money_parse_failed")
        return None
    try:
        return str(Decimal(match.group("amount")).quantize(Decimal("0.01")))
    except InvalidOperation:
        warnings.append(f"{field_name}_money_parse_failed")
        return None


def _parse_date(value: str, *, warnings: list[str], field_name: str) -> str | None:
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    warnings.append(f"{field_name}_date_parse_failed")
    return None


def _currency_from_text(value: str) -> str | None:
    upper = value.upper()
    for code in ("USD", "EUR", "GBP", "CAD", "AUD"):
        if code in upper:
            return code
    if "$" in value:
        return "USD"
    return None


def _find_field(fields: dict[str, Any], aliases: tuple[str, ...]) -> Any | None:
    normalized = {_normalize_key(key): value for key, value in fields.items()}
    for alias in aliases:
        key = _normalize_key(alias)
        if key in normalized:
            return normalized[key]
    return None


def _line_items_from_tables(
    result: DocumentAdapterResult,
    warnings: list[str],
) -> list[InvoiceLineItem]:
    items: list[InvoiceLineItem] = []
    for table in result.tables:
        rows = table.rows
        if len(rows) < MIN_TABLE_ROWS_WITH_HEADER:
            continue
        header = [_normalize_key(cell) for cell in rows[0]]
        description_idx = _first_index(header, ("description", "item", "product", "name"))
        quantity_idx = _first_index(header, ("qty", "quantity"))
        unit_price_idx = _first_index(header, ("unit_price", "price", "rate"))
        amount_idx = _first_index(header, ("amount", "total", "line_total"))
        if description_idx is None and amount_idx is None:
            continue
        for row in rows[1:]:
            items.append(
                InvoiceLineItem(
                    description=_line_field(row, description_idx),
                    quantity=_line_field(row, quantity_idx),
                    unit_price=_line_field(row, unit_price_idx),
                    amount=_line_field(row, amount_idx),
                )
            )
    if result.tables and not items:
        warnings.append("line_items_table_unmapped")
    return items


def _first_index(header: list[str], aliases: tuple[str, ...]) -> int | None:
    for alias in aliases:
        normalized_alias = _normalize_key(alias)
        for index, value in enumerate(header):
            if normalized_alias == value or normalized_alias in value:
                return index
    return None


def _line_field(row: list[str], index: int | None) -> InvoiceField:
    if index is None or index >= len(row):
        return InvoiceField()
    raw = row[index]
    return InvoiceField(value=str(raw).strip() or None, raw_value=raw)


def normalize_invoice_result(
    result: DocumentAdapterResult,
    *,
    request: InvoiceExtractRequest,
) -> tuple[InvoiceObject, list[str]]:
    warnings = list(result.warnings)
    invoice = InvoiceObject()
    for field_name, aliases in FIELD_ALIASES.items():
        raw = _find_field(result.fields, aliases)
        if raw is None:
            continue
        setattr(invoice, field_name, _field_from_raw(raw, field_name=field_name, warnings=warnings))
    if invoice.currency.value is None:
        for field_name in ("total", "amount_due", "subtotal", "tax"):
            field = getattr(invoice, field_name)
            if field.raw_value is None:
                continue
            currency = _currency_from_text(str(field.raw_value))
            if currency is not None:
                invoice.currency = InvoiceField(value=currency, raw_value=field.raw_value)
                break
    if request.line_items_mode != InvoiceLineItemsMode.DISABLED:
        invoice.line_items = _line_items_from_tables(result, warnings)
    return invoice, warnings


def evaluate_invoice_quality(
    invoice: InvoiceObject,
    *,
    request: InvoiceExtractRequest,
    warnings: list[str],
) -> InvoiceQuality:
    missing = [
        field_name
        for field_name in request.required_fields
        if getattr(invoice, field_name, InvoiceField()).value in (None, "")
    ]
    if request.line_items_mode == InvoiceLineItemsMode.REQUIRED and not invoice.line_items:
        missing.append("line_items")
    present_recommended = sum(
        1
        for field_name in (
            "vendor_name",
            "invoice_number",
            "invoice_date",
            "due_date",
            "subtotal",
            "tax",
            "total",
        )
        if getattr(invoice, field_name).value not in (None, "")
    )
    completeness = present_recommended / 7
    line_item_score = 1.0 if invoice.line_items else 0.0
    quality_warnings = list(warnings)
    for field_name in missing:
        quality_warnings.append(f"{field_name}_missing")
    return InvoiceQuality(
        accepted=not missing,
        completeness_score=round(completeness, 4),
        field_confidence=None,
        line_item_score=line_item_score,
        missing_required_fields=missing,
        warnings=quality_warnings,
    )


def _record_attempt(
    session: Session,
    *,
    tenant_id: str,
    request_id: str,
    job_id: str | None,
    routing_decision_id: str,
    provider_id: str,
    service_id: str,
    status: str,
    error_code: str | None = None,
    provider_request_id: str | None = None,
    latency_ms: int = 0,
    cost_units: int = 0,
    metadata: dict[str, Any] | None = None,
) -> ProviderAttempt:
    row = ProviderAttempt(
        tenant_id=tenant_id,
        request_id=request_id,
        job_id=job_id,
        routing_decision_id=routing_decision_id,
        provider=provider_id,
        service_id=service_id,
        provider_request_id=provider_request_id,
        status=status,
        error_code=error_code,
        latency_ms=latency_ms,
        cost_units=cost_units,
        capability_metadata_json=metadata or {},
    )
    session.add(row)
    session.flush()
    return row


def execute_invoice_workflow_decision(
    session: Session,
    *,
    tenant_id: str,
    document: DocumentFile,
    request: InvoiceExtractRequest,
    decision: RoutingDecision,
    object_store: ObjectStore,
    circuit_breaker: CircuitBreaker,
    idempotency_key: str | None,
    job_id: str | None = None,
) -> InvoiceWorkflowResult:
    attempted: list[dict[str, str]] = []
    rejections: list[dict[str, Any]] = []
    payload = decision.decision_json
    execution_plan = WorkflowExecutionPlan.model_validate(payload["execution_plan"])
    for item in payload["ordered_provider_services"]:
        provider_id = str(item["provider_id"])
        service_id = str(item["service_id"])
        provider_key = _service_key(provider_id, service_id)
        if not circuit_breaker.is_available(tenant_id, provider_key, INVOICE_OPERATION):
            continue
        attempted.append({"provider_id": provider_id, "service_id": service_id})
        manifest_row = _manifest_for(
            session,
            tenant_id=tenant_id,
            provider_id=provider_id,
            service_id=service_id,
        )
        manifest = spec_from_row(manifest_row)
        adapter = get_document_adapter(provider_id, service_id)
        try:
            credentials = _credential_json(
                session,
                tenant_id=tenant_id,
                provider_id=provider_id,
                requires_credentials=bool(
                    manifest.service_config.get("requires_credentials", True)
                ),
            )
            adapter_result = adapter.execute_sync(
                document=document,
                credentials=credentials,
                manifest=manifest,
                provider_options=request.provider_options.get(provider_id, {}),
            )
        except (DocumentProviderError, UnsupportedCapability) as exc:
            error_code = getattr(exc, "error_code", "unsupported_capability")
            _record_attempt(
                session,
                tenant_id=tenant_id,
                request_id=decision.request_id,
                job_id=job_id,
                routing_decision_id=decision.id,
                provider_id=provider_id,
                service_id=service_id,
                status="failed",
                error_code=error_code,
                metadata={"workflow": INVOICE_WORKFLOW, "workflow_accepted": False},
            )
            circuit_breaker.record_failure(tenant_id, provider_key, INVOICE_OPERATION)
            continue

        invoice, warnings = normalize_invoice_result(adapter_result, request=request)
        quality = evaluate_invoice_quality(invoice, request=request, warnings=warnings)
        cost_units = adapter.estimate_cost(
            manifest=manifest,
            page_count=len(adapter_result.pages) or 1,
        )
        metadata = {
            "workflow": INVOICE_WORKFLOW,
            "workflow_accepted": quality.accepted,
            "missing_required_fields": quality.missing_required_fields,
            "quality_score": quality.completeness_score,
        }
        if not quality.accepted:
            rejections.append(
                {
                    "provider_id": provider_id,
                    "service_id": service_id,
                    "missing_required_fields": quality.missing_required_fields,
                }
            )
            _record_attempt(
                session,
                tenant_id=tenant_id,
                request_id=decision.request_id,
                job_id=job_id,
                routing_decision_id=decision.id,
                provider_id=provider_id,
                service_id=service_id,
                status="workflow_rejected",
                error_code="workflow_quality_rejected",
                provider_request_id=adapter_result.provider_request_id,
                latency_ms=int(manifest.cost_model.get("latency_ms", 0)),
                cost_units=0,
                metadata={**metadata, "workflow_rejection_reason": "missing_required_fields"},
            )
            circuit_breaker.record_success(tenant_id, provider_key, INVOICE_OPERATION)
            continue

        raw_ref = object_store.put_bytes(
            tenant_id=tenant_id,
            name=f"raw/{decision.request_id}-{provider_id}-{service_id}-invoice.json",
            data=json.dumps(adapter_result.raw, sort_keys=True).encode("utf-8"),
        )
        charge_units = charge_units_for_cost(cost_units)
        usage = BillingUsage(
            cost_units=cost_units,
            charge_units=charge_units,
            provider=provider_id,
            service_id=service_id,
        )
        routing_summary = {
            **payload.get("routing_summary", {}),
            "attempted_provider_services": attempted,
            "workflow_rejections": rejections,
        }
        response = InvoiceExtractResponse(
            request_id=decision.request_id,
            document_id=document.document_id,
            provider_id=provider_id,
            service_id=service_id,
            provider_request_id=adapter_result.provider_request_id,
            execution_plan=execution_plan,
            routing_summary=routing_summary,
            invoice=invoice,
            quality=quality,
            warnings=quality.warnings,
            raw_result_ref=raw_ref,
            usage=usage,
        )
        _record_attempt(
            session,
            tenant_id=tenant_id,
            request_id=decision.request_id,
            job_id=job_id,
            routing_decision_id=decision.id,
            provider_id=provider_id,
            service_id=service_id,
            status="succeeded",
            provider_request_id=adapter_result.provider_request_id,
            latency_ms=int(manifest.cost_model.get("latency_ms", 0)),
            cost_units=cost_units,
            metadata=metadata,
        )
        ledger = BillingLedgerEntry(
            tenant_id=tenant_id,
            request_id=decision.request_id,
            operation=INVOICE_OPERATION,
            provider=provider_id,
            service_id=service_id,
            cost_units=cost_units,
            charge_units=charge_units,
            idempotency_key=idempotency_key,
        )
        workflow_result = DocumentWorkflowResult(
            tenant_id=tenant_id,
            document_id=document.document_id,
            request_id=decision.request_id,
            job_id=job_id,
            workflow=INVOICE_WORKFLOW,
            workflow_version=INVOICE_WORKFLOW_VERSION,
            provider_id=provider_id,
            service_id=service_id,
            execution_plan_json=execution_plan.model_dump(mode="json"),
            routing_summary_json=routing_summary,
            result_json=response.model_dump(mode="json"),
            quality_json=quality.model_dump(mode="json"),
            raw_result_ref=raw_ref,
        )
        session.add_all([ledger, workflow_result])
        circuit_breaker.record_success(tenant_id, provider_key, INVOICE_OPERATION)
        session.flush()
        return InvoiceWorkflowResult(
            response=response,
            attempted_provider_services=attempted,
            billing_entry_id=ledger.id,
        )

    raise ProviderUnavailableError("No invoice provider service produced an acceptable result")
