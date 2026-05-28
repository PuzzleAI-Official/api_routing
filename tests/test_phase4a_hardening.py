from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.config import settings
from puzzle_gateway.deletion import DataDeletionTargetNotFoundError, perform_data_deletion
from puzzle_gateway.documents.storage import store_document_object
from puzzle_gateway.errors import DependencyUnavailableError
from puzzle_gateway.idempotency import complete_operation
from puzzle_gateway.jobs import claim_next_queued_job, process_job_once, submit_job
from puzzle_gateway.kv import InMemoryKVStore, RedisKVStore, get_kv_store
from puzzle_gateway.models import (
    AuditLogEntry,
    BillingLedgerEntry,
    DocumentResult,
    DocumentWorkflowResult,
    IdempotencyRecord,
    Job,
    ProviderExternalJob,
    ProviderServiceManifest,
    TelemetryEvent,
    TelemetryOutboxEvent,
    Tenant,
    WorkflowProviderPrior,
    now_utc,
)
from puzzle_gateway.routing import create_routing_decision
from puzzle_gateway.seed import create_tenant, seed_phase4b_staging_tenant
from puzzle_gateway.storage import LocalObjectStore
from puzzle_shared import ApiErrorCode, JobStatus, RoutingStrategy
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

STALE_JOB_ATTEMPT_COUNT = 2
REDIS_EXPIRATION_SECONDS = 60
PHASE4B_FAKE_DOCUMENT_SERVICE_COUNT = 4
PHASE4B_INVOICE_PRIOR_COUNT = 2


class _BrokenRedisClient:
    def get(self, _key: str) -> str | None:
        raise RedisError("down")

    def set(self, _key: str, _value: str, *, ex: int | None = None) -> None:
        raise RedisError("down")

    def delete(self, _key: str) -> None:
        raise RedisError("down")

    def incr(self, _key: str) -> int:
        raise RedisError("down")


class _FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

    def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    def expire(self, key: str, ex: int) -> None:
        self.expirations[key] = ex


class _DependencyFailingKV:
    def get(self, _key: str) -> str | None:
        raise DependencyUnavailableError("Redis is unavailable")

    def set(self, _key: str, _value: str, *, ex: int | None = None) -> None:
        raise DependencyUnavailableError("Redis is unavailable")

    def delete(self, _key: str) -> None:
        raise DependencyUnavailableError("Redis is unavailable")

    def incr(self, _key: str, *, ex: int | None = None) -> int:
        raise DependencyUnavailableError("Redis is unavailable")


def test_redis_kv_store_maps_redis_errors_to_dependency_unavailable() -> None:
    store = RedisKVStore("redis://localhost:6379/0")
    store._client = _BrokenRedisClient()  # type: ignore[assignment]

    with pytest.raises(DependencyUnavailableError):
        store.incr("rate:test")
    with pytest.raises(DependencyUnavailableError):
        store.get("rate:test")
    with pytest.raises(DependencyUnavailableError):
        store.set("rate:test", "1")
    with pytest.raises(DependencyUnavailableError):
        store.delete("rate:test")


def test_redis_kv_store_success_path() -> None:
    store = RedisKVStore("redis://localhost:6379/0")
    fake = _FakeRedisClient()
    store._client = fake  # type: ignore[assignment]

    assert store.get("missing") is None
    store.set("key", "value", ex=30)
    assert store.get("key") == "value"
    assert store.incr("counter", ex=REDIS_EXPIRATION_SECONDS) == 1
    assert fake.expirations["counter"] == REDIS_EXPIRATION_SECONDS
    store.delete("key")
    assert store.get("key") is None


def test_get_kv_store_uses_in_memory_when_redis_url_is_missing() -> None:
    original = settings.redis_url
    object.__setattr__(settings, "redis_url", None)
    try:
        assert isinstance(get_kv_store(), InMemoryKVStore)
    finally:
        object.__setattr__(settings, "redis_url", original)


def test_get_kv_store_uses_redis_when_url_is_configured() -> None:
    original = settings.redis_url
    object.__setattr__(settings, "redis_url", "redis://localhost:6379/0")
    try:
        assert isinstance(get_kv_store(), RedisKVStore)
    finally:
        object.__setattr__(settings, "redis_url", original)


def test_routing_decision_includes_routing_overhead_metric(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant

    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.BALANCED,
        provider_set_name="default",
        circuit_breaker=CircuitBreaker(InMemoryKVStore()),
        request_id="req-routing-metrics",
    )

    assert decision.decision_json["metrics"]["routing_overhead_ms"] >= 0


