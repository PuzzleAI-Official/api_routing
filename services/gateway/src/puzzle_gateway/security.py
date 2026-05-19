from __future__ import annotations

import hashlib
import hmac
import secrets

API_KEY_PREFIX = "pzl_live_"


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def api_key_prefix(api_key: str) -> str:
    return api_key[:20]


def create_salt() -> str:
    return secrets.token_hex(16)


def hash_api_key(api_key: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        salt.encode("utf-8"),
        210_000,
    )
    return digest.hex()


def verify_api_key(api_key: str, *, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(api_key, salt), expected_hash)
