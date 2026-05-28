from __future__ import annotations

import sys
from datetime import datetime
from types import ModuleType

from apps.api.routers import sources as sources_router
from apps.api.routers.upload import _ingest_documents_task
from apps.api.services.audit_store import AuditStore
from pipelines.common.metadata_enrichment import generate_document_id
from pipelines.common.models import IngestionResult, RawDocument
from pipelines.ingest_documents import DocumentIngestor


class _FakeCollection:
    def __init__(self, existing_ids: list[str]) -> None:
        self.existing_ids = existing_ids
        self.deleted_ids: list[str] = []

    def get(self, where=None, include=None):  # noqa: ANN001, ARG002
        return {"ids": list(self.existing_ids), "metadatas": [{} for _ in self.existing_ids]}

    def delete(self, ids):  # noqa: ANN001
        self.deleted_ids.extend(ids)


def test_reingest_local_file_removes_stale_chunks(monkeypatch, tmp_path):
    acquired_at = datetime(2025, 1, 1)
    document_id = generate_document_id(None, acquired_at, "protocol", file_path="documents/protocol.txt")
    stale_ids = [f"{document_id}_{idx:04d}" for idx in range(3)]
    fake_collection = _FakeCollection(stale_ids)

    for stale_id in stale_ids:
        (tmp_path / f"{stale_id}.json").write_text("stale", encoding="utf-8")

    monkeypatch.setattr("pipelines.ingest_documents.get_collection", lambda **_: fake_collection)
    monkeypatch.setattr("pipelines.ingest_documents.save_to_vector_store", lambda *args, **kwargs: 0)

    ingestor = DocumentIngestor(raw_dir=tmp_path, output_dir=tmp_path, chroma_persist_dir=str(tmp_path))
    doc = RawDocument(
        source_type="document",
        source_name="protocol",
        raw_content="Updated local protocol content for re-ingestion.",
        acquired_at=acquired_at,
        file_path="documents/protocol.txt",
    )

    result = ingestor.process([doc])

    assert result.count >= 1
    assert fake_collection.deleted_ids == stale_ids
    assert not (tmp_path / f"{document_id}_0002.json").exists()


def test_quarantined_pdf_records_are_persisted_in_ingest_results(tmp_path, monkeypatch):
    quarantine_entry = {
        "document_id": "doc-123",
        "source_name": "bad-scan",
        "file_path": "documents/bad-scan.pdf",
        "extraction_method": "pypdf",
        "warnings": ["low_alpha_ratio", "ocr_dependency_missing_pymupdf"],
    }

    class _FakeIngestor:
        def __init__(self, **kwargs):  # noqa: ANN003, ARG002
            pass

        def run(self) -> IngestionResult:
            return IngestionResult(
                source_type="document",
                count=0,
                errors=["Quarantined low-quality PDF extraction"],
                quarantined_documents=[quarantine_entry],
                duration_seconds=0.01,
            )

    fake_module = ModuleType("pipelines.ingest_documents")
    fake_module.DocumentIngestor = _FakeIngestor
    monkeypatch.setitem(sys.modules, "pipelines.ingest_documents", fake_module)

    audit_store = AuditStore(db_path=str(tmp_path / "audit.db"), ip_salt="test-salt")
    job_id = audit_store.log_ingest_job(source_type="documents")

    _ingest_documents_task(
        raw_dir=str(tmp_path),
        chroma_persist_dir=str(tmp_path / "chroma"),
        audit_store=audit_store,
        job_id=job_id,
    )

    job = audit_store.get_ingest_job(job_id)
    assert job is not None
    docs_result = (job.get("results") or {}).get("documents") or {}
    assert docs_result.get("quarantined_documents") == [quarantine_entry]

    quarantined = audit_store.list_pdf_quarantine_records(limit=10, offset=0)
    assert quarantined
    assert quarantined[0]["document_id"] == "doc-123"
    assert "low_alpha_ratio" in quarantined[0]["warnings"]


async def test_list_sources_exposes_quarantined_documents(tmp_path, monkeypatch):
    audit_store = AuditStore(db_path=str(tmp_path / "audit.db"), ip_salt="test-salt")
    audit_store.log_pdf_quarantine_records(
        [
            {
                "document_id": "doc-q1",
                "source_name": "scan-1",
                "file_path": "documents/scan-1.pdf",
                "extraction_method": "pypdf",
                "warnings": ["low_alpha_ratio"],
            }
        ],
        job_id="job-1",
    )

    class _EmptyCollection:
        def count(self):
            return 0

    monkeypatch.setattr(sources_router, "_get_collection", lambda _settings: _EmptyCollection())

    payload = await sources_router.list_sources(
        limit=20,
        offset=0,
        _=None,
        settings=object(),
        audit_store=audit_store,
    )

    assert payload["sources"] == []
    assert payload["quarantined_documents"]
    assert payload["quarantined_documents"][0]["document_id"] == "doc-q1"