def test_job_dependency_failure_requeues_without_billing(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.BALANCED,
        provider_set_name="default",
        circuit_breaker=CircuitBreaker(InMemoryKVStore()),
        request_id="req-dependency-requeue",
    )
    job = submit_job(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        request_id="req-dependency-requeue",
        decision=decision,
        payload={"ok": True},
        idempotency_key="dependency-job",
    )

    processed = process_job_once(
        session,
        job_id=job.id,
        circuit_breaker=CircuitBreaker(_DependencyFailingKV()),
    )

    assert processed.status == JobStatus.QUEUED.value
    assert processed.attempt_count == 1
    assert processed.next_run_at is not None
    assert processed.last_error_json is not None
    assert processed.last_error_json["code"] == ApiErrorCode.DEPENDENCY_UNAVAILABLE.value
    assert session.scalar(select(func.count()).select_from(BillingLedgerEntry)) == 0


def test_stale_running_job_is_claimed_once_more(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.BALANCED,
        provider_set_name="default",
        circuit_breaker=CircuitBreaker(InMemoryKVStore()),
        request_id="req-stale-job",
    )
    job = submit_job(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        request_id="req-stale-job",
        decision=decision,
        payload={"ok": True},
    )
    job.status = JobStatus.RUNNING.value
    job.attempt_count = 1
    job.locked_at = now_utc() - timedelta(minutes=5)
    session.add(job)
    session.flush()

    claimed = claim_next_queued_job(session)

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == JobStatus.RUNNING.value
    assert claimed.attempt_count == STALE_JOB_ATTEMPT_COUNT


def test_data_deletion_redacts_content_and_preserves_billing(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
    tmp_path: Path,
) -> None:
    tenant, _api_key = seeded_tenant
    object_store = LocalObjectStore(tmp_path)
    document = store_document_object(
        session,
        tenant_id=tenant.id,
        filename="invoice.pdf",
        mime_type="application/pdf",
        data=b"%PDF-1.4 secret invoice %%EOF",
        object_store=object_store,
    )
    document_ref = document.object_key
    raw_ref = object_store.put_bytes(
        tenant_id=tenant.id,
        name="raw/provider.json",
        data=b'{"full_text":"secret invoice"}',
    )
    result = DocumentResult(
        tenant_id=tenant.id,
        document_id=document.id,
        request_id="req-delete",
        job_id=None,
        provider_id="fake-doc",
        service_id="parse",
        normalized_result_json={"full_text": "secret invoice"},
        raw_result_ref=raw_ref,
    )
    workflow_result = DocumentWorkflowResult(
        tenant_id=tenant.id,
        document_id=document.id,
        request_id="req-delete",
        job_id=None,
        workflow="invoice.extract",
        workflow_version="invoice.extract.v1",
        provider_id="fake-doc",
        service_id="parse",
        execution_plan_json={"mode": "single_service"},
        routing_summary_json={"provider": "fake-doc"},
        result_json={"invoice": {"total": "108.25"}},
        quality_json={"accepted": True},
        raw_result_ref=raw_ref,
    )
    idempotency = IdempotencyRecord(
        tenant_id=tenant.id,
        operation="documents.process",
        idempotency_key="delete-key",
        request_hash="a" * 64,
        status="processing",
    )
    session.add_all([result, workflow_result, idempotency])
    session.flush()
    complete_operation(
        session,
        idempotency,
        response_json={"request_id": "req-delete", "document_id": document.id, "full_text": "x"},
        billing_entry_id=None,
        result_ref=raw_ref,
    )
    job = Job(
        tenant_id=tenant.id,
        request_id="req-delete",
        operation="documents.process",
        status=JobStatus.SUCCEEDED.value,
        routing_decision_id="route-delete",
        payload_json={"document_id": document.id, "secret": "invoice"},
        result_json={"full_text": "secret invoice"},
    )
    session.add(job)
    session.add(
        BillingLedgerEntry(
            tenant_id=tenant.id,
            request_id="req-delete",
            operation="documents.process",
            provider="fake-doc",
            service_id="parse",
            cost_units=1,
            charge_units=1,
            idempotency_key="delete-key",
        )
    )
    session.add(
        TelemetryEvent(
            tenant_id=tenant.id,
            event_type="document.completed",
            payload_json={"request_id": "req-delete", "document_id": document.id, "text": "secret"},
        )
    )
    session.add(
        TelemetryOutboxEvent(
            tenant_id=tenant.id,
            event_type="document.completed",
            payload_json={"request_id": "req-delete", "document_id": document.id, "text": "secret"},
        )
    )
    session.flush()
    session.add(
        ProviderExternalJob(
            tenant_id=tenant.id,
            job_id=job.id,
            provider_id="fake-doc",
            service_id="parse",
            external_job_id="external-delete",
            metadata_json={"document_id": document.id, "text": "secret"},
        )
    )
    session.flush()

    deletion = perform_data_deletion(
        session,
        tenant_id=tenant.id,
        target_type="document",
        target_id=document.id,
        requested_by="tester",
        reason="unit-test",
        object_store=object_store,
    )

    assert deletion.status == "completed"
    assert deletion.summary_json["object_refs_deleted"] >= 1
    assert not Path(document_ref).exists()
    assert not Path(raw_ref).exists()
    assert document.filename == "[deleted]"
    assert result.normalized_result_json["redacted"] is True
    assert workflow_result.result_json["redacted"] is True
    assert idempotency.response_json is not None
    assert idempotency.response_json["redacted"] is True
    assert job.payload_json["redacted"] is True
    billing_count = session.scalar(select(func.count()).select_from(BillingLedgerEntry))
    audit_count = session.scalar(select(func.count()).select_from(AuditLogEntry))
    assert billing_count == 1
    assert audit_count is not None
    assert audit_count >= 1


