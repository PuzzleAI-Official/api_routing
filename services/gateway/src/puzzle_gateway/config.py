from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./.local/puzzle.db")
    redis_url: str | None = os.getenv("REDIS_URL")
    environment: str = os.getenv("PUZZLE_ENV", "local")
    admin_token: str = os.getenv("PUZZLE_ADMIN_TOKEN", "local-admin-token")
    kms_master_key: str = os.getenv(
        "PUZZLE_LOCAL_KMS_MASTER_KEY",
        "local-development-master-key-32b",
    )
    platform_spend_limit_units: int = int(os.getenv("PUZZLE_PLATFORM_SPEND_LIMIT_UNITS", "1000000"))
    default_rate_limit_per_minute: int = int(os.getenv("PUZZLE_RATE_LIMIT_PER_MINUTE", "120"))
    circuit_failure_threshold: int = int(os.getenv("PUZZLE_CIRCUIT_FAILURE_THRESHOLD", "3"))
    circuit_cooldown_seconds: int = int(os.getenv("PUZZLE_CIRCUIT_COOLDOWN_SECONDS", "60"))


settings = Settings()
