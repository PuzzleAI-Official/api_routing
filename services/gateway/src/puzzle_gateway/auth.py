from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from puzzle_gateway.errors import AuthenticationError, AuthorizationError
from puzzle_gateway.models import ApiKey, Tenant
from puzzle_gateway.security import api_key_prefix, verify_api_key


def authenticate_api_key(session: Session, api_key: str) -> Tenant:
    key = session.scalars(
        select(ApiKey).where(ApiKey.key_prefix == api_key_prefix(api_key), ApiKey.active.is_(True))
    ).first()
    if key is None or not verify_api_key(api_key, salt=key.salt, expected_hash=key.key_hash):
        raise AuthenticationError("Invalid API key")
    tenant = session.get(Tenant, key.tenant_id)
    if tenant is None or not tenant.active:
        raise AuthorizationError("Tenant is inactive")
    return tenant
