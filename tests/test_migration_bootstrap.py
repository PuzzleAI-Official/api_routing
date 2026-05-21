from __future__ import annotations

from alembic.config import Config
from puzzle_gateway import migration_bootstrap
from pytest import MonkeyPatch


class FakeInspector:
    def __init__(self, tables: set[str], columns: dict[str, set[str]]) -> None:
        self._tables = tables
        self._columns = columns

    def get_table_names(self) -> list[str]:
        return sorted(self._tables)

    def get_columns(self, table_name: str) -> list[dict[str, str]]:
        return [{"name": column} for column in sorted(self._columns.get(table_name, set()))]


def _install_fake_inspector(
    monkeypatch: MonkeyPatch,
    tables: set[str],
    columns: dict[str, set[str]],
) -> list[str]:
    stamped_revisions: list[str] = []
    inspector = FakeInspector(tables, columns)
    monkeypatch.setattr(migration_bootstrap, "inspect", lambda _engine: inspector)

    def fake_stamp(_config: Config, revision: str) -> None:
        stamped_revisions.append(revision)

    monkeypatch.setattr(migration_bootstrap, "_stamp_revision", fake_stamp)
    return stamped_revisions


def test_alembic_config_uses_env_path(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("PUZZLE_ALEMBIC_CONFIG", "custom-alembic.ini")

    assert migration_bootstrap._alembic_config().config_file_name == "custom-alembic.ini"


def test_bootstrap_does_not_stamp_empty_database(monkeypatch: MonkeyPatch) -> None:
    stamped_revisions = _install_fake_inspector(monkeypatch, set(), {})

    migration_bootstrap._stamp_existing_bootstrap_schema(Config())

    assert stamped_revisions == []


def test_bootstrap_stamps_existing_core_schema(monkeypatch: MonkeyPatch) -> None:
    stamped_revisions = _install_fake_inspector(
        monkeypatch,
        {"tenants", "provider_attempts"},
        {"provider_attempts": {"provider", "status"}},
    )

    migration_bootstrap._stamp_existing_bootstrap_schema(Config())

    assert stamped_revisions == ["0001_initial_core"]


def test_bootstrap_stamps_existing_document_schema_as_0002(monkeypatch: MonkeyPatch) -> None:
    stamped_revisions = _install_fake_inspector(
        monkeypatch,
        {"tenants", "provider_attempts", "provider_service_manifests"},
        {"provider_attempts": {"provider", "service_id"}},
    )

    migration_bootstrap._stamp_existing_bootstrap_schema(Config())

    assert stamped_revisions == ["0002_document_intelligence"]


def test_bootstrap_stamps_existing_verified_capability_schema_as_0003(
    monkeypatch: MonkeyPatch,
) -> None:
    stamped_revisions = _install_fake_inspector(
        monkeypatch,
        {
            "tenants",
            "provider_attempts",
            "provider_service_manifests",
            "provider_service_validation_history",
        },
        {
            "provider_attempts": {"provider", "service_id"},
            "provider_service_manifests": {"verified_capabilities_json"},
        },
    )

    migration_bootstrap._stamp_existing_bootstrap_schema(Config())

    assert stamped_revisions == ["0003_provider_capabilities"]


def test_bootstrap_stamps_existing_workflow_schema_as_0004(
    monkeypatch: MonkeyPatch,
) -> None:
    stamped_revisions = _install_fake_inspector(
        monkeypatch,
        {
            "tenants",
            "provider_attempts",
            "provider_service_manifests",
            "provider_service_validation_history",
            "document_workflow_results",
        },
        {
            "provider_attempts": {"provider", "service_id"},
            "provider_service_manifests": {"verified_capabilities_json"},
        },
    )

    migration_bootstrap._stamp_existing_bootstrap_schema(Config())

    assert stamped_revisions == ["0004_document_workflows"]


def test_bootstrap_stamps_existing_workflow_prior_schema_as_head(
    monkeypatch: MonkeyPatch,
) -> None:
    stamped_revisions = _install_fake_inspector(
        monkeypatch,
        {
            "tenants",
            "provider_attempts",
            "provider_service_manifests",
            "provider_service_validation_history",
            "document_workflow_results",
            "workflow_provider_priors",
        },
        {
            "provider_attempts": {"provider", "service_id"},
            "provider_service_manifests": {"verified_capabilities_json"},
        },
    )

    migration_bootstrap._stamp_existing_bootstrap_schema(Config())

    assert stamped_revisions == ["head"]


def test_main_runs_bootstrap_then_upgrade(monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []
    config = Config()
    monkeypatch.setattr(migration_bootstrap, "_alembic_config", lambda: config)

    def fake_bootstrap(bootstrap_config: Config) -> None:
        assert bootstrap_config is config
        calls.append("bootstrap")

    def fake_upgrade(upgrade_config: Config) -> None:
        assert upgrade_config is config
        calls.append("upgrade")

    monkeypatch.setattr(migration_bootstrap, "_stamp_existing_bootstrap_schema", fake_bootstrap)
    monkeypatch.setattr(migration_bootstrap, "_upgrade_to_head", fake_upgrade)

    migration_bootstrap.main()

    assert calls == ["bootstrap", "upgrade"]
