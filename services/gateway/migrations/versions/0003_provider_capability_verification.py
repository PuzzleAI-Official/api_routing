"""provider capability verification

Revision ID: 0003_provider_capabilities
Revises: 0002_document_intelligence
Create Date: 2026-05-20
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0003_provider_capabilities"
down_revision = "0002_document_intelligence"
branch_labels = None
depends_on = None


def _table_names(bind: Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _column_names(bind: Connection, table_name: str) -> set[str]:
    if table_name not in _table_names(bind):
        return set()
    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table_name)}


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
        "provider_service_manifests",
        sa.Column(
            "verified_capabilities_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    _add_column_if_missing(
        bind,
        "provider_service_manifests",
        sa.Column(
            "capability_status_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    _add_column_if_missing(
        bind,
        "provider_service_manifests",
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        bind,
        "provider_service_manifests",
        sa.Column(
            "validation_status",
            sa.String(length=50),
            nullable=False,
            server_default="not_validated",
        ),
    )
    _add_column_if_missing(
        bind,
        "provider_service_manifests",
        sa.Column("validation_error_code", sa.String(length=100), nullable=True),
    )

    if "provider_service_validation_history" not in _table_names(bind):
        op.create_table(
            "provider_service_validation_history",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=36),
                sa.ForeignKey("tenants.id"),
                nullable=False,
            ),
            sa.Column("provider_id", sa.String(length=100), nullable=False),
            sa.Column("service_id", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("checked_capabilities_json", sa.JSON(), nullable=False),
            sa.Column("verified_capabilities_json", sa.JSON(), nullable=False),
            sa.Column("summary_json", sa.JSON(), nullable=False),
            sa.Column("raw_result_ref", sa.String(length=500), nullable=True),
            sa.Column("error_code", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index_if_missing(
        bind,
        "ix_provider_service_validation_history_tenant_id",
        "provider_service_validation_history",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_table("provider_service_validation_history")
    op.drop_column("provider_service_manifests", "validation_error_code")
    op.drop_column("provider_service_manifests", "validation_status")
    op.drop_column("provider_service_manifests", "last_validated_at")
    op.drop_column("provider_service_manifests", "capability_status_json")
    op.drop_column("provider_service_manifests", "verified_capabilities_json")