def test_data_deletion_by_request_id_redacts_related_rows(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
    tmp_path: Path,
) -> None:
    tenant, _api_key = seeded_tenant
    object_store = LocalObjectStore(tmp_path)
    document = store_document_object(
        session,
        tenant_id=tenant.id,
        filename="request.pdf",
        mime_type="application/pdf",
        data=b"%PDF-1.4 request secret %%EOF",
        object_store=object_store,
    )
    raw_ref = object_store.put_bytes(
        tenant_id=tenant.id,
        name="raw/request.json",
        data=b'{"text":"request secret"}',
    )
    result = DocumentResult(
        tenant_id=tenant.id,
        document_id=document.id,
        request_id="req-delete-by-request",
        job_id=None,
        provider_id="fake-doc",
        service_id="parse",
        normalized_result_json={"full_text": "request secret"},
        raw_result_ref=raw_ref,
    )
    session.add(result)
    session.flush()

    deletion = perform_data_deletion(
        session,
        tenant_id=tenant.id,
        target_type="request",
        target_id="req-delete-by-request",
        requested_by="tester",
        reason="request-target",
        object_store=object_store,
    )

    assert deletion.status == "completed"
    assert result.normalized_result_json["redacted"] is True
    assert document.filename == "[deleted]"


def test_data_deletion_by_job_id_redacts_job(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
    tmp_path: Path,
) -> None:
    tenant, _api_key = seeded_tenant
    object_store = LocalObjectStore(tmp_path)
    job = Job(
        tenant_id=tenant.id,
        request_id="req-delete-by-job",
        operation="documents.process",
        status=JobStatus.SUCCEEDED.value,
        routing_decision_id="route-job-delete",
        payload_json={"secret": "job payload"},
        result_json={"secret": "job result"},
        error_json={"secret": "job error"},
        last_error_json={"secret": "job last error"},
    )
    session.add(job)
    session.flush()

    deletion = perform_data_deletion(
        session,
        tenant_id=tenant.id,
        target_type="job",
        target_id=job.id,
        requested_by="tester",
        reason="job-target",
        object_store=object_store,
    )

    assert deletion.status == "completed"
    assert job.payload_json["redacted"] is True
    assert job.result_json is not None
    assert job.result_json["redacted"] is True
    assert job.error_json is not None
    assert job.error_json["redacted"] is True


def test_data_deletion_rejects_unknown_target_type(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
    tmp_path: Path,
) -> None:
    tenant, _api_key = seeded_tenant

    with pytest.raises(DataDeletionTargetNotFoundError):
        perform_data_deletion(
            session,
            tenant_id=tenant.id,
            target_type="tenant",
            target_id=tenant.id,
            requested_by="tester",
            reason="unsupported",
            object_store=LocalObjectStore(tmp_path),
        )


def test_data_deletion_is_tenant_scoped(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
    tmp_path: Path,
) -> None:
    tenant, _api_key = seeded_tenant
    other_tenant = create_tenant(session, name="tenant-b")
    object_store = LocalObjectStore(tmp_path)
    document = store_document_object(
        session,
        tenant_id=tenant.id,
        filename="invoice.pdf",
        mime_type="application/pdf",
        data=b"%PDF-1.4 secret invoice %%EOF",
        object_store=object_store,
    )

    with pytest.raises(DataDeletionTargetNotFoundError):
        perform_data_deletion(
            session,
            tenant_id=other_tenant.id,
            target_type="document",
            target_id=document.id,
            requested_by="tester",
            reason="cross-tenant",
            object_store=object_store,
        )


def test_phase4b_seed_creates_verified_fake_document_services(session: Session) -> None:
    tenant, api_key = seed_phase4b_staging_tenant(session, name="phase4b-test")

    manifests = list(
        session.scalars(
            select(ProviderServiceManifest).where(
                ProviderServiceManifest.tenant_id == tenant.id
            )
        )
    )
    priors = list(
        session.scalars(
            select(WorkflowProviderPrior).where(WorkflowProviderPrior.tenant_id == tenant.id)
        )
    )

    assert api_key.startswith("pzl_live_")
    assert len(manifests) == PHASE4B_FAKE_DOCUMENT_SERVICE_COUNT
    assert all(row.readiness_status == "verified" for row in manifests)
    assert all("documents.text" in row.verified_capabilities_json for row in manifests)
    assert len(priors) == PHASE4B_INVOICE_PRIOR_COUNT
