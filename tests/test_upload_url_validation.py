from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from apps.api.routers.upload import (
    UrlIngestRequest,
    _normalize_registration_value,
    upload_url,
)
from apps.api.services.audit_store import AuditStore


class _DummyRequest:
    client = SimpleNamespace(host="127.0.0.1")


@pytest.mark.parametrize(
    ("source_type", "value", "expected"),
    [
        ("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("youtube", "https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("pubmed", "https://pubmed.ncbi.nlm.nih.gov/12345678/", "12345678"),
        ("pubmed", "12345678", "12345678"),
    ],
)
def test_normalize_registration_value_accepts_supported_formats(
    source_type: str,
    value: str,
    expected: str,
) -> None:
    assert _normalize_registration_value(value, source_type) == expected


def test_normalize_registration_value_rejects_malformed_values() -> None:
    with pytest.raises(HTTPException):
        _normalize_registration_value("not-a-url", "website")
    with pytest.raises(HTTPException):
        _normalize_registration_value("abc", "pubmed")
    with pytest.raises(HTTPException):
        _normalize_registration_value("https://youtube.com/watch?v=", "youtube")


@pytest.mark.asyncio
async def test_upload_url_skips_duplicate_registration(tmp_path) -> None:
    raw_dir = tmp_path / "raw"
    (raw_dir / "youtube").mkdir(parents=True)
    (raw_dir / "youtube" / "video_ids.txt").write_text("dQw4w9WgXcQ\n", encoding="utf-8")

    result = await upload_url(
        request=_DummyRequest(),
        body=UrlIngestRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            source_type="youtube",
        ),
        background_tasks=BackgroundTasks(),
        _=None,
        settings=SimpleNamespace(raw_data_dir=str(raw_dir), chroma_persist_dir=str(tmp_path / "chroma")),
        audit_store=AuditStore(db_path=str(tmp_path / "audit.db"), ip_salt="test-salt"),
    )

    assert result["status"] == "skipped_duplicate"


@pytest.mark.asyncio
async def test_upload_url_rejects_skool_url_registration_in_phase1(tmp_path) -> None:
    with pytest.raises(HTTPException) as exc:
        await upload_url(
            request=_DummyRequest(),
            body=UrlIngestRequest(url="https://example.com/course", source_type="skool_courses"),
            background_tasks=BackgroundTasks(),
            _=None,
            settings=SimpleNamespace(raw_data_dir=str(tmp_path / "raw"), chroma_persist_dir=str(tmp_path / "chroma")),
            audit_store=AuditStore(db_path=str(tmp_path / "audit.db"), ip_salt="test-salt"),
        )

    assert exc.value.status_code == 422
    assert "exported files" in str(exc.value.detail).lower()
