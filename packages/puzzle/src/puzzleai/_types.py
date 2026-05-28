from __future__ import annotations

from enum import StrEnum


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
