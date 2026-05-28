from __future__ import annotations

import json
from collections.abc import Callable, Generator
from pathlib import Path

from fastapi.testclient import TestClient
from puzzle_gateway.app import app as fastapi_app
from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.config import settings
from puzzle_gateway.db import get_session
from puzzle_gateway.documents.adapters import DocumentAdapterResult
from puzzle_gateway.documents.manifests import (
    create_documents_provider_set,
    fake_document_provider_spec,
    upsert_provider_service_manifest,
)
from puzzle_gateway.documents.workflow_priors import (
    seed_invoice_workflow_priors,
    upsert_workflow_prior,
    workflow_provider_service_statuses,
)
from puzzle_gateway.documents.workflows import (
    INVOICE_OPERATION,
    create_invoice_routing_decision,
    evaluate_invoice_quality,
    invoice_document_request,
    normalize_invoice_result,
)
from puzzle_gateway.jobs import process_job_once
from puzzle_gateway.kv import InMemoryKVStore
from puzzle_gateway.models import (
    Base,
    BillingLedgerEntry,
    DocumentWorkflowResult,
    ProviderAttempt,
)
from puzzle_gateway.seed import create_api_key, create_tenant
from puzzle_shared import (
    DocumentCapability,
    DocumentPage,
    InvoiceExtractRequest,
    InvoiceLineItemsMode,
    WorkflowProviderPriorSpec,
)
from pytest import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

HTTP_OK = 200


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


def _seed_invoice_tenant(
    factory: sessionmaker[Session],
    *,
    behaviors: list[str],
) -> tuple[str, str]:
    with factory() as session:
        tenant = create_tenant(session, name="invoice-tenant")
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


def test_invoice_policy_derives_capabilities_from_line_item_mode() -> None:
    required = invoice_document_request(
        InvoiceExtractRequest(line_items_mode=InvoiceLineItemsMode.REQUIRED)
    )
    preferred = invoice_document_request(
        InvoiceExtractRequest(line_items_mode=InvoiceLineItemsMode.PREFERRED)
    )
    disabled = invoice_document_request(
        InvoiceExtractRequest(line_items_mode=InvoiceLineItemsMode.DISABLED)
    )

    assert required.required_capabilities == [
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.TABLES.value,
    ]
    assert preferred.required_capabilities == [
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
    ]
    assert preferred.optional_capabilities == [DocumentCapability.TABLES.value]
    assert disabled.optional_capabilities == []


def test_invoice_normalizer_maps_nanonets_invoice_aliases() -> None:
    invoice, warnings = normalize_invoice_result(
        DocumentAdapterResult(
            provider_request_id="nanonets",
            raw={},
            full_text="invoice",
            pages=[DocumentPage(page_number=1, text="invoice")],
            fields={
                "seller_name": "Acme",
                "buyer_name": "Buyer Co",
                "invoice_amount": "USD 108.25",
                "total_tax": "USD 8.25",
                "payment_due_date": "2026-05-31",
            },
        ),
        request=InvoiceExtractRequest(),
    )
    quality = evaluate_invoice_quality(
        invoice,
        request=InvoiceExtractRequest(),
        warnings=warnings,
    )

    assert invoice.vendor_name.value == "Acme"
    assert invoice.customer_name.value == "Buyer Co"
    assert invoice.total.value == "108.25"
    assert invoice.tax.value == "8.25"
    assert invoice.due_date.value == "2026-05-31"
    assert quality.accepted is True


def test_invoice_verified_routing_skips_table_unverified_service(
    monkeypatch: MonkeyPatch,
    session: Session,
) -> None:
    monkeypatch.setenv("PUZZLE_DOCUMENT_CAPABILITY_MODE", "verified")
    tenant = create_tenant(session, name="verified-invoice-tenant")
    no_tables = fake_document_provider_spec(
        provider_id="fake-doc-no-tables",
        behavior="invoice",
        quality_score=99,
    )
    no_tables.verified_capabilities = [
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
    ]
    with_tables = fake_document_provider_spec(
        provider_id="fake-doc-with-tables",
        behavior="invoice",
        quality_score=70,
    )
    with_tables.verified_capabilities = [
        DocumentCapability.TEXT.value,
        DocumentCapability.FIELDS.value,
        DocumentCapability.TABLES.value,
    ]
    for spec in [no_tables, with_tables]:
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    create_documents_provider_set(session, tenant_id=tenant.id, specs=[no_tables, with_tables])

    decision = create_invoice_routing_decision(
        session,
        tenant_id=tenant.id,
        request=InvoiceExtractRequest(line_items_mode=InvoiceLineItemsMode.REQUIRED),
        request_id="invoice-routing",
        mime_type="application/pdf",
        byte_size=100,
        circuit_breaker=CircuitBreaker(InMemoryKVStore()),
        sync=True,
    )

    assert decision.operation == INVOICE_OPERATION
    assert decision.decision_json["ordered_provider_services"] == [
        {"provider_id": "fake-doc-with-tables", "service_id": "parse"}
    ]
    assert {
        item["reason"] for item in decision.decision_json["skipped_provider_services"]
    } >= {"capability_not_verified"}


