from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO, cast

import httpx

from puzzleai._exceptions import ERROR_CLASS_BY_CODE, PuzzleError, PuzzleTransportError
from puzzleai._types import ApiErrorCode, RoutingStrategy

SUCCESS_STATUS_CEILING = 400
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_BASE_URL = "https://api.puzzleai.us"
FileInput = str | Path | bytes | BinaryIO


class _ClientMixin:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
    ) -> None:
        self.api_key = api_key
        configured_base_url = base_url or os.getenv("PUZZLE_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = configured_base_url.rstrip("/")
        self.timeout = timeout or 30.0
        self.max_retries: int = max_retries if max_retries > 0 else 0
        self.retry_backoff: float = retry_backoff if retry_backoff > 0.0 else 0.0

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

    def _can_retry(self, method: str, headers: Mapping[str, str]) -> bool:
        return method.upper() == "GET" or bool(headers.get("Idempotency-Key"))

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                return float(self.retry_backoff * (2**attempt))
        return float(self.retry_backoff * (2**attempt))

    def _raise_transport_error(self, exc: httpx.RequestError) -> None:
        raise PuzzleTransportError(
            "Puzzle API request failed before receiving a response",
            code=ApiErrorCode.INTERNAL_ERROR,
            details={"error": exc.__class__.__name__},
        ) from exc


class Client(_ClientMixin):
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        self.jobs = JobsClient(self)
        self.documents = DocumentsClient(self)

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
        response = self._request_with_retries(
            "POST",
            path,
            json=body,
            headers=self._headers(idempotency_key),
        )
        self._raise_for_error(response)
        return cast(dict[str, Any], response.json())

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.jobs.get(job_id)

    def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        **kwargs: Any,
    ) -> httpx.Response:
        can_retry = self._can_retry(method, headers)
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(method, path, headers=headers, **kwargs)
            except httpx.RequestError as exc:
                if not can_retry or attempt >= self.max_retries:
                    self._raise_transport_error(exc)
                time.sleep(self._retry_delay(None, attempt))
                continue
            if (
                not can_retry
                or response.status_code not in RETRYABLE_STATUS_CODES
                or attempt >= self.max_retries
            ):
                return response
            time.sleep(self._retry_delay(response, attempt))
        msg = "Puzzle API request retry loop exited unexpectedly"
        raise PuzzleTransportError(msg, code=ApiErrorCode.INTERNAL_ERROR)


class JobsClient:
    def __init__(self, parent: Client) -> None:
        self._parent = parent

    def get(self, job_id: str) -> dict[str, Any]:
        response = self._parent._request_with_retries(
            "GET",
            f"/v1/jobs/{job_id}",
            headers=self._parent._headers(None),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())


class AsyncClient(_ClientMixin):
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
        )
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        self.jobs = AsyncJobsClient(self)
        self.documents = AsyncDocumentsClient(self)

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
        response = await self._request_with_retries(
            "POST",
            path,
            json=body,
            headers=self._headers(idempotency_key),
        )
        self._raise_for_error(response)
        return cast(dict[str, Any], response.json())

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self.jobs.get(job_id)

    async def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        **kwargs: Any,
    ) -> httpx.Response:
        can_retry = self._can_retry(method, headers)
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(method, path, headers=headers, **kwargs)
            except httpx.RequestError as exc:
                if not can_retry or attempt >= self.max_retries:
                    self._raise_transport_error(exc)
                await asyncio.sleep(self._retry_delay(None, attempt))
                continue
            if (
                not can_retry
                or response.status_code not in RETRYABLE_STATUS_CODES
                or attempt >= self.max_retries
            ):
                return response
            await asyncio.sleep(self._retry_delay(response, attempt))
        msg = "Puzzle API request retry loop exited unexpectedly"
        raise PuzzleTransportError(msg, code=ApiErrorCode.INTERNAL_ERROR)


class AsyncJobsClient:
    def __init__(self, parent: AsyncClient) -> None:
        self._parent = parent

    async def get(self, job_id: str) -> dict[str, Any]:
        response = await self._parent._request_with_retries(
            "GET",
            f"/v1/jobs/{job_id}",
            headers=self._parent._headers(None),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())


def _file_parts(
    file: FileInput,
    *,
    filename: str | None,
    content_type: str,
) -> tuple[str, bytes, str]:
    if isinstance(file, bytes):
        return filename or "document", file, content_type
    if isinstance(file, str | Path):
        path = Path(file)
        return filename or path.name, path.read_bytes(), content_type
    upload_name = filename or Path(getattr(file, "name", "document")).name
    return upload_name, file.read(), content_type


def _document_metadata(
    *,
    task: str,
    required_capabilities: list[str] | None,
    optional_capabilities: list[str] | None,
    provider: str | None,
    service_id: str | None,
    provider_set: str,
    strategy: RoutingStrategy | str,
    idempotency_key: str | None,
    provider_options: Mapping[str, Any] | None,
) -> str:
    body: dict[str, Any] = {
        "task": task,
        "provider": provider,
        "service_id": service_id,
        "provider_set": provider_set,
        "strategy": str(strategy),
        "idempotency_key": idempotency_key,
        "provider_options": dict(provider_options or {}),
    }
    if required_capabilities is not None:
        body["required_capabilities"] = required_capabilities
    if optional_capabilities is not None:
        body["optional_capabilities"] = optional_capabilities
    return json.dumps(body)


