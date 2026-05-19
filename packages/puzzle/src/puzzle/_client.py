from __future__ import annotations

from typing import Any, cast

import httpx
from puzzle_shared import ApiErrorCode, RoutingStrategy

from puzzle._exceptions import ERROR_CLASS_BY_CODE, PuzzleError

SUCCESS_STATUS_CEILING = 400


class _ClientMixin:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or "http://localhost:8000").rstrip("/")
        self.timeout = timeout or 30.0

    def _headers(self, idempotency_key: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.status_code < SUCCESS_STATUS_CEILING:
            return
        request_id = response.headers.get("x-request-id")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PuzzleError(
                f"Puzzle API request failed with status {response.status_code}",
                request_id=request_id,
            ) from exc
        error = payload.get("error", payload)
        raw_code = error.get("code", ApiErrorCode.INTERNAL_ERROR)
        try:
            code = ApiErrorCode(raw_code)
        except ValueError:
            code = ApiErrorCode.INTERNAL_ERROR
        error_class = ERROR_CLASS_BY_CODE.get(code, PuzzleError)
        raise error_class(
            error.get("message", "Puzzle API request failed"),
            code=code,
            request_id=error.get("request_id", request_id),
            details=error.get("details", {}),
        )


class Client(_ClientMixin):
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout)
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def request(
        self,
        *,
        payload: dict[str, Any] | None = None,
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        provider: str | None = None,
        idempotency_key: str | None = None,
        submit: bool = False,
    ) -> dict[str, Any]:
        path = "/v1/core/mock:submit" if submit else "/v1/core/mock:run"
        body: dict[str, Any] = {
            "payload": payload or {},
            "strategy": str(strategy),
            "provider": provider,
            "idempotency_key": idempotency_key,
        }
        response = self._client.post(path, json=body, headers=self._headers(idempotency_key))
        self._raise_for_error(response)
        return cast(dict[str, Any], response.json())

    def get_job(self, job_id: str) -> dict[str, Any]:
        response = self._client.get(f"/v1/jobs/{job_id}", headers=self._headers(None))
        self._raise_for_error(response)
        return cast(dict[str, Any], response.json())


class AsyncClient(_ClientMixin):
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout)
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def request(
        self,
        *,
        payload: dict[str, Any] | None = None,
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        provider: str | None = None,
        idempotency_key: str | None = None,
        submit: bool = False,
    ) -> dict[str, Any]:
        path = "/v1/core/mock:submit" if submit else "/v1/core/mock:run"
        body: dict[str, Any] = {
            "payload": payload or {},
            "strategy": str(strategy),
            "provider": provider,
            "idempotency_key": idempotency_key,
        }
        response = await self._client.post(path, json=body, headers=self._headers(idempotency_key))
        self._raise_for_error(response)
        return cast(dict[str, Any], response.json())

    async def get_job(self, job_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/jobs/{job_id}", headers=self._headers(None))
        self._raise_for_error(response)
        return cast(dict[str, Any], response.json())
