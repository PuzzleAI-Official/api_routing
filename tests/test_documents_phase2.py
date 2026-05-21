from __future__ import annotations

import json
from collections.abc import Callable, Generator
from pathlib import Path

from fastapi.testclient import TestClient
from puzzle_gateway.app import app as fastapi_app
from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.config import settings
from puzzle_gateway.db import get_session
from puzzle_gateway.documents.adapters import VeryfiDocumentsAdapter
from puzzle_gateway.documents.manifests import (
    DOCUMENT_OPERATION,
    create_documents_provider_set,
    fake_document_provider_spec,
    phase2_provider_specs,
    upsert_provider_service_manifest,
)
from puzzle_gateway.documents.registry_import import import_provider_registry
from puzzle_gateway.documents.routing import create_document_routing_decision
from puzzle_gateway.jobs import process_job_once
from puzzle_gateway.kv import InMemoryKVStore
from puzzle_gateway.models import AuditLogEntry, Base, ProviderAttempt, ProviderCredential
from puzzle_gateway.provider_sets import upsert_provider_set
from puzzle_gateway.seed import create_api_key, create_tenant
from puzzle_gateway.vault import Vault
from puzzle_shared import DocumentProcessRequest, ProviderSetSpec
from puzzle_shared.schemas import ProviderSpec
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

HTTP_OK = 200
EXPECTED_IMPORTED_CREDENTIALS = 2
MIN_AUDIT_ENTRIES = 3


def _app_session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _session_override(
    factory: sessionmaker[Session],
) -> Callable[[], Generator[Session, None, None]]:
    def override() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    return override


def _seed_document_tenant(
    factory: sessionmaker[Session],
    *,
    behaviors: list[str],
) -> tuple[str, str]:
    with factory() as session:
        tenant = create_tenant(session, name="doc-tenant")
        _key, api_key = create_api_key(session, tenant_id=tenant.id)
        specs = [
            fake_document_provider_spec(
                provider_id=f"fake-doc-{index}",
                behavior=behavior,
                quality_score=90 - index,
                cost_units=5 + index,
            )
            for index, behavior in enumerate(behaviors, start=1)
        ]
        for spec in specs:
            upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
        create_documents_provider_set(session, tenant_id=tenant.id, specs=specs)
        session.commit()
        return tenant.id, api_key


