from __future__ import annotations

import os
from pathlib import Path

import pytest
from puzzle_gateway.db import SessionLocal
from puzzle_gateway.documents.validation import validate_provider_service
from puzzle_gateway.storage import get_object_store

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        os.getenv("PUZZLE_LIVE_PROVIDER") != "1",
        reason="live provider smoke tests are opt-in",
    ),
]


@pytest.mark.parametrize(
    ("provider_id", "service_id"),
    [
        ("mindee", "model_inference"),
        ("veryfi", "documents"),
        ("nanonets", "ocr_model"),
        ("klippa", "generic"),
    ],
)
def test_live_provider_smoke(provider_id: str, service_id: str) -> None:
    sample_path = os.getenv("PUZZLE_LIVE_SAMPLE_PATH")
    tenant_id = os.getenv("PUZZLE_LIVE_TENANT_ID")
    assert sample_path, f"PUZZLE_LIVE_SAMPLE_PATH is required for {provider_id}:{service_id}"
    assert tenant_id, f"PUZZLE_LIVE_TENANT_ID is required for {provider_id}:{service_id}"
    with SessionLocal() as session:
        result = validate_provider_service(
            session,
            tenant_id=tenant_id,
            provider_id=provider_id,
            service_id=service_id,
            sample_path=Path(sample_path),
            object_store=get_object_store(),
            write=True,
            check_async=False,
            actor="live-provider-smoke",
        )
        session.commit()
    assert result.status in {"succeeded", "warning"}
    assert result.verified_capabilities
    assert result.raw_result_ref
    assert "secret" not in str(result.to_safe_dict()).lower()
