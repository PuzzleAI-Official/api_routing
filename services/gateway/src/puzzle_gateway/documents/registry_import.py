from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from puzzle_gateway.documents.manifests import (
    create_documents_provider_set,
    phase2_provider_specs,
    upsert_provider_service_manifest,
)
from puzzle_gateway.vault import Vault

PROVIDER_ALIASES = {
    "klippa": "klippa",
    "Klippa": "klippa",
    "verify": "veryfi",
    "veryfi": "veryfi",
    "mindee": "mindee",
    "nanonets": "nanonets",
}


def _provider_entry(registry: dict[str, Any], provider: str) -> dict[str, Any]:
    providers = registry.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("Provider registry is malformed")
    for raw_name, entry in providers.items():
        if PROVIDER_ALIASES.get(raw_name, raw_name.lower()) == provider and isinstance(entry, dict):
            return entry
    return {}


def _env_vars(entry: dict[str, Any]) -> dict[str, str]:
    raw = entry.get("env_vars", {})
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _secret_and_config(
    provider: str,
    entry: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    env = _env_vars(entry)
    config: dict[str, Any] = {
        "tier": entry.get("tier"),
        "monthly_limit": entry.get("monthly_limit"),
    }
    if provider == "mindee":
        config["model_id"] = env.get("MINDEE_MODEL_ID")
        return {"api_key": env.get("MINDEE_API_KEY", "")}, config
    if provider == "nanonets":
        config["model_id"] = env.get("NANONETS_MODEL_ID")
        return {"api_key": env.get("NANONETS_API_KEY", "")}, config
    if provider == "veryfi":
        return (
            {
                "client_id": env.get("VERYFI_CLIENT_ID", ""),
                "client_secret": env.get("VERYFI_CLIENT_SECRET", ""),
                "username": env.get("VERYFI_USERNAME", ""),
                "api_key": env.get("VERYFI_API_KEY", ""),
            },
            config,
        )
    if provider == "klippa":
        config["endpoint_family"] = "dochorizon"
        return {"api_key": env.get("KLIPPA_API_KEY", "")}, config
    return {}, config


def _validation_warning(
    provider: str,
    secret: dict[str, str],
    config: dict[str, Any],
) -> str | None:
    required_secret_keys = {
        "mindee": ["api_key"],
        "veryfi": ["client_id", "client_secret", "username", "api_key"],
        "nanonets": ["api_key"],
        "klippa": ["api_key"],
    }
    required_config_keys = {
        "mindee": ["model_id"],
        "nanonets": ["model_id"],
    }
    if any(not secret.get(key) for key in required_secret_keys.get(provider, [])):
        return "credential_incomplete"
    if any(not config.get(key) for key in required_config_keys.get(provider, [])):
        return "provider_config_missing"
    return None


def import_provider_registry(
    session: Session,
    *,
    tenant_id: str,
    path: str | Path,
    providers: list[str],
    actor: str = "registry-import",
) -> list[str]:
    raw_registry = json.loads(Path(path).read_text(encoding="utf-8"))
    requested = [PROVIDER_ALIASES.get(provider, provider.lower()) for provider in providers]
    imported: list[str] = []
    provider_configs: dict[str, dict[str, Any]] = {}
    provider_warnings: dict[str, str] = {}
    vault = Vault()

    for provider in requested:
        entry = _provider_entry(raw_registry, provider)
        if not entry:
            continue
        secret, config = _secret_and_config(provider, entry)
        if not any(secret.values()):
            continue
        vault.store(
            session,
            tenant_id=tenant_id,
            provider=provider,
            secret=json.dumps(secret, sort_keys=True),
            actor=actor,
        )
        provider_configs[provider] = {
            key: value for key, value in config.items() if value is not None
        }
        warning = _validation_warning(provider, secret, config)
        if warning is not None:
            provider_warnings[provider] = warning
        imported.append(provider)

    specs = [
        spec
        for spec in phase2_provider_specs(provider_configs=provider_configs)
        if spec.provider_id in set(imported)
    ]
    for spec in specs:
        row = upsert_provider_service_manifest(session, tenant_id=tenant_id, spec=spec)
        if spec.provider_id in provider_warnings:
            row.validation_status = "warning"
            row.validation_error_code = provider_warnings[spec.provider_id]
            session.add(row)
    if specs:
        create_documents_provider_set(session, tenant_id=tenant_id, specs=specs)
    session.flush()
    return imported