def test_sync_document_process_and_idempotency_replay(tmp_path: Path) -> None:
    original_root = settings.object_store_root
    object.__setattr__(settings, "object_store_root", str(tmp_path))
    factory = _app_session_factory()
    _tenant_id, api_key = _seed_document_tenant(factory, behaviors=["success"])
    fastapi_app.dependency_overrides[get_session] = _session_override(factory)
    try:
        client = TestClient(fastapi_app)
        metadata = {
            "provider": "fake-doc-1",
            "service_id": "parse",
            "provider_set": "documents-default",
        }
        response = client.post(
            "/v1/documents:process",
            headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "doc-sync-1"},
            data={"metadata": json.dumps(metadata)},
            files={"file": ("tiny.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
        assert response.status_code == HTTP_OK
        payload = response.json()
        assert payload["schema_version"] == "documents.process.v1"
        assert payload["provider_id"] == "fake-doc-1"
        assert payload["service_id"] == "parse"
        assert payload["full_text"]
        assert payload["replayed"] is False

        replay = client.post(
            "/v1/documents:process",
            headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "doc-sync-1"},
            data={"metadata": json.dumps(metadata)},
            files={"file": ("tiny.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
        assert replay.status_code == HTTP_OK
        assert replay.json()["replayed"] is True
        with factory() as session:
            attempts = session.scalars(select(ProviderAttempt)).all()
            assert len(attempts) == 1
    finally:
        fastapi_app.dependency_overrides.clear()
        object.__setattr__(settings, "object_store_root", original_root)


def test_document_fallback_records_failed_primary_and_successful_secondary(tmp_path: Path) -> None:
    original_root = settings.object_store_root
    object.__setattr__(settings, "object_store_root", str(tmp_path))
    factory = _app_session_factory()
    _tenant_id, api_key = _seed_document_tenant(factory, behaviors=["timeout", "success"])
    fastapi_app.dependency_overrides[get_session] = _session_override(factory)
    try:
        client = TestClient(fastapi_app)
        response = client.post(
            "/v1/documents:process",
            headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "doc-fallback"},
            data={"metadata": json.dumps({"provider_set": "documents-default"})},
            files={"file": ("tiny.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
        assert response.status_code == HTTP_OK
        assert response.json()["provider_id"] == "fake-doc-2"
        with factory() as session:
            attempts = session.scalars(
                select(ProviderAttempt).order_by(ProviderAttempt.created_at)
            ).all()
            assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
            assert attempts[0].error_code == "timeout"
            assert attempts[1].service_id == "parse"
    finally:
        fastapi_app.dependency_overrides.clear()
        object.__setattr__(settings, "object_store_root", original_root)


def test_async_document_submit_completes_via_worker(tmp_path: Path) -> None:
    original_root = settings.object_store_root
    object.__setattr__(settings, "object_store_root", str(tmp_path))
    factory = _app_session_factory()
    _tenant_id, api_key = _seed_document_tenant(factory, behaviors=["success"])
    fastapi_app.dependency_overrides[get_session] = _session_override(factory)
    try:
        client = TestClient(fastapi_app)
        submit = client.post(
            "/v1/documents:submit",
            headers={"Authorization": f"Bearer {api_key}", "Idempotency-Key": "doc-async"},
            data={"metadata": json.dumps({"provider_set": "documents-default"})},
            files={"file": ("tiny.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
        assert submit.status_code == HTTP_OK
        job_id = submit.json()["job_id"]
        with factory() as session:
            job = process_job_once(
                session,
                job_id=job_id,
                circuit_breaker=CircuitBreaker(InMemoryKVStore()),
            )
            session.commit()
            assert job.status == "succeeded"
        job_response = client.get(
            f"/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert job_response.status_code == HTTP_OK
        assert job_response.json()["result"]["schema_version"] == "documents.process.v1"
    finally:
        fastapi_app.dependency_overrides.clear()
        object.__setattr__(settings, "object_store_root", original_root)


def test_document_routing_records_skip_reasons(session: Session) -> None:
    tenant = create_tenant(session, name="routing-tenant")
    good = fake_document_provider_spec(provider_id="fake-doc-good")
    disabled = fake_document_provider_spec(provider_id="fake-doc-disabled")
    mindee = phase2_provider_specs()[0]
    for spec in [good, disabled, mindee]:
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    upsert_provider_set(
        session,
        tenant_id=tenant.id,
        spec=ProviderSetSpec(
            name="documents-routing-test",
            providers=[
                ProviderSpec(
                    provider="fake-doc-disabled",
                    service_id="parse",
                    enabled=False,
                    capabilities=[DOCUMENT_OPERATION],
                ),
                ProviderSpec(
                    provider="fake-doc-no-operation",
                    service_id="parse",
                    capabilities=["documents.text"],
                ),
                ProviderSpec(
                    provider="fake-doc-missing-manifest",
                    service_id="parse",
                    capabilities=[DOCUMENT_OPERATION],
                ),
                ProviderSpec(
                    provider="mindee",
                    service_id="model_inference",
                    capabilities=[DOCUMENT_OPERATION],
                ),
                ProviderSpec(
                    provider="fake-doc-good",
                    service_id="parse",
                    capabilities=[DOCUMENT_OPERATION],
                ),
            ],
        ),
    )
    decision = create_document_routing_decision(
        session,
        tenant_id=tenant.id,
        request=DocumentProcessRequest(provider_set="documents-routing-test"),
        request_id="routing-req",
        mime_type="application/pdf",
        byte_size=100,
        circuit_breaker=CircuitBreaker(InMemoryKVStore()),
        sync=True,
    )
    reasons = {item["reason"] for item in decision.decision_json["skipped_provider_services"]}
    assert {
        "provider_disabled",
        "operation_not_allowed",
        "manifest_missing",
        "credential_missing",
    }.issubset(reasons)


def test_registry_import_stores_encrypted_credentials_and_manifests(
    tmp_path: Path,
    session: Session,
) -> None:
    tenant = create_tenant(session, name="registry-tenant")
    registry_path = tmp_path / "provider_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "providers": {
                    "mindee": {
                        "env_vars": {
                            "MINDEE_API_KEY": "mindee-secret",
                            "MINDEE_MODEL_ID": "model-1",
                        },
                        "monthly_limit": 250,
                    },
                    "Klippa": {
                        "env_vars": {"KLIPPA_API_KEY": "klippa-secret"},
                        "monthly_limit": 100,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    imported = import_provider_registry(
        session,
        tenant_id=tenant.id,
        path=registry_path,
        providers=["mindee", "klippa"],
    )
    session.commit()

    assert imported == ["mindee", "klippa"]
    credentials = session.scalars(select(ProviderCredential)).all()
    assert len(credentials) == EXPECTED_IMPORTED_CREDENTIALS
    assert all("secret" not in credential.ciphertext for credential in credentials)
    stored = Vault().retrieve(
        session,
        tenant_id=tenant.id,
        credential_id=credentials[0].id,
        actor="test",
    )
    assert "secret" in stored
    audit_count = len(session.scalars(select(AuditLogEntry)).all())
    assert audit_count >= MIN_AUDIT_ENTRIES


def test_veryfi_auth_headers_do_not_embed_client_secret() -> None:
    adapter = VeryfiDocumentsAdapter()
    headers = adapter.build_headers(
        credentials={
            "client_id": "client",
            "client_secret": "super-secret",
            "username": "user",
            "api_key": "api-key",
        },
        payload={"file_name": "tiny.pdf"},
    )
    assert headers["CLIENT-ID"] == "client"
    assert headers["AUTHORIZATION"] == "apikey user:api-key"
    assert headers["X-VERYFI-REQUEST-SIGNATURE"]
    assert "super-secret" not in json.dumps(headers)
