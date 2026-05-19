from __future__ import annotations

import httpx
import pytest
from puzzle import Client, PuzzleAuthenticationError


def test_sdk_core_request_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["idempotency-key"] == "sdk-idem"
        return httpx.Response(200, json={"request_id": "req-sdk", "ok": True})

    client = Client("pzl_live_test", base_url="https://api.example.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    try:
        response = client.request(payload={"x": 1}, idempotency_key="sdk-idem")
    finally:
        client.close()

    assert response == {"request_id": "req-sdk", "ok": True}


def test_sdk_maps_canonical_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "authentication_failed",
                    "message": "Invalid API key",
                    "request_id": "req-error",
                }
            },
        )

    client = Client("bad", base_url="https://api.example.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)

    with pytest.raises(PuzzleAuthenticationError) as error:
        client.request(payload={})

    assert error.value.request_id == "req-error"