def test_invoice_workflow_priors_are_seeded_and_can_deactivate_service(
    session: Session,
) -> None:
    tenant = create_tenant(session, name="workflow-prior-tenant")
    primary = fake_document_provider_spec(provider_id="fake-doc-prior-primary", behavior="invoice")
    secondary = fake_document_provider_spec(
        provider_id="fake-doc-prior-secondary",
        behavior="invoice",
    )
    for spec in [primary, secondary]:
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    create_documents_provider_set(session, tenant_id=tenant.id, specs=[primary, secondary])
    seed_invoice_workflow_priors(session, tenant_id=tenant.id)
    upsert_workflow_prior(
        session,
        tenant_id=tenant.id,
        spec=WorkflowProviderPriorSpec(
            provider_id="fake-doc-prior-primary",
            service_id="parse",
            active=False,
            quality_prior=0.99,
            fallback_priority=1,
        ),
    )
    upsert_workflow_prior(
        session,
        tenant_id=tenant.id,
        spec=WorkflowProviderPriorSpec(
            provider_id="fake-doc-prior-secondary",
            service_id="parse",
            active=True,
            quality_prior=0.70,
            fallback_priority=2,
        ),
    )

    decision = create_invoice_routing_decision(
        session,
        tenant_id=tenant.id,
        request=InvoiceExtractRequest(provider_set="documents-default"),
        request_id="invoice-prior",
        mime_type="application/pdf",
        byte_size=100,
        circuit_breaker=CircuitBreaker(InMemoryKVStore()),
        sync=True,
    )

    assert decision.decision_json["ordered_provider_services"] == [
        {"provider_id": "fake-doc-prior-secondary", "service_id": "parse"}
    ]
    assert {
        item["reason"] for item in decision.decision_json["skipped_provider_services"]
    } >= {"workflow_inactive"}


def test_invoice_routing_records_invoice_circuit_open_skip(
    session: Session,
) -> None:
    tenant = create_tenant(session, name="invoice-circuit-tenant")
    primary = fake_document_provider_spec(
        provider_id="fake-doc-circuit-primary",
        behavior="invoice",
        quality_score=99,
    )
    secondary = fake_document_provider_spec(
        provider_id="fake-doc-circuit-secondary",
        behavior="invoice",
        quality_score=70,
    )
    for spec in [primary, secondary]:
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    create_documents_provider_set(session, tenant_id=tenant.id, specs=[primary, secondary])
    breaker = CircuitBreaker(InMemoryKVStore())
    for _ in range(3):
        breaker.record_failure(
            tenant.id,
            "fake-doc-circuit-primary:parse",
            INVOICE_OPERATION,
        )

    decision = create_invoice_routing_decision(
        session,
        tenant_id=tenant.id,
        request=InvoiceExtractRequest(provider_set="documents-default"),
        request_id="invoice-circuit",
        mime_type="application/pdf",
        byte_size=100,
        circuit_breaker=breaker,
        sync=True,
    )

    assert decision.decision_json["ordered_provider_services"] == [
        {"provider_id": "fake-doc-circuit-secondary", "service_id": "parse"}
    ]
    assert {
        (item["provider_id"], item["reason"])
        for item in decision.decision_json["skipped_provider_services"]
    } >= {("fake-doc-circuit-primary", "circuit_open")}


def test_workflow_provider_service_status_shows_invoice_eligibility(
    session: Session,
) -> None:
    tenant = create_tenant(session, name="workflow-status-tenant")
    active = fake_document_provider_spec(provider_id="fake-doc-status-active", behavior="invoice")
    inactive = fake_document_provider_spec(
        provider_id="fake-doc-status-inactive",
        behavior="invoice",
    )
    for spec in [active, inactive]:
        spec.verified_capabilities = [
            DocumentCapability.TEXT.value,
            DocumentCapability.FIELDS.value,
            DocumentCapability.TABLES.value,
        ]
        upsert_provider_service_manifest(session, tenant_id=tenant.id, spec=spec)
    create_documents_provider_set(session, tenant_id=tenant.id, specs=[active, inactive])
    upsert_workflow_prior(
        session,
        tenant_id=tenant.id,
        spec=WorkflowProviderPriorSpec(
            provider_id="fake-doc-status-active",
            service_id="parse",
            active=True,
            quality_prior=0.8,
            fallback_priority=1,
        ),
    )
    upsert_workflow_prior(
        session,
        tenant_id=tenant.id,
        spec=WorkflowProviderPriorSpec(
            provider_id="fake-doc-status-inactive",
            service_id="parse",
            active=False,
            quality_prior=0.9,
            fallback_priority=2,
        ),
    )

    statuses = workflow_provider_service_statuses(session, tenant_id=tenant.id)
    by_provider = {item.provider_id: item for item in statuses}

    assert by_provider["fake-doc-status-active"].selection_status == "eligible"
    assert by_provider["fake-doc-status-inactive"].selection_status == "inactive"
    assert "workflow_inactive" in by_provider["fake-doc-status-inactive"].reasons


