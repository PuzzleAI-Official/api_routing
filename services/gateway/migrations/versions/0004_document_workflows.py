"""document workflow results

Revision ID: 0004_document_workflows
Revises: 0003_provider_capabilities
Create Date: 2026-05-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "0004_document_workflows"
down_revision = "0003_provider_capabilities"
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
    if "document_workflow_results" not in _table_names(bind):
        op.create_table(
            "document_workflow_results",
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
            sa.Column("workflow", sa.String(length=100), nullable=False),
            sa.Column("workflow_version", sa.String(length=100), nullable=False),
            sa.Column("provider_id", sa.String(length=100), nullable=False),
            sa.Column("service_id", sa.String(length=100), nullable=False),
            sa.Column("execution_plan_json", sa.JSON(), nullable=False),
            sa.Column("routing_summary_json", sa.JSON(), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=False),
            sa.Column("quality_json", sa.JSON(), nullable=False),
            sa.Column("raw_result_ref", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index_if_missing(
        bind,
        "ix_document_workflow_results_tenant_id",
        "document_workflow_results",
        ["tenant_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_document_workflow_results_request_id",
        "document_workflow_results",
        ["request_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_document_workflow_results_job_id",
        "document_workflow_results",
        ["job_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_document_workflow_results_workflow",
        "document_workflow_results",
        ["workflow"],
    )


def downgrade() -> None:
    op.drop_table("document_workflow_results")
