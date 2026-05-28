from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ApiErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    VALIDATION_FAILED = "validation_failed"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"


class RoutingStrategy(StrEnum):
    BALANCED = "balanced"
    CHEAPEST = "cheapest"
    HIGHEST_QUALITY = "highest_quality"
    REGULATED = "regulated"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CredentialMode(StrEnum):
    MANAGED = "managed"
    BYOK = "byok"


class MockProviderBehavior(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"
    MALFORMED_RESPONSE = "malformed_response"


class ApiError(BaseModel):
    code: ApiErrorCode
    message: str
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RequestMetadata(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    operation: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BillingUsage(BaseModel):
    cost_units: int = 0
    charge_units: int = 0
    provider: str | None = None
    service_id: str | None = None


class ProviderSpec(BaseModel):
    provider: str
    service_id: str | None = None
    enabled: bool = True
    credential_mode: CredentialMode = CredentialMode.MANAGED
    hard_deny: bool = False
    fallback_priority: int = 100
    capabilities: list[str] = Field(default_factory=lambda: ["core.mock"])
    regions: list[str] = Field(default_factory=list)
    privacy_tags: list[str] = Field(default_factory=list)
    rollout_enabled: bool = True


class ShadowPolicy(BaseModel):
    enabled: bool = False
    sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    allowed_providers: list[str] = Field(default_factory=list)
    max_cost_units: int = 0
    allow_raw_bytes: bool = False


class ProviderSetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "default"
    providers: list[ProviderSpec]
    shadow_policy: ShadowPolicy = Field(default_factory=ShadowPolicy)


class ScoreComponent(BaseModel):
    provider: str
    service_id: str | None = None
    score: float
    quality_score: float
    cost_units: int
    latency_ms: int
    reason: str


class RoutingDecisionView(BaseModel):
    request_id: str
    operation: str
    strategy: RoutingStrategy
    ordered_providers: list[str]
    skipped_providers: list[str] = Field(default_factory=list)
    score_components: list[ScoreComponent] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class DocumentCapability(StrEnum):
    TEXT = "documents.text"
    LAYOUT = "documents.layout"
    TABLES = "documents.tables"
    FIGURES = "documents.figures"
    FIELDS = "documents.fields"
    CLASSIFICATION = "documents.classification"
    CITATIONS = "documents.citations"
    RAG_CHUNKS = "documents.rag_chunks"
    MARKDOWN = "documents.markdown"
    JSON = "documents.json"
    ASYNC = "documents.async"


class ProviderCapabilityStatus(StrEnum):
    DECLARED = "declared"
    CONFIGURED = "configured"
    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ProviderValidationStatus(StrEnum):
    NOT_VALIDATED = "not_validated"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WARNING = "warning"


class DocumentCapabilityMode(StrEnum):
    CONFIGURED = "configured"
    VERIFIED = "verified"


class InvoiceLineItemsMode(StrEnum):
    PREFERRED = "preferred"
    REQUIRED = "required"
    DISABLED = "disabled"


class WorkflowExecutionMode(StrEnum):
    SINGLE_SERVICE = "single_service"
    SEGMENTED_BY_PAGE = "segmented_by_page"
    CASCADE_ESCALATION = "cascade_escalation"
    MULTI_PROVIDER_MERGE = "multi_provider_merge"


class ProviderServiceRef(BaseModel):
    provider_id: str
    service_id: str


class ProviderServiceManifestSpec(BaseModel):
    provider_id: str
    service_id: str
    display_name: str
    capabilities: list[str]
    supported_mime_types: list[str]
    max_sync_bytes: int
    max_async_bytes: int
    supports_sync: bool = True
    supports_async: bool = False
    credential_schema: dict[str, Any] = Field(default_factory=dict)
    config_schema: dict[str, Any] = Field(default_factory=dict)
    option_schema: dict[str, Any] = Field(default_factory=dict)
    service_config: dict[str, Any] = Field(default_factory=dict)
    cost_model: dict[str, Any] = Field(default_factory=dict)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    normalizer_version: str = "documents.process.v1"
    readiness_status: str = "unknown"
    verified_capabilities: list[str] = Field(default_factory=list)
    capability_status: dict[str, str] = Field(default_factory=dict)
    last_validated_at: datetime | None = None
    validation_status: str = ProviderValidationStatus.NOT_VALIDATED.value
    validation_error_code: str | None = None


class ProviderServiceStatusView(BaseModel):
    tenant_id: str
    provider_id: str
    service_id: str
    display_name: str
    declared_capabilities: list[str]
    verified_capabilities: list[str]
    capability_status: dict[str, str]
    readiness_status: str
    validation_status: str
    validation_error_code: str | None = None
    last_validated_at: datetime | None = None
    async_execution_mode: str
    supported_mime_types: list[str]
    max_sync_bytes: int
    max_async_bytes: int
    last_validation_summary: dict[str, Any] | None = None


class DocumentProcessRequest(BaseModel):
    task: str = "parse"
    required_capabilities: list[str] = Field(
        default_factory=lambda: [DocumentCapability.TEXT.value]
    )
    optional_capabilities: list[str] = Field(
        default_factory=lambda: [
            DocumentCapability.LAYOUT.value,
            DocumentCapability.TABLES.value,
            DocumentCapability.FIELDS.value,
        ]
    )
    provider: str | None = None
    service_id: str | None = None
    provider_set: str = "documents-default"
    strategy: RoutingStrategy = RoutingStrategy.BALANCED
    idempotency_key: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, Any] = Field(default_factory=dict)


class DocumentPage(BaseModel):
    page_number: int
    text: str = ""
    confidence: float | None = None


class DocumentBlock(BaseModel):
    page_number: int
    block_type: str
    text: str = ""
    bbox: dict[str, Any] | None = None
    confidence: float | None = None


class DocumentTable(BaseModel):
    page_number: int | None = None
    rows: list[list[str]] = Field(default_factory=list)
    markdown: str | None = None
    confidence: float | None = None


class DocumentProcessResponse(BaseModel):
    schema_version: str = "documents.process.v1"
    request_id: str
    document_id: str
    provider_id: str
    service_id: str
    provider_request_id: str | None = None
    capabilities_satisfied: list[str] = Field(default_factory=list)
    capabilities_missing: list[str] = Field(default_factory=list)
    full_text: str = ""
    pages: list[DocumentPage] = Field(default_factory=list)
    blocks: list[DocumentBlock] = Field(default_factory=list)
    tables: list[DocumentTable] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)
    classification: dict[str, Any] | None = None
    chunks: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float | None = None
    warnings: list[str] = Field(default_factory=list)
    raw_result_ref: str
    usage: BillingUsage = Field(default_factory=BillingUsage)
    replayed: bool = False


