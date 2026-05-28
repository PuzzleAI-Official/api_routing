from __future__ import annotations

import argparse
import io
import json
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx
from hardening_lib import (
    DEFAULT_ADMIN_TOKEN,
    DEFAULT_BASE_URL,
    SessionLocal,
    create_report_dir,
    seed_hardening_tenant,
    tiny_pdf_bytes,
    write_report,
)
from puzzle_gateway.models import (
    ApiKey,
    BillingLedgerEntry,
    DocumentObject,
    IdempotencyRecord,
    Job,
    ProviderCredential,
    ProviderServiceManifest,
    Tenant,
    WorkflowProviderPrior,
)
from puzzle_gateway.vault import Vault
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

TABLES_TO_COMPARE = [
    Tenant,
    ApiKey,
    IdempotencyRecord,
    Job,
    BillingLedgerEntry,
    ProviderServiceManifest,
    WorkflowProviderPrior,
    DocumentObject,
    ProviderCredential,
]


def run_capture(command: list[str], *, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(  # noqa: S603
        command,
        input=input_bytes,
        check=True,
        capture_output=True,
    )
    return result.stdout


def run_quiet(command: list[str], *, input_bytes: bytes | None = None) -> None:
    subprocess.run(command, input=input_bytes, check=True)  # noqa: S603


def find_object_store_volume() -> str:
    output = run_capture(["docker", "volume", "ls", "--format", "{{.Name}}"]).decode()
    candidates = [line.strip() for line in output.splitlines() if line.strip()]
    for name in candidates:
        if name.endswith("_puzzle-object-store") or name == "puzzle-object-store":
            return name
    raise RuntimeError("Could not find docker volume for puzzle-object-store")


def copy_object_store_volume(destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    volume = find_object_store_volume()
    tar_bytes = run_capture(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume}:/source:ro",
            "busybox",
            "tar",
            "-C",
            "/source",
            "-cf",
            "-",
            ".",
        ]
    )
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        archive.extractall(destination)
    return len(tar_bytes)


def count_table(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def create_seed_data(base_url: str, admin_token: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        client.get("/healthz").raise_for_status()
        tenant = seed_hardening_tenant(
            client,
            admin_token=admin_token,
            name=f"phase4a-dr-{time.time_ns()}",
        )
        response = client.post(
            "/v1/documents/invoices:extract",
            headers={
                "Authorization": f"Bearer {tenant.api_key}",
                "Idempotency-Key": f"dr-{time.time_ns()}",
            },
            data={
                "metadata": json.dumps(
                    {"provider_set": "documents-default", "line_items_mode": "preferred"}
                )
            },
            files={"file": ("invoice.pdf", tiny_pdf_bytes(), "application/pdf")},
        )
        response.raise_for_status()
    with SessionLocal() as session:
        Vault().store(
            session,
            tenant_id=tenant.tenant_id,
            provider="fake-doc-primary",
            secret=json.dumps({"api_key": "local-dr-secret"}),
            actor="local-dr-check",
        )
        session.commit()
    return tenant.tenant_id


def restore_dump_to_clean_database(dump_bytes: bytes, restore_db: str) -> None:
    run_quiet(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "dropdb",
            "-U",
            "puzzle",
            "--if-exists",
            restore_db,
        ]
    )
    run_quiet(
        ["docker", "compose", "exec", "-T", "postgres", "createdb", "-U", "puzzle", restore_db]
    )
    run_quiet(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "puzzle", "-d", restore_db],
        input_bytes=dump_bytes,
    )


def object_ref_exists_in_copy(ref: str, object_store_copy: Path) -> bool:
    if ref.startswith("deleted:"):
        return True
    marker = "/data/object-store/"
    if marker in ref:
        relative = ref.split(marker, 1)[1]
        return object_store_copy.joinpath(relative).exists()
    return Path(ref).exists()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Phase 4A local Postgres/object-store DR check."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--admin-token", default=DEFAULT_ADMIN_TOKEN)
    parser.add_argument("--restore-db-prefix", default="puzzle_restore")
    args = parser.parse_args()

    report_dir = create_report_dir("dr")
    tenant_id = create_seed_data(args.base_url, args.admin_token)
    with SessionLocal() as session:
        source_counts = {
            model.__tablename__: count_table(session, model) for model in TABLES_TO_COMPARE
        }

    started = time.perf_counter()
    dump_bytes = run_capture(
        ["docker", "compose", "exec", "-T", "postgres", "pg_dump", "-U", "puzzle", "-d", "puzzle"]
    )
    object_store_copy = report_dir / "object-store-restore"
    object_store_tar_bytes = copy_object_store_volume(object_store_copy)
    restore_db = f"{args.restore_db_prefix}_{int(time.time())}"
    restore_dump_to_clean_database(dump_bytes, restore_db)
    restore_seconds = round(time.perf_counter() - started, 3)

    restore_url = f"postgresql+psycopg://puzzle:puzzle@localhost:5432/{restore_db}"
    engine = create_engine(restore_url, pool_pre_ping=True)
    RestoreSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with RestoreSession() as session:
        session.execute(text("SELECT 1"))
        restored_counts = {
            model.__tablename__: count_table(session, model) for model in TABLES_TO_COMPARE
        }
        credential = session.scalars(
            select(ProviderCredential).where(ProviderCredential.tenant_id == tenant_id)
        ).first()
        vault_ok = False
        if credential is not None:
            restored_secret = Vault().retrieve(
                session,
                tenant_id=tenant_id,
                credential_id=credential.id,
                actor="local-dr-check",
            )
            vault_ok = "local-dr-secret" in restored_secret
        document_refs = [
            row.object_key
            for row in session.scalars(
                select(DocumentObject).where(DocumentObject.tenant_id == tenant_id)
            )
        ]
    object_refs_ok = all(object_ref_exists_in_copy(ref, object_store_copy) for ref in document_refs)
    counts_ok = all(restored_counts[name] >= count for name, count in source_counts.items())
    payload = {
        "passed": counts_ok and vault_ok and object_refs_ok,
        "tenant_id": tenant_id,
        "restore_db": restore_db,
        "dump_size_bytes": len(dump_bytes),
        "object_store_tar_bytes": object_store_tar_bytes,
        "restore_seconds": restore_seconds,
        "source_counts": source_counts,
        "restored_counts": restored_counts,
        "counts_ok": counts_ok,
        "vault_ok": vault_ok,
        "object_refs_ok": object_refs_ok,
    }
    write_report(report_dir, "dr", payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not payload["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
