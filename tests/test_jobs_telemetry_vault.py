from __future__ import annotations

from puzzle_gateway.circuit_breaker import CircuitBreaker
from puzzle_gateway.jobs import claim_next_queued_job, process_job_once, submit_job
from puzzle_gateway.kv import InMemoryKVStore
from puzzle_gateway.models import (
    AuditLogEntry,
    ProviderAttempt,
    TelemetryEvent,
    TelemetryOutboxEvent,
    Tenant,
)
from puzzle_gateway.routing import create_routing_decision
from puzzle_gateway.telemetry import drain_telemetry_outbox, publish_telemetry_outbox
from puzzle_gateway.vault import CloudKmsEnvelopeKms, Vault
from puzzle_shared import JobStatus, RoutingStrategy
from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROTATED_KEY_VERSION = 2
MIN_AUDIT_EVENTS = 4


class _KmsResponse:
    def __init__(self, *, ciphertext: bytes = b"", plaintext: bytes = b"") -> None:
        self.ciphertext = ciphertext
        self.plaintext = plaintext


class _FakeKmsClient:
    def __init__(self) -> None:
        self.encrypt_requests: list[dict[str, object]] = []
        self.decrypt_requests: list[dict[str, object]] = []

    def encrypt(self, *, request: dict[str, object]) -> _KmsResponse:
        self.encrypt_requests.append(request)
        plaintext = request["plaintext"]
        assert isinstance(plaintext, bytes)
        return _KmsResponse(ciphertext=plaintext[::-1])

    def decrypt(self, *, request: dict[str, object]) -> _KmsResponse:
        self.decrypt_requests.append(request)
        ciphertext = request["ciphertext"]
        assert isinstance(ciphertext, bytes)
        return _KmsResponse(plaintext=ciphertext[::-1])


def test_async_job_redelivery_completes_once(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    breaker = CircuitBreaker(InMemoryKVStore())
    decision = create_routing_decision(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        strategy=RoutingStrategy.BALANCED,
        provider_set_name="default",
        circuit_breaker=breaker,
        request_id="req-job",
    )
    job = submit_job(
        session,
        tenant_id=tenant.id,
        operation="core.mock",
        request_id="req-job",
        decision=decision,
        payload={"job": True},
        idempotency_key="job-idem",
    )
    claimed = claim_next_queued_job(session)
    assert claimed is not None
    assert claimed.id == job.id

    first = process_job_once(session, job_id=job.id, circuit_breaker=breaker)
    second = process_job_once(session, job_id=job.id, circuit_breaker=breaker)

    assert first.status == JobStatus.SUCCEEDED.value
    assert second.status == JobStatus.SUCCEEDED.value
    assert session.scalar(select(func.count()).select_from(ProviderAttempt)) == 1


def test_telemetry_ingest_is_off_request_path(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant

    publish_telemetry_outbox(
        session,
        event_type="request.completed",
        tenant_id=tenant.id,
        payload={"request_id": "req-telemetry"},
    )
    assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 0
    assert session.scalar(select(func.count()).select_from(TelemetryOutboxEvent)) == 1

    count = drain_telemetry_outbox(session)

    assert count == 1
    assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 1


def test_vault_encrypts_rotates_and_audits_secret(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, _api_key = seeded_tenant
    vault = Vault()

    first = vault.store(
        session,
        tenant_id=tenant.id,
        provider="mock-primary",
        secret="secret-v1",
        actor="tester",
    )
    assert (
        vault.retrieve(session, tenant_id=tenant.id, credential_id=first.id, actor="tester")
        == "secret-v1"
    )
    second = vault.store(
        session,
        tenant_id=tenant.id,
        provider="mock-primary",
        secret="secret-v2",
        actor="tester",
    )

    assert second.key_version == ROTATED_KEY_VERSION
    assert (
        vault.retrieve(session, tenant_id=tenant.id, credential_id=second.id, actor="tester")
        == "secret-v2"
    )
    audit_count = session.scalar(select(func.count()).select_from(AuditLogEntry)) or 0
    assert audit_count >= MIN_AUDIT_EVENTS


def test_cloud_kms_envelope_wraps_and_unwraps_with_aad() -> None:
    client = _FakeKmsClient()
    kms = CloudKmsEnvelopeKms("projects/p/locations/us/keyRings/r/cryptoKeys/k", client=client)

    wrapped = kms.wrap_key(b"data-key", aad=b"tenant:provider")

    assert kms.unwrap_key(wrapped, aad=b"tenant:provider") == b"data-key"
    assert client.encrypt_requests[0]["name"] == "projects/p/locations/us/keyRings/r/cryptoKeys/k"
    assert client.encrypt_requests[0]["additional_authenticated_data"] == b"tenant:provider"
    assert client.decrypt_requests[0]["additional_authenticated_data"] == b"tenant:provider"