class InvoiceExtractRequest(BaseModel):
    provider_set: str = "documents-default"
    strategy: RoutingStrategy = RoutingStrategy.BALANCED
    provider: str | None = None
    service_id: str | None = None
    line_items_mode: InvoiceLineItemsMode = InvoiceLineItemsMode.PREFERRED
    required_fields: list[str] = Field(default_factory=lambda: ["total"])
    idempotency_key: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, Any] = Field(default_factory=dict)


class InvoiceField(BaseModel):
    value: str | None = None
    raw_value: Any | None = None
    confidence: float | None = None
    source: dict[str, Any] | None = None


class InvoiceLineItem(BaseModel):
    description: InvoiceField = Field(default_factory=InvoiceField)
    quantity: InvoiceField = Field(default_factory=InvoiceField)
    unit_price: InvoiceField = Field(default_factory=InvoiceField)
    amount: InvoiceField = Field(default_factory=InvoiceField)
    confidence: float | None = None


class InvoiceObject(BaseModel):
    vendor_name: InvoiceField = Field(default_factory=InvoiceField)
    customer_name: InvoiceField = Field(default_factory=InvoiceField)
    invoice_number: InvoiceField = Field(default_factory=InvoiceField)
    purchase_order_number: InvoiceField = Field(default_factory=InvoiceField)
    invoice_date: InvoiceField = Field(default_factory=InvoiceField)
    due_date: InvoiceField = Field(default_factory=InvoiceField)
    currency: InvoiceField = Field(default_factory=InvoiceField)
    subtotal: InvoiceField = Field(default_factory=InvoiceField)
    tax: InvoiceField = Field(default_factory=InvoiceField)
    total: InvoiceField = Field(default_factory=InvoiceField)
    amount_due: InvoiceField = Field(default_factory=InvoiceField)
    line_items: list[InvoiceLineItem] = Field(default_factory=list)
    payment_terms: InvoiceField = Field(default_factory=InvoiceField)
    notes: InvoiceField = Field(default_factory=InvoiceField)


