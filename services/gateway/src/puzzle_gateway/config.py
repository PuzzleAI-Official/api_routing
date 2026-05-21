from __future__ import annotations

import os
from dataclasses import dataclass


def _default_document_capability_mode() -> str:
    explicit = os.getenv("PUZZLE_DOCUMENT_CAPABILITY_MODE")
    if explicit:
        return explicit
    environment = os.getenv("PUZZLE_ENV", "local")
    return "configured" if environment in {"local", "test"} else "verified"


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./.local/puzzle.db")
    redis_url: str | None = os.getenv("REDIS_URL")
    environment: str = os.getenv("PUZZLE_ENV", "local")
    admin_token: str = os.getenv("PUZZLE_ADMIN_TOKEN", "local-admin-token")
    admin_api_enabled: bool = os.getenv(
        "PUZZLE_ENABLE_ADMIN_API",
        "true" if os.getenv("PUZZLE_ENV", "local") in {"local", "test"} else "false",
    ).lower() in {"1", "true", "yes"}
    kms_master_key: str = os.getenv(
        "PUZZLE_LOCAL_KMS_MASTER_KEY",
        "local-development-master-key-32b",
    )
    object_store_root: str = os.getenv("PUZZLE_OBJECT_STORE_ROOT", ".local/object-store")
    document_capability_mode: str = _default_document_capability_mode()
    platform_spend_limit_units: int = int(os.getenv("PUZZLE_PLATFORM_SPEND_LIMIT_UNITS", "1000000"))
    default_rate_limit_per_minute: int = int(os.getenv("PUZZLE_RATE_LIMIT_PER_MINUTE", "120"))
    circuit_failure_threshold: int = int(os.getenv("PUZZLE_CIRCUIT_FAILURE_THRESHOLD", "3"))
    circuit_cooldown_seconds: int = int(os.getenv("PUZZLE_CIRCUIT_COOLDOWN_SECONDS", "60"))


settings = Settings()
