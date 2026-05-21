from __future__ import annotations

import os
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect

from puzzle_gateway.db import engine

CORE_REVISION = "0001_initial_core"
DOCUMENT_REVISION = "0002_document_intelligence"
CAPABILITY_REVISION = "0003_provider_capabilities"
WORKFLOW_REVISION = "0004_document_workflows"


def _alembic_config() -> Config:
    config_path = Path(os.getenv("PUZZLE_ALEMBIC_CONFIG", "services/gateway/alembic.ini"))
    return Config(str(config_path))


def _stamp_revision(config: Config, revision: str) -> None:
    alembic_command.stamp(config, revision)


def _upgrade_to_head(config: Config) -> None:
    alembic_command.upgrade(config, "head")


def _stamp_existing_bootstrap_schema(config: Config) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables or "tenants" not in tables:
        return

    provider_attempt_columns = (
        {column["name"] for column in inspector.get_columns("provider_attempts")}
        if "provider_attempts" in tables
        else set()
    )
    manifest_columns = (
        {column["name"] for column in inspector.get_columns("provider_service_manifests")}
        if "provider_service_manifests" in tables
        else set()
    )
    if (
        "workflow_provider_priors" in tables
        and "document_workflow_results" in tables
        and "provider_service_validation_history" in tables
        and "verified_capabilities_json" in manifest_columns
    ):
        _stamp_revision(config, "head")
        return
    if (
        "document_workflow_results" in tables
        and "provider_service_validation_history" in tables
        and "verified_capabilities_json" in manifest_columns
    ):
        _stamp_revision(config, WORKFLOW_REVISION)
        return
    if (
        "provider_service_validation_history" in tables
        and "verified_capabilities_json" in manifest_columns
    ):
        _stamp_revision(config, CAPABILITY_REVISION)
        return
    if "provider_service_manifests" in tables and "service_id" in provider_attempt_columns:
        _stamp_revision(config, DOCUMENT_REVISION)
        return

    _stamp_revision(config, CORE_REVISION)


def main() -> None:
    config = _alembic_config()
    _stamp_existing_bootstrap_schema(config)
    _upgrade_to_head(config)


if __name__ == "__main__":
    main()