class WorkflowPlanSegment(BaseModel):
    segment_id: str = "document"
    page_range: str = "all"
    required_capabilities: list[str] = Field(default_factory=list)
    optional_capabilities: list[str] = Field(default_factory=list)
    ordered_services: list[ProviderServiceRef] = Field(default_factory=list)


class WorkflowExecutionPlan(BaseModel):
    mode: WorkflowExecutionMode = WorkflowExecutionMode.SINGLE_SERVICE
    workflow: str
    segments: list[WorkflowPlanSegment]
    policy_version: str = "workflow_rules_v1"


class WorkflowProviderPriorSpec(BaseModel):
    workflow: str = "invoice.extract"
    workflow_version: str = "invoice.extract.v1"
    provider_id: str
    service_id: str
    active: bool = True
    quality_prior: float = Field(default=0.75, ge=0.0, le=1.0)
    fallback_priority: int = Field(default=100, ge=1)
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowProviderServiceStatusView(BaseModel):
    tenant_id: str
    workflow: str
    workflow_version: str
    provider_id: str
    service_id: str
    active: bool
    selection_status: str
    reasons: list[str] = Field(default_factory=list)
    quality_prior: float | None = None
    fallback_priority: int | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    optional_capabilities: list[str] = Field(default_factory=list)
    declared_capabilities: list[str] = Field(default_factory=list)
    verified_capabilities: list[str] = Field(default_factory=list)
    capability_mode: str
    readiness_status: str
    validation_status: str
    validation_error_code: str | None = None
    last_validated_at: datetime | None = None


class InvoiceQuality(BaseModel):
    accepted: bool
    completeness_score: float
    field_confidence: float | None = None
    line_item_score: float
    missing_required_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class InvoiceExtractResponse(BaseModel):
    schema_version: str = "documents.invoice.extract.v1"
    request_id: str
    document_id: str
    workflow: str = "invoice.extract"
    workflow_version: str = "invoice.extract.v1"
    provider_id: str
    service_id: str
    provider_request_id: str | None = None
    execution_plan: WorkflowExecutionPlan
    routing_summary: dict[str, Any] = Field(default_factory=dict)
    invoice: InvoiceObject
    quality: InvoiceQuality
    warnings: list[str] = Field(default_factory=list)
    raw_result_ref: str
    usage: BillingUsage = Field(default_factory=BillingUsage)
    replayed: bool = False


class CoreMockRunRequest(BaseModel):
    operation: str = "core.mock"
    payload: dict[str, Any] = Field(default_factory=dict)
    strategy: RoutingStrategy = RoutingStrategy.BALANCED
    provider: str | None = None
    provider_set: str = "default"
    idempotency_key: str | None = None
    features: dict[str, Any] = Field(default_factory=dict)


class CoreMockRunResponse(BaseModel):
    request_id: str
    result: dict[str, Any]
    route_decision: RoutingDecisionView
    attempted_providers: list[str]
    usage: BillingUsage
    replayed: bool = False


class JobView(BaseModel):
    job_id: str
    status: JobStatus
    request_id: str
    result: dict[str, Any] | None = None
    error: ApiError | None = None
