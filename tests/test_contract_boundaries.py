from __future__ import annotations

from pathlib import Path


def test_core_does_not_import_document_provider_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    checked_roots = [
        root / "services" / "gateway" / "src" / "puzzle_gateway" / "auth.py",
        root / "services" / "gateway" / "src" / "puzzle_gateway" / "circuit_breaker.py",
        root / "services" / "gateway" / "src" / "puzzle_gateway" / "idempotency.py",
        root / "services" / "gateway" / "src" / "puzzle_gateway" / "provider_sets.py",
        root / "services" / "gateway" / "src" / "puzzle_gateway" / "providers.py",
        root / "services" / "gateway" / "src" / "puzzle_gateway" / "routing.py",
        root / "services" / "gateway" / "src" / "puzzle_gateway" / "vault.py",
        root / "services" / "worker" / "src",
        root / "services" / "telemetry" / "src",
    ]

    offenders: list[str] = []
    for checked_root in checked_roots:
        paths = checked_root.rglob("*.py") if checked_root.is_dir() else [checked_root]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            if "documents.adapters" in text:
                offenders.append(str(path.relative_to(root)))

    assert offenders == []