def test_sync_invoice_extract_persists_bills_and_replays(tmp_path: Path) -> None:
    original_root = settings.object_store_root
    object.__setattr__(settings, "object_store_root", str(tmp_path))
    factory = _app_session_factory()
    _tenant_id, api_key = _seed_invoice_tenant(factory, behaviors=["invoice"])
    fastapi_app.dependency_overrides[get_session] = _session_override(factory)
    try:
        client = TestClient(fastapi_app)
        metadata = {"provider": "fake-doc-1", "service_id": "parse"}
        response = client.post(
            "/v1/documents/invoices:extract",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Idempotency-Key": "invoice-sync",
            },
            data={"metadata": json.dumps(metadata)},
            files={"file": ("invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
        assert response.status_code == HTTP_OK
        payload = response.json()
        assert payload["schema_version"] == "documents.invoice.extract.v1"
        assert payload["workflow"] == "invoice.extract"
        assert payload["provider_id"] == "fake-doc-1"
        assert payload["invoice"]["total"]["value"] == "108.25"
        assert payload["invoice"]["line_items"][0]["description"]["value"] == "Widget"
        assert payload["quality"]["accepted"] is True

        replay = client.post(
            "/v1/documents/invoices:extract",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Idempotency-Key": "invoice-sync",
            },
            data={"metadata": json.dumps(metadata)},
            files={"file": ("invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
        assert replay.status_code == HTTP_OK
        assert replay.json()["replayed"] is True
        with factory() as session:
            assert len(session.scalars(select(ProviderAttempt)).all()) == 1
            assert len(session.scalars(select(BillingLedgerEntry)).all()) == 1
            stored = session.scalars(select(DocumentWorkflowResult)).one()
            assert stored.workflow == "invoice.extract"
    finally:
        fastapi_app.dependency_overrides.clear()
        object.__setattr__(settings, "object_store_root", original_root)


def test_invoice_quality_rejection_falls_back_without_double_billing(tmp_path: Path) -> None:
    original_root = settings.object_store_root
    object.__setattr__(settings, "object_store_root", str(tmp_path))
    factory = _app_session_factory()
    _tenant_id, api_key = _seed_invoice_tenant(
        factory,
        behaviors=["invoice_missing_total", "invoice"],
    )
    fastapi_app.dependency_overrides[get_session] = _session_override(factory)
    try:
        client = TestClient(fastapi_app)
        response = client.post(
            "/v1/documents/invoices:extract",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Idempotency-Key": "invoice-fallback",
            },
            data={"metadata": json.dumps({"provider_set": "documents-default"})},
            files={"file": ("invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
        )
        assert response.status_code == HTTP_OK
        assert response.json()["provider_id"] == "fake-doc-2"

        with factory() as session:
            attempts = session.scalars(
                select(ProviderAttempt).order_by(ProviderAttempt.created_at)
            ).all()
            assert [attempt.status for attempt in attempts] == [
                "workflow_rejected",
                "succeeded",
            ]
            assert attempts[0].cost_units == 0
            assert attempts[0].capability_metadata_json["missing_required_fields"] == ["total"]
            ledgers = session.scalars(select(BillingLedgerEntry)).all()
            assert len(ledgers) == 1
            assert ledgers[0].operation == INVOICE_OPERATION
    finally:
        fastapi_app.dependency_overrides.clear()
        object.__setattr__(settings, "object_store_root", original_root)


def test_async_invoice_submit_completes_via_worker(tmp_path: Path) -> None:
    original_root = settings.object_store_root
    object.__setattr__(settings, "object_store_root", str(tmp_path))
    factory = _app_session_factory()
    _tenant_id, api_key = _seed_invoice_tenant(factory, behaviors=["invoice"])
    fastapi_app.dependency_overrides[get_session] = _session_override(factory)
    try:
        client = TestClient(fastapi_app)
        submit = client.post(
            "/v1/documents/invoices:submit",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Idempotency-Key": "invoice-async",
            },
            data={"metadata": json.dumps({"provider_set": "documents-default"})},
            files={"file": ("invoice.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
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
        result = job_response.json()["result"]
        assert result["schema_version"] == "documents.invoice.extract.v1"
        assert result["quality"]["accepted"] is True
    finally:
        fastapi_app.dependency_overrides.clear()
        object.__setattr__(settings, "object_store_root", original_root)
