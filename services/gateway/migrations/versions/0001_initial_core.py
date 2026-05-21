"""initial core tables

Revision ID: 0001_initial_core
Revises:
Create Date: 2026-05-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("region", sa.String(length=50), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("monthly_quota_units", sa.Integer(), nullable=False),
        sa.Column("spent_units", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("salt", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_table(
        "provider_sets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_provider_set_tenant_name"),
    )
    op.create_index("ix_provider_sets_tenant_id", "provider_sets", ["tenant_id"])
    op.create_table(
        "mock_provider_configs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("behavior", sa.String(length=50), nullable=False),
        sa.Column("quality_score", sa.Integer(), nullable=False),
        sa.Column("cost_units", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "provider", name="uq_mock_provider_tenant"),
    )
    op.create_index("ix_mock_provider_configs_tenant_id", "mock_provider_configs", ["tenant_id"])
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("result_ref", sa.String(length=500), nullable=True),
        sa.Column("billing_entry_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "operation", "idempotency_key", name="uq_idempotency_key"),
    )
    op.create_index("ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"])
    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("strategy", sa.String(length=50), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_routing_decisions_tenant_id", "routing_decisions", ["tenant_id"])
    op.create_index("ix_routing_decisions_request_id", "routing_decisions", ["request_id"])
    op.create_table(
        "provider_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("routing_decision_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_units", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_provider_attempts_tenant_id", "provider_attempts", ["tenant_id"])
    op.create_index("ix_provider_attempts_request_id", "provider_attempts", ["request_id"])
    op.create_index("ix_provider_attempts_job_id", "provider_attempts", ["job_id"])
    op.create_index(
        "ix_provider_attempts_routing_decision_id",
        "provider_attempts",
        ["routing_decision_id"],
    )
    op.create_table(
        "billing_ledger_entries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("cost_units", sa.Integer(), nullable=False),
        sa.Column("charge_units", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_billing_ledger_entries_tenant_id", "billing_ledger_entries", ["tenant_id"])
    op.create_index(
        "ix_billing_ledger_entries_request_id",
        "billing_ledger_entries",
        ["request_id"],
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("routing_decision_id", sa.String(length=36), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_request_id", "jobs", ["request_id"])
    op.create_table(
        "telemetry_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_telemetry_events_tenant_id", "telemetry_events", ["tenant_id"])
    op.create_index("ix_telemetry_events_event_type", "telemetry_events", ["event_type"])
    op.create_table(
        "telemetry_outbox_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_telemetry_outbox_events_tenant_id",
        "telemetry_outbox_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_telemetry_outbox_events_event_type",
        "telemetry_outbox_events",
        ["event_type"],
    )
    op.create_index("ix_telemetry_outbox_events_status", "telemetry_outbox_events", ["status"])
    op.create_table(
        "audit_log_entries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=100), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_entries_tenant_id", "audit_log_entries", ["tenant_id"])
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("encrypted_data_key", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_provider_credentials_tenant_id", "provider_credentials", ["tenant_id"])


def downgrade() -> None:
    for table in [
        "provider_credentials",
        "audit_log_entries",
        "telemetry_events",
        "telemetry_outbox_events",
        "jobs",
        "billing_ledger_entries",
        "provider_attempts",
        "routing_decisions",
        "idempotency_records",
        "mock_provider_configs",
        "provider_sets",
        "api_keys",
        "tenants",
    ]:
        op.drop_table(table)
