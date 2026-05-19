from __future__ import annotations

from puzzle_gateway.db import SessionLocal, create_all
from puzzle_gateway.seed import seed_default_tenant


def main() -> None:
    create_all()
    with SessionLocal() as session:
        tenant, api_key = seed_default_tenant(session)
        session.commit()
        print(f"tenant_id={tenant.id}")
        print(f"api_key={api_key}")


if __name__ == "__main__":
    main()
