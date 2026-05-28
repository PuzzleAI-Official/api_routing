from __future__ import annotations

from typing import Any

from puzzle._types import ApiErrorCode


class PuzzleError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: ApiErrorCode = ApiErrorCode.INTERNAL_ERROR,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.details = details or {}


class PuzzleAuthenticationError(PuzzleError):
    pass


class PuzzleAuthorizationError(PuzzleError):
    pass


class PuzzleValidationError(PuzzleError):
    pass


class PuzzleRateLimitError(PuzzleError):
    pass


class PuzzleQuotaExceededError(PuzzleError):
    pass


class PuzzleIdempotencyConflictError(PuzzleError):
    pass


class PuzzleDependencyUnavailableError(PuzzleError):
    pass


class PuzzleProviderUnavailableError(PuzzleError):
    pass


class PuzzleTimeoutError(PuzzleError):
    pass


class PuzzleTransportError(PuzzleError):
    pass


ERROR_CLASS_BY_CODE: dict[ApiErrorCode, type[PuzzleError]] = {
    ApiErrorCode.AUTHENTICATION_FAILED: PuzzleAuthenticationError,
    ApiErrorCode.AUTHORIZATION_FAILED: PuzzleAuthorizationError,
    ApiErrorCode.VALIDATION_FAILED: PuzzleValidationError,
    ApiErrorCode.RATE_LIMITED: PuzzleRateLimitError,
    ApiErrorCode.QUOTA_EXCEEDED: PuzzleQuotaExceededError,
    ApiErrorCode.IDEMPOTENCY_CONFLICT: PuzzleIdempotencyConflictError,
    ApiErrorCode.DEPENDENCY_UNAVAILABLE: PuzzleDependencyUnavailableError,
    ApiErrorCode.PROVIDER_UNAVAILABLE: PuzzleProviderUnavailableError,
    ApiErrorCode.TIMEOUT: PuzzleTimeoutError,
}
