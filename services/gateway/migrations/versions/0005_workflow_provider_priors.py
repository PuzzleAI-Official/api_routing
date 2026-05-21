"""workflow provider priors

Revision ID: 0005_workflow_provider_priors
Revises: 0004_document_workflows
Create Date: 2026-05-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0005_workflow_provider_priors"
down_revision = "0004_document_workflows"
branch_labels = None
depends_on = None


def _table_names(bind: Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _index_names(bind: Connection, table_name: str) -> set[str]:
    if table_name not in _table_names(bind):
        return set()
    return {str(index["name"]) for index in sa.inspect(bind).get_indexes(table_name)}


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
    if "workflow_provider_priors" not in _table_names(bind):
        op.create_table(
            "workflow_provider_priors",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=36),
                sa.ForeignKey("tenants.id"),
                nullable=False,
            ),
            sa.Column("workflow", sa.String(length=100), nullable=False),
            sa.Column("workflow_version", sa.String(length=100), nullable=False),
            sa.Column("provider_id", sa.String(length=100), nullable=False),
            sa.Column("service_id", sa.String(length=100), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("quality_prior", sa.Float(), nullable=False),
            sa.Column("fallback_priority", sa.Integer(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "workflow",
                "provider_id",
                "service_id",
                name="uq_workflow_provider_prior",
            ),
        )
    _create_index_if_missing(
        bind,
        "ix_workflow_provider_priors_tenant_id",
        "workflow_provider_priors",
        ["tenant_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_workflow_provider_priors_workflow",
        "workflow_provider_priors",
        ["workflow"],
    )


def downgrade() -> None:
    op.drop_table("workflow_provider_priors")
