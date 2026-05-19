from __future__ import annotations

from pathlib import Path


def test_phase_one_core_does_not_import_documents() -> None:
    root = Path(__file__).resolve().parents[1]
    checked_roots = [
        root / "packages" / "shared" / "src",
        root / "packages" / "puzzle" / "src",
        root / "services" / "gateway" / "src",
        root / "services" / "worker" / "src",
        root / "services" / "telemetry" / "src",
    ]

    offenders: list[str] = []
    for checked_root in checked_roots:
        for path in checked_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "documents" in text:
                offenders.append(str(path.relative_to(root)))

    assert offenders == []
