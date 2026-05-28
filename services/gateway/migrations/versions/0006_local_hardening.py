"""local hardening

Revision ID: 0006_local_hardening
Revises: 0005_workflow_provider_priors
Create Date: 2026-05-21
"""
from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0006_local_hardening"
down_revision = "0005_workflow_provider_priors"
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
        "jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    _add_column_if_missing(
        bind,
        "jobs",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        bind,
        "jobs",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        bind,
        "jobs",
        sa.Column("last_error_json", sa.JSON(), nullable=True),
    )

    if "data_deletion_requests" not in _table_names(bind):
        op.create_table(
            "data_deletion_requests",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=36),
                sa.ForeignKey("tenants.id"),
                nullable=False,
            ),
            sa.Column("target_type", sa.String(length=30), nullable=False),
            sa.Column("target_id", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("requested_by", sa.String(length=100), nullable=False),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column("summary_json", sa.JSON(), nullable=False),
            sa.Column("error_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index_if_missing(
        bind,
        "ix_data_deletion_requests_tenant_id",
        "data_deletion_requests",
        ["tenant_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_data_deletion_requests_target_id",
        "data_deletion_requests",
        ["target_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_data_deletion_requests_status",
        "data_deletion_requests",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("data_deletion_requests")
    for column_name in ("last_error_json", "next_run_at", "locked_at", "attempt_count"):
        op.drop_column("jobs", column_name)
