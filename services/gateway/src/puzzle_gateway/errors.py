from __future__ import annotations

from typing import Any

from puzzle_shared import ApiErrorCode


class GatewayError(Exception):
    code = ApiErrorCode.INTERNAL_ERROR
    status_code = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthenticationError(GatewayError):
    code = ApiErrorCode.AUTHENTICATION_FAILED
    status_code = 401


class AuthorizationError(GatewayError):
    code = ApiErrorCode.AUTHORIZATION_FAILED
    status_code = 403


class ValidationError(GatewayError):
    code = ApiErrorCode.VALIDATION_FAILED
    status_code = 422


class RateLimitError(GatewayError):
    code = ApiErrorCode.RATE_LIMITED
    status_code = 429


class QuotaExceededError(GatewayError):
    code = ApiErrorCode.QUOTA_EXCEEDED
    status_code = 402


class IdempotencyConflictError(GatewayError):
    code = ApiErrorCode.IDEMPOTENCY_CONFLICT
    status_code = 409


class ProviderUnavailableError(GatewayError):
    code = ApiErrorCode.PROVIDER_UNAVAILABLE
    status_code = 503


class ProviderTimeoutError(GatewayError):
    code = ApiErrorCode.TIMEOUT
    status_code = 504
