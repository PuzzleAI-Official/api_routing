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


class ProviderSpec(BaseModel):
    provider: str
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
