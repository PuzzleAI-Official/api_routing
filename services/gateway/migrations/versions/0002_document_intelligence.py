"""document intelligence provider layer

Revision ID: 0002_document_intelligence
Revises: 0001_initial_core
Create Date: 2026-05-20
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0002_document_intelligence"
down_revision = "0001_initial_core"
branch_labels = None
depends_on = None


def _table_names(bind: Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind: Connection, table_name: str) -> set[str]:
    if table_name not in _table_names(bind):
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _index_names(bind: Connection, table_name: str) -> set[str]:
    if table_name not in _table_names(bind):
        return set()
    return {str(index["name"]) for index in sa.inspect(bind).get_indexes(table_name)}


def _add_column_if_missing(bind: Connection, table_name: str, column: sa.Column[Any]) -> None:
    if column.name not in _column_names(bind, table_name):
        op.add_column(table_name, column)


def _create_index_if_missing(
    bind: Connection,
    index_name: str,
    table_name: str,
    columns: list[str],
) -> None:
    if index_name not in _index_names(bind, table_name):
        op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    bind = op.get_bind()

    _add_column_if_missing(
        bind,
        "provider_attempts",
        sa.Column("service_id", sa.String(length=100), nullable=True),
    )
    _add_column_if_missing(
        bind,
        "provider_attempts",
        sa.Column("provider_request_id", sa.String(length=200), nullable=True),
    )
    _add_column_if_missing(
        bind,
        "provider_attempts",
        sa.Column(
            "capability_metadata_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    _add_column_if_missing(
        bind,
        "billing_ledger_entries",
        sa.Column("service_id", sa.String(length=100), nullable=True),
    )

    if "provider_service_manifests" not in _table_names(bind):
        op.create_table(
            "provider_service_manifests",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=36),
                sa.ForeignKey("tenants.id"),
                nullable=False,
            ),
            sa.Column("provider_id", sa.String(length=100), nullable=False),
            sa.Column("service_id", sa.String(length=100), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=False),
            sa.Column("capabilities_json", sa.JSON(), nullable=False),
            sa.Column("supported_mime_types_json", sa.JSON(), nullable=False),
            sa.Column("max_sync_bytes", sa.Integer(), nullable=False),
            sa.Column("max_async_bytes", sa.Integer(), nullable=False),
            sa.Column("supports_sync", sa.Boolean(), nullable=False),
            sa.Column("supports_async", sa.Boolean(), nullable=False),
            sa.Column("credential_schema_json", sa.JSON(), nullable=False),
            sa.Column("config_schema_json", sa.JSON(), nullable=False),
            sa.Column("option_schema_json", sa.JSON(), nullable=False),
            sa.Column("service_config_json", sa.JSON(), nullable=False),
            sa.Column("cost_model_json", sa.JSON(), nullable=False),
            sa.Column("retry_policy_json", sa.JSON(), nullable=False),
            sa.Column("normalizer_version", sa.String(length=100), nullable=False),
            sa.Column("readiness_status", sa.String(length=50), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "provider_id",
                "service_id",
                name="uq_provider_service_manifest",
            ),
        )
    _create_index_if_missing(
        bind,
        "ix_provider_service_manifests_tenant_id",
        "provider_service_manifests",
        ["tenant_id"],
    )

    if "document_objects" not in _table_names(bind):
        op.create_table(
            "document_objects",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=36),
                sa.ForeignKey("tenants.id"),
                nullable=False,
            ),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("mime_type", sa.String(length=100), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("object_key", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index_if_missing(
        bind,
        "ix_document_objects_tenant_id",
        "document_objects",
        ["tenant_id"],
    )
    _create_index_if_missing(bind, "ix_document_objects_sha256", "document_objects", ["sha256"])

    if "document_results" not in _table_names(bind):
        op.create_table(
            "document_results",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=36),
                sa.ForeignKey("tenants.id"),
                nullable=False,
            ),
            sa.Column(
                "document_id",
                sa.String(length=36),
                sa.ForeignKey("document_objects.id"),
                nullable=False,
            ),
            sa.Column("request_id", sa.String(length=64), nullable=False),
            sa.Column("job_id", sa.String(length=36), nullable=True),
            sa.Column("provider_id", sa.String(length=100), nullable=False),
            sa.Column("service_id", sa.String(length=100), nullable=False),
            sa.Column("normalized_result_json", sa.JSON(), nullable=False),
            sa.Column("raw_result_ref", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index_if_missing(
        bind,
        "ix_document_results_tenant_id",
        "document_results",
        ["tenant_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_document_results_request_id",
        "document_results",
        ["request_id"],
    )
    _create_index_if_missing(bind, "ix_document_results_job_id", "document_results", ["job_id"])

    if "provider_external_jobs" not in _table_names(bind):
        op.create_table(
            "provider_external_jobs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=36),
                sa.ForeignKey("tenants.id"),
                nullable=False,
            ),
            sa.Column("job_id", sa.String(length=36), sa.ForeignKey("jobs.id"), nullable=False),
            sa.Column("provider_id", sa.String(length=100), nullable=False),
            sa.Column("service_id", sa.String(length=100), nullable=False),
            sa.Column("external_job_id", sa.String(length=200), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "provider_id",
                "service_id",
                "external_job_id",
                name="uq_provider_external_job",
            ),
        )
    _create_index_if_missing(
        bind,
        "ix_provider_external_jobs_tenant_id",
        "provider_external_jobs",
        ["tenant_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_provider_external_jobs_job_id",
        "provider_external_jobs",
        ["job_id"],
    )


def downgrade() -> None:
    op.drop_table("provider_external_jobs")
    op.drop_table("document_results")
    op.drop_table("document_objects")
    op.drop_table("provider_service_manifests")
    op.drop_column("billing_ledger_entries", "service_id")
    op.drop_column("provider_attempts", "capability_metadata_json")
    op.drop_column("provider_attempts", "provider_request_id")
    op.drop_column("provider_attempts", "service_id")
