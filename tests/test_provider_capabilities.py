from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from puzzle_gateway import cli
from puzzle_gateway.app import app as fastapi_app
from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.db import get_session
from puzzle_gateway.documents.adapters import DocumentProviderError, FakeDocumentAdapter
from puzzle_gateway.documents.manifests import (
    create_documents_provider_set,
    fake_document_provider_spec,
    upsert_provider_service_manifest,
)
from puzzle_gateway.documents.registry_import import import_provider_registry
from puzzle_gateway.documents.routing import create_document_routing_decision
from puzzle_gateway.documents.validation import (
    choose_sample_file,
    list_provider_service_statuses,
    validate_provider_service,
)
from puzzle_gateway.documents.workflow_priors import upsert_workflow_prior
from puzzle_gateway.errors import ProviderUnavailableError, ValidationError
from puzzle_gateway.kv import InMemoryKVStore
from puzzle_gateway.models import (
    AuditLogEntry,
    Base,
    ProviderServiceManifest,
    ProviderServiceValidationHistory,
)
from puzzle_gateway.seed import create_tenant
from puzzle_gateway.storage import LocalObjectStore
from puzzle_shared import (
    DocumentProcessRequest,
    ProviderServiceManifestSpec,
    WorkflowProviderPriorSpec,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

HTTP_OK = 200


def _factory() -> sessionmaker[Session]:
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


def _sample_pdf(tmp_path: Path) -> Path:
    sample = tmp_path / "tiny.pdf"
    sample.write_bytes(b"%PDF-1.4\n%%EOF")
    return sample


def test_validation_writes_verified_capabilities_history_and_audit(
    tmp_path: Path,
    session: Session,
) -> None:
    tenant = create_tenant(session, name="validation-tenant")
    spec = fake_document_provider_spec(provider_id="fake-doc-verified", behavior="success")
    upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    session.commit()

    result = validate_provider_service(
        session,
        tenant_id=tenant.id,
        provider_id="fake-doc-verified",
        service_id="parse",
        sample_path=_sample_pdf(tmp_path),
        object_store=LocalObjectStore(tmp_path / "objects"),
        write=True,
        check_async=True,
    )
    session.commit()

    assert result.status == "succeeded"
    assert {
        "documents.text",
        "documents.fields",
        "documents.tables",
        "documents.async",
    }.issubset(result.verified_capabilities)
    assert "Parsed tiny.pdf" not in json.dumps(result.summary)
    row = session.scalars(select(ProviderServiceManifest)).one()
    assert row.readiness_status == "verified"
    assert row.validation_status == "succeeded"
    assert "documents.text" in row.verified_capabilities_json
    assert row.capability_status_json["documents.tables"] == "verified"
    history = session.scalars(select(ProviderServiceValidationHistory)).one()
    assert history.raw_result_ref
    assert len(session.scalars(select(AuditLogEntry)).all()) == 1

    status = list_provider_service_statuses(session, tenant_id=tenant.id)[0]
    assert status.verified_capabilities == row.verified_capabilities_json
    assert status.last_validation_summary is not None
    assert status.last_validation_summary["field_count"] > 0


def test_validation_marks_unobserved_capability_as_failed(
    tmp_path: Path,
    session: Session,
) -> None:
    tenant = create_tenant(session, name="validation-warning-tenant")
    spec = fake_document_provider_spec(provider_id="fake-doc-text-only", behavior="text_only")
    upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    session.commit()

    result = validate_provider_service(
        session,
        tenant_id=tenant.id,
        provider_id="fake-doc-text-only",
        service_id="parse",
        sample_path=_sample_pdf(tmp_path),
        object_store=LocalObjectStore(tmp_path / "objects"),
        write=True,
    )
    session.commit()

    assert result.status == "warning"
    assert result.error_code == "capability_not_observed"
    assert result.verified_capabilities == ["documents.text"]
    row = session.scalars(select(ProviderServiceManifest)).one()
    assert row.capability_status_json["documents.tables"] == "failed"
    assert row.verified_capabilities_json == ["documents.text"]


def test_async_validation_failure_preserves_sync_capabilities(
    tmp_path: Path,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = create_tenant(session, name="validation-async-warning-tenant")
    spec = fake_document_provider_spec(provider_id="fake-doc-async-warning", behavior="success")
    upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    session.commit()

    def fail_async(*_args: object, **_kwargs: object) -> str:
        raise DocumentProviderError("Async endpoint failed", error_code="server")

    monkeypatch.setattr(FakeDocumentAdapter, "submit_async", fail_async)

    result = validate_provider_service(
        session,
        tenant_id=tenant.id,
        provider_id="fake-doc-async-warning",
        service_id="parse",
        sample_path=_sample_pdf(tmp_path),
        object_store=LocalObjectStore(tmp_path / "objects"),
        write=True,
        check_async=True,
    )
    session.commit()

    assert result.status == "warning"
    assert result.error_code == "async_server"
    assert "documents.text" in result.verified_capabilities
    assert "documents.fields" in result.verified_capabilities
    assert "documents.tables" in result.verified_capabilities
    assert "documents.async" not in result.verified_capabilities
    row = session.scalars(select(ProviderServiceManifest)).one()
    assert row.capability_status_json["documents.text"] == "verified"
    assert row.capability_status_json["documents.async"] == "failed"


def test_validation_missing_credentials_writes_failed_status(
    tmp_path: Path,
    session: Session,
) -> None:
    tenant = create_tenant(session, name="validation-missing-credential-tenant")
    spec = ProviderServiceManifestSpec(
        provider_id="fake-doc-credentialed",
        service_id="parse",
        display_name="Fake Credentialed",
        capabilities=["documents.text", "documents.fields"],
        supported_mime_types=["application/pdf"],
        max_sync_bytes=1024 * 1024,
        max_async_bytes=1024 * 1024,
        supports_sync=True,
        supports_async=True,
        credential_schema={"required": ["api_key"]},
        service_config={"async_execution_mode": "worker_sync"},
        capability_status={
            "documents.text": "configured",
            "documents.fields": "configured",
        },
    )
    upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    session.commit()

    result = validate_provider_service(
        session,
        tenant_id=tenant.id,
        provider_id="fake-doc-credentialed",
        service_id="parse",
        sample_path=_sample_pdf(tmp_path),
        object_store=LocalObjectStore(tmp_path / "objects"),
        write=True,
    )
    session.commit()

    assert result.status == "failed"
    assert result.error_code == "credential_missing"
    row = session.scalars(select(ProviderServiceManifest)).one()
    assert row.validation_status == "failed"
    assert row.capability_status_json["documents.text"] == "failed"


def test_verified_mode_routing_requires_verified_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
) -> None:
    tenant = create_tenant(session, name="verified-routing-tenant")
    spec = fake_document_provider_spec(provider_id="fake-doc-route")
    row = upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    create_documents_provider_set(session, tenant_id=tenant.id, specs=[spec])
    session.commit()

    monkeypatch.setenv("PUZZLE_DOCUMENT_CAPABILITY_MODE", "verified")
    with pytest.raises(ProviderUnavailableError):
        create_document_routing_decision(
            session,
            tenant_id=tenant.id,
            request=DocumentProcessRequest(provider_set="documents-default"),
            request_id="verified-miss",
            mime_type="application/pdf",
            byte_size=100,
            circuit_breaker=CircuitBreaker(InMemoryKVStore()),
            sync=True,
        )

    row.verified_capabilities_json = ["documents.text"]
    session.add(row)
    session.commit()
    decision = create_document_routing_decision(
        session,
        tenant_id=tenant.id,
        request=DocumentProcessRequest(provider_set="documents-default"),
        request_id="verified-hit",
        mime_type="application/pdf",
        byte_size=100,
        circuit_breaker=CircuitBreaker(InMemoryKVStore()),
        sync=True,
    )
    assert decision.decision_json["constraints"]["capability_mode"] == "verified"
    assert decision.decision_json["ordered_provider_services"][0]["provider_id"] == "fake-doc-route"

    monkeypatch.setenv("PUZZLE_DOCUMENT_CAPABILITY_MODE", "configured")
    row.verified_capabilities_json = []
    session.add(row)
    session.commit()
    configured = create_document_routing_decision(
        session,
        tenant_id=tenant.id,
        request=DocumentProcessRequest(provider_set="documents-default"),
        request_id="configured-hit",
        mime_type="application/pdf",
        byte_size=100,
        circuit_breaker=CircuitBreaker(InMemoryKVStore()),
        sync=True,
    )
    assert configured.decision_json["constraints"]["capability_mode"] == "configured"


def test_registry_import_starts_configured_not_verified(
    tmp_path: Path,
    session: Session,
) -> None:
    tenant = create_tenant(session, name="registry-capability-tenant")
    registry_path = tmp_path / "provider_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "providers": {
                    "mindee": {"env_vars": {"MINDEE_API_KEY": "mindee-secret"}},
                    "verify": {
                        "env_vars": {
                            "VERYFI_CLIENT_ID": "client",
                            "VERYFI_CLIENT_SECRET": "secret",
                            "VERYFI_USERNAME": "user",
                            "VERYFI_API_KEY": "key",
                        }
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
        providers=["mindee", "verify"],
    )
    session.commit()

    assert imported == ["mindee", "veryfi"]
    rows = {
        row.provider_id: row
        for row in session.scalars(select(ProviderServiceManifest)).all()
    }
    assert rows["mindee"].verified_capabilities_json == []
    assert rows["mindee"].validation_status == "warning"
    assert rows["mindee"].validation_error_code == "provider_config_missing"
    assert rows["veryfi"].validation_status == "not_validated"


def test_admin_provider_service_status_endpoints_do_not_leak_secrets(
    tmp_path: Path,
) -> None:
    factory = _factory()
    with factory() as session:
        tenant = create_tenant(session, name="admin-capability-tenant")
        spec = fake_document_provider_spec(provider_id="fake-doc-admin")
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
        validate_provider_service(
            session,
            tenant_id=tenant.id,
            provider_id="fake-doc-admin",
            service_id="parse",
            sample_path=_sample_pdf(tmp_path),
            object_store=LocalObjectStore(tmp_path / "objects"),
            write=True,
        )
        session.commit()
        tenant_id = tenant.id

    fastapi_app.dependency_overrides[get_session] = _session_override(factory)
    try:
        client = TestClient(fastapi_app)
        list_response = client.get(
            f"/v1/admin/tenants/{tenant_id}/provider-services",
            headers={"X-Admin-Token": "local-admin-token"},
        )
        assert list_response.status_code == HTTP_OK
        payload = list_response.json()
        assert payload[0]["provider_id"] == "fake-doc-admin"
        assert "verified_capabilities" in payload[0]
        assert "secret" not in json.dumps(payload).lower()
        get_response = client.get(
            f"/v1/admin/tenants/{tenant_id}/provider-services/fake-doc-admin/parse",
            headers={"X-Admin-Token": "local-admin-token"},
        )
        assert get_response.status_code == HTTP_OK
        assert get_response.json()["last_validation_summary"]["has_text"] is True
    finally:
        fastapi_app.dependency_overrides.clear()


def test_admin_workflow_provider_services_show_active_and_inactive_status() -> None:
    factory = _factory()
    with factory() as session:
        tenant = create_tenant(session, name="admin-workflow-tenant")
        active = fake_document_provider_spec(provider_id="fake-doc-admin-workflow-active")
        inactive = fake_document_provider_spec(provider_id="fake-doc-admin-workflow-inactive")
        for spec in [active, inactive]:
            upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
        create_documents_provider_set(session, tenant_id=tenant.id, specs=[active, inactive])
        upsert_workflow_prior(
            session,
            tenant_id=tenant.id,
            spec=WorkflowProviderPriorSpec(
                provider_id="fake-doc-admin-workflow-active",
                service_id="parse",
                active=True,
                quality_prior=0.8,
            ),
        )
        upsert_workflow_prior(
            session,
            tenant_id=tenant.id,
            spec=WorkflowProviderPriorSpec(
                provider_id="fake-doc-admin-workflow-inactive",
                service_id="parse",
                active=False,
                quality_prior=0.9,
            ),
        )
        session.commit()
        tenant_id = tenant.id

    fastapi_app.dependency_overrides[get_session] = _session_override(factory)
    try:
        client = TestClient(fastapi_app)
        response = client.get(
            f"/v1/admin/tenants/{tenant_id}/workflows/invoice.extract/provider-services",
            headers={"X-Admin-Token": "local-admin-token"},
        )
        assert response.status_code == HTTP_OK
        payload = response.json()
        by_provider = {item["provider_id"]: item for item in payload}
        assert by_provider["fake-doc-admin-workflow-active"]["selection_status"] == "eligible"
        assert by_provider["fake-doc-admin-workflow-inactive"]["selection_status"] == "inactive"
        assert "workflow_inactive" in by_provider["fake-doc-admin-workflow-inactive"]["reasons"]
        assert "secret" not in json.dumps(payload).lower()
    finally:
        fastapi_app.dependency_overrides.clear()


def test_cli_provider_capabilities_and_validation_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = _factory()
    with factory() as session:
        tenant = create_tenant(session, name="cli-capability-tenant")
        spec = fake_document_provider_spec(provider_id="fake-doc-cli")
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
        session.commit()
        tenant_id = tenant.id

    monkeypatch.setattr(cli, "SessionLocal", factory)
    monkeypatch.setattr(cli, "get_object_store", lambda: LocalObjectStore(tmp_path / "objects"))

    listed = cli._run_command(
        argparse.Namespace(command="provider-capabilities", tenant_id=tenant_id)
    )
    assert listed is True
    assert "fake-doc-cli" in capsys.readouterr().out

    validated = cli._run_command(
        argparse.Namespace(
            command="validate-provider-service",
            tenant_id=tenant_id,
            provider="fake-doc-cli",
            service="parse",
            sample_path=str(_sample_pdf(tmp_path)),
            write=True,
            check_async=False,
        )
    )
    assert validated is True
    assert '"written": true' in capsys.readouterr().out.lower()


def test_cli_workflow_prior_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = _factory()
    with factory() as session:
        tenant = create_tenant(session, name="cli-workflow-prior-tenant")
        spec = fake_document_provider_spec(provider_id="fake-doc-cli-workflow")
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
        create_documents_provider_set(session, tenant_id=tenant.id, specs=[spec])
        session.commit()
        tenant_id = tenant.id

    monkeypatch.setattr(cli, "SessionLocal", factory)
    monkeypatch.setattr(cli, "get_object_store", lambda: LocalObjectStore(tmp_path / "objects"))

    seeded = cli._run_command(
        argparse.Namespace(command="seed-invoice-workflow-priors", tenant_id=tenant_id)
    )
    assert seeded is True
    assert '"provider_id": "klippa"' in capsys.readouterr().out

    upserted = cli._run_command(
        argparse.Namespace(
            command="upsert-workflow-prior",
            tenant_id=tenant_id,
            workflow="invoice.extract",
            provider="fake-doc-cli-workflow",
            service="parse",
            quality_prior=0.88,
            fallback_priority=3,
            inactive=False,
            notes="test prior",
            metadata="{}",
        )
    )
    assert upserted is True
    assert '"quality_prior": 0.88' in capsys.readouterr().out

    listed = cli._run_command(
        argparse.Namespace(
            command="workflow-priors",
            tenant_id=tenant_id,
            workflow="invoice.extract",
        )
    )
    assert listed is True
    assert "fake-doc-cli-workflow" in capsys.readouterr().out

    visible = cli._run_command(
        argparse.Namespace(
            command="workflow-provider-services",
            tenant_id=tenant_id,
            workflow="invoice.extract",
            provider_set="documents-default",
            line_items_mode="preferred",
        )
    )
    assert visible is True
    assert '"selection_status": "eligible"' in capsys.readouterr().out


def test_cli_import_and_validate_document_providers_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = _factory()
    with factory() as session:
        tenant = create_tenant(session, name="cli-many-capability-tenant")
        spec = fake_document_provider_spec(provider_id="fake-doc-many")
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
        session.commit()
        tenant_id = tenant.id

    registry_path = tmp_path / "provider_registry.json"
    registry_path.write_text(
        json.dumps({"providers": {"Klippa": {"env_vars": {"KLIPPA_API_KEY": "secret"}}}}),
        encoding="utf-8",
    )
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    _sample_pdf(sample_dir)
    monkeypatch.setattr(cli, "SessionLocal", factory)
    monkeypatch.setattr(cli, "get_object_store", lambda: LocalObjectStore(tmp_path / "objects"))
    monkeypatch.setitem(cli.PHASE2_SERVICE_IDS, "fake-doc-many", "parse")

    imported = cli._run_command(
        argparse.Namespace(
            command="import-provider-registry",
            tenant_id=tenant_id,
            path=str(registry_path),
            providers="Klippa",
        )
    )
    assert imported is True
    assert "imported_providers=klippa" in capsys.readouterr().out

    validated = cli._run_command(
        argparse.Namespace(
            command="validate-document-providers",
            tenant_id=tenant_id,
            sample_dir=str(sample_dir),
            providers="fake-doc-many",
            write=False,
            check_async=False,
        )
    )
    assert validated is True
    assert '"provider_id": "fake-doc-many"' in capsys.readouterr().out
    assert cli._run_command(argparse.Namespace(command="unknown")) is False


def test_choose_sample_file_rejects_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="supported document"):
        choose_sample_file(tmp_path)
