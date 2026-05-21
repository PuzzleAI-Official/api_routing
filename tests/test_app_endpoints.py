from __future__ import annotations

from collections.abc import Generator

from fastapi.testclient import TestClient
from puzzle_gateway.app import app as fastapi_app
from puzzle_gateway.config import settings
from puzzle_gateway.db import get_session
from puzzle_gateway.models import Base
from puzzle_gateway.seed import seed_default_tenant
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

HTTP_OK = 200
HTTP_NOT_FOUND = 404


def test_health_version_and_mock_run_endpoint() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as seed_session:
        _tenant, api_key = seed_default_tenant(seed_session)
        seed_session.commit()

    def override_session() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(fastapi_app)
        assert client.get("/healthz").json() == {"status": "ok"}
        assert "version" in client.get("/version").json()

        response = client.post(
            "/v1/core/mock:run",
            headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "endpoint-1"},
            json={"payload": {"ok": True}, "strategy": "balanced"},
        )
        assert response.status_code == HTTP_OK
        payload = response.json()
        assert payload["result"]["normalized"]["schema_version"] == "core.mock.v1"
        assert payload["replayed"] is False

        replay = client.post(
            "/v1/core/mock:run",
            headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "endpoint-1"},
            json={"payload": {"ok": True}, "strategy": "balanced"},
        )
        assert replay.status_code == HTTP_OK
        assert replay.json()["replayed"] is True
    finally:
        fastapi_app.dependency_overrides.clear()


def test_admin_endpoint_can_be_disabled() -> None:
    original = settings.admin_api_enabled
    object.__setattr__(settings, "admin_api_enabled", False)
    client = TestClient(fastapi_app)
    try:
        response = client.post(
            "/v1/admin/tenants",
            headers={"X-Admin-Token": "local-admin-token"},
            json={"name": "disabled"},
        )
        assert response.status_code == HTTP_NOT_FOUND
    finally:
        object.__setattr__(settings, "admin_api_enabled", original)


def test_admin_endpoints_create_core_seed_data() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_session() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_session] = override_session
    headers = {"X-Admin-Token": "local-admin-token"}
    try:
        client = TestClient(fastapi_app)
        tenant_response = client.post(
            "/v1/admin/tenants",
            headers=headers,
            json={"name": "admin-created"},
        )
        assert tenant_response.status_code == HTTP_OK
        tenant_id = tenant_response.json()["tenant_id"]

        key_response = client.post(
            "/v1/admin/api-keys",
            headers=headers,
            json={"tenant_id": tenant_id},
        )
        assert key_response.status_code == HTTP_OK
        assert key_response.json()["api_key"].startswith("pzl_live_")

        default_set_response = client.post(
            "/v1/admin/provider-sets/default",
            headers=headers,
            json={"tenant_id": tenant_id},
        )
        assert default_set_response.status_code == HTTP_OK

        provider_response = client.post(
            "/v1/admin/mock-providers",
            headers=headers,
            json={"tenant_id": tenant_id, "provider": "mock-primary", "behavior": "success"},
        )
        assert provider_response.status_code == HTTP_OK
    finally:
        fastapi_app.dependency_overrides.clear()