def _invoice_metadata(
    *,
    provider: str | None,
    service_id: str | None,
    provider_set: str,
    strategy: RoutingStrategy | str,
    line_items_mode: str,
    required_fields: list[str] | None,
    idempotency_key: str | None,
    provider_options: Mapping[str, Any] | None,
    features: Mapping[str, Any] | None,
) -> str:
    body: dict[str, Any] = {
        "provider": provider,
        "service_id": service_id,
        "provider_set": provider_set,
        "strategy": str(strategy),
        "line_items_mode": line_items_mode,
        "idempotency_key": idempotency_key,
        "provider_options": dict(provider_options or {}),
        "features": dict(features or {}),
    }
    if required_fields is not None:
        body["required_fields"] = required_fields
    return json.dumps(body)


class InvoicesClient:
    def __init__(self, parent: Client) -> None:
        self._parent = parent

    def extract(
        self,
        *,
        file: FileInput,
        filename: str | None = None,
        content_type: str = "application/pdf",
        provider: str | None = None,
        service_id: str | None = None,
        provider_set: str = "documents-default",
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        line_items_mode: str = "preferred",
        required_fields: list[str] | None = None,
        idempotency_key: str | None = None,
        provider_options: Mapping[str, Any] | None = None,
        features: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_name, data, upload_content_type = _file_parts(
            file,
            filename=filename,
            content_type=content_type,
        )
        response = self._parent._request_with_retries(
            "POST",
            "/v1/documents/invoices:extract",
            data={
                "metadata": _invoice_metadata(
                    provider=provider,
                    service_id=service_id,
                    provider_set=provider_set,
                    strategy=strategy,
                    line_items_mode=line_items_mode,
                    required_fields=required_fields,
                    idempotency_key=idempotency_key,
                    provider_options=provider_options,
                    features=features,
                )
            },
            files={"file": (upload_name, data, upload_content_type)},
            headers=self._parent._headers(idempotency_key),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())

    def submit(
        self,
        *,
        file: FileInput,
        filename: str | None = None,
        content_type: str = "application/pdf",
        provider: str | None = None,
        service_id: str | None = None,
        provider_set: str = "documents-default",
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        line_items_mode: str = "preferred",
        required_fields: list[str] | None = None,
        idempotency_key: str | None = None,
        provider_options: Mapping[str, Any] | None = None,
        features: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_name, data, upload_content_type = _file_parts(
            file,
            filename=filename,
            content_type=content_type,
        )
        response = self._parent._request_with_retries(
            "POST",
            "/v1/documents/invoices:submit",
            data={
                "metadata": _invoice_metadata(
                    provider=provider,
                    service_id=service_id,
                    provider_set=provider_set,
                    strategy=strategy,
                    line_items_mode=line_items_mode,
                    required_fields=required_fields,
                    idempotency_key=idempotency_key,
                    provider_options=provider_options,
                    features=features,
                )
            },
            files={"file": (upload_name, data, upload_content_type)},
            headers=self._parent._headers(idempotency_key),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())


class DocumentsClient:
    def __init__(self, parent: Client) -> None:
        self._parent = parent
        self.invoices = InvoicesClient(parent)

    def process(
        self,
        *,
        file: FileInput,
        filename: str | None = None,
        content_type: str = "application/pdf",
        task: str = "parse",
        required_capabilities: list[str] | None = None,
        optional_capabilities: list[str] | None = None,
        provider: str | None = None,
        service_id: str | None = None,
        provider_set: str = "documents-default",
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        idempotency_key: str | None = None,
        provider_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_name, data, upload_content_type = _file_parts(
            file,
            filename=filename,
            content_type=content_type,
        )
        response = self._parent._request_with_retries(
            "POST",
            "/v1/documents:process",
            data={
                "metadata": _document_metadata(
                    task=task,
                    required_capabilities=required_capabilities,
                    optional_capabilities=optional_capabilities,
                    provider=provider,
                    service_id=service_id,
                    provider_set=provider_set,
                    strategy=strategy,
                    idempotency_key=idempotency_key,
                    provider_options=provider_options,
                )
            },
            files={"file": (upload_name, data, upload_content_type)},
            headers=self._parent._headers(idempotency_key),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())

    def submit(
        self,
        *,
        file: FileInput,
        filename: str | None = None,
        content_type: str = "application/pdf",
        task: str = "parse",
        required_capabilities: list[str] | None = None,
        optional_capabilities: list[str] | None = None,
        provider: str | None = None,
        service_id: str | None = None,
        provider_set: str = "documents-default",
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        idempotency_key: str | None = None,
        provider_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_name, data, upload_content_type = _file_parts(
            file,
            filename=filename,
            content_type=content_type,
        )
        response = self._parent._request_with_retries(
            "POST",
            "/v1/documents:submit",
            data={
                "metadata": _document_metadata(
                    task=task,
                    required_capabilities=required_capabilities,
                    optional_capabilities=optional_capabilities,
                    provider=provider,
                    service_id=service_id,
                    provider_set=provider_set,
                    strategy=strategy,
                    idempotency_key=idempotency_key,
                    provider_options=provider_options,
                )
            },
            files={"file": (upload_name, data, upload_content_type)},
            headers=self._parent._headers(idempotency_key),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())


class AsyncInvoicesClient:
    def __init__(self, parent: AsyncClient) -> None:
        self._parent = parent

    async def extract(
        self,
        *,
        file: FileInput,
        filename: str | None = None,
        content_type: str = "application/pdf",
        provider: str | None = None,
        service_id: str | None = None,
        provider_set: str = "documents-default",
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        line_items_mode: str = "preferred",
        required_fields: list[str] | None = None,
        idempotency_key: str | None = None,
        provider_options: Mapping[str, Any] | None = None,
        features: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_name, data, upload_content_type = _file_parts(
            file,
            filename=filename,
            content_type=content_type,
        )
        response = await self._parent._request_with_retries(
            "POST",
            "/v1/documents/invoices:extract",
            data={
                "metadata": _invoice_metadata(
                    provider=provider,
                    service_id=service_id,
                    provider_set=provider_set,
                    strategy=strategy,
                    line_items_mode=line_items_mode,
                    required_fields=required_fields,
                    idempotency_key=idempotency_key,
                    provider_options=provider_options,
                    features=features,
                )
            },
            files={"file": (upload_name, data, upload_content_type)},
            headers=self._parent._headers(idempotency_key),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())

    async def submit(
        self,
        *,
        file: FileInput,
        filename: str | None = None,
        content_type: str = "application/pdf",
        provider: str | None = None,
        service_id: str | None = None,
        provider_set: str = "documents-default",
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        line_items_mode: str = "preferred",
        required_fields: list[str] | None = None,
        idempotency_key: str | None = None,
        provider_options: Mapping[str, Any] | None = None,
        features: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_name, data, upload_content_type = _file_parts(
            file,
            filename=filename,
            content_type=content_type,
        )
        response = await self._parent._request_with_retries(
            "POST",
            "/v1/documents/invoices:submit",
            data={
                "metadata": _invoice_metadata(
                    provider=provider,
                    service_id=service_id,
                    provider_set=provider_set,
                    strategy=strategy,
                    line_items_mode=line_items_mode,
                    required_fields=required_fields,
                    idempotency_key=idempotency_key,
                    provider_options=provider_options,
                    features=features,
                )
            },
            files={"file": (upload_name, data, upload_content_type)},
            headers=self._parent._headers(idempotency_key),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())


class AsyncDocumentsClient:
    def __init__(self, parent: AsyncClient) -> None:
        self._parent = parent
        self.invoices = AsyncInvoicesClient(parent)

    async def process(
        self,
        *,
        file: FileInput,
        filename: str | None = None,
        content_type: str = "application/pdf",
        task: str = "parse",
        required_capabilities: list[str] | None = None,
        optional_capabilities: list[str] | None = None,
        provider: str | None = None,
        service_id: str | None = None,
        provider_set: str = "documents-default",
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        idempotency_key: str | None = None,
        provider_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_name, data, upload_content_type = _file_parts(
            file,
            filename=filename,
            content_type=content_type,
        )
        response = await self._parent._request_with_retries(
            "POST",
            "/v1/documents:process",
            data={
                "metadata": _document_metadata(
                    task=task,
                    required_capabilities=required_capabilities,
                    optional_capabilities=optional_capabilities,
                    provider=provider,
                    service_id=service_id,
                    provider_set=provider_set,
                    strategy=strategy,
                    idempotency_key=idempotency_key,
                    provider_options=provider_options,
                )
            },
            files={"file": (upload_name, data, upload_content_type)},
            headers=self._parent._headers(idempotency_key),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())

    async def submit(
        self,
        *,
        file: FileInput,
        filename: str | None = None,
        content_type: str = "application/pdf",
        task: str = "parse",
        required_capabilities: list[str] | None = None,
        optional_capabilities: list[str] | None = None,
        provider: str | None = None,
        service_id: str | None = None,
        provider_set: str = "documents-default",
        strategy: RoutingStrategy | str = RoutingStrategy.BALANCED,
        idempotency_key: str | None = None,
        provider_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_name, data, upload_content_type = _file_parts(
            file,
            filename=filename,
            content_type=content_type,
        )
        response = await self._parent._request_with_retries(
            "POST",
            "/v1/documents:submit",
            data={
                "metadata": _document_metadata(
                    task=task,
                    required_capabilities=required_capabilities,
                    optional_capabilities=optional_capabilities,
                    provider=provider,
                    service_id=service_id,
                    provider_set=provider_set,
                    strategy=strategy,
                    idempotency_key=idempotency_key,
                    provider_options=provider_options,
                )
            },
            files={"file": (upload_name, data, upload_content_type)},
            headers=self._parent._headers(idempotency_key),
        )
        self._parent._raise_for_error(response)
        return cast(dict[str, Any], response.json())
