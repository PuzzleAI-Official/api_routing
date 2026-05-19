from __future__ import annotations

import pytest
from puzzle_gateway.auth import authenticate_api_key
from puzzle_gateway.errors import AuthenticationError
from puzzle_gateway.models import Tenant
from puzzle_gateway.seed import create_api_key, create_tenant
from sqlalchemy.orm import Session


def test_api_key_hash_lookup_authenticates_tenant(
    session: Session,
    seeded_tenant: tuple[Tenant, str],
) -> None:
    tenant, api_key = seeded_tenant

    authenticated = authenticate_api_key(session, api_key)

    assert authenticated.id == tenant.id


def test_cross_tenant_api_key_does_not_authenticate_other_key(session: Session) -> None:
    tenant_a = create_tenant(session, name="tenant-a")
    _row_a, api_key_a = create_api_key(session, tenant_id=tenant_a.id)
    _tenant_b = create_tenant(session, name="tenant-b")
    session.commit()

    assert authenticate_api_key(session, api_key_a).id == tenant_a.id
    with pytest.raises(AuthenticationError):
        authenticate_api_key(session, "pzl_live_invalid")
