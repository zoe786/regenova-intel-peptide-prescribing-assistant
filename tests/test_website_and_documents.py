"""Tests for website orchestration wiring and document ingestion robustness."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pipelines.autonomous_orchestrator import AutonomousIngestionOrchestrator
from pipelines.ingest_documents import DocumentIngestor


@dataclass
class _Settings:
    chroma_persist_dir: str = "/tmp/x"
    llm_api_key: str = ""
    openai_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_base_url: str = ""


class TestWebsiteOrchestration:
    def test_run_website_records_provenance(self, monkeypatch) -> None:
        captured = []
        orch = AutonomousIngestionOrchestrator(_Settings(), audit_sink=captured.append)

        import pipelines.ingest_websites as iw

        @dataclass
        class _Result:
            count: int = 12
            errors: list = None
            quarantined_documents: list = None
            @property
            def success(self):
                return True

        class _FakeIngestor:
            def __init__(self, *a, **k):
                pass
            def run_autonomous(self, **kwargs):
                assert kwargs["seed_url"] == "https://example.com"
                r = _Result()
                r.errors = []
                r.quarantined_documents = []
                return r

        monkeypatch.setattr(iw, "WebsiteIngestor", _FakeIngestor)

        record = orch.run_website("https://example.com", evidence_tier=4)
        assert record.status == "completed"
        assert record.chunks_ingested == 12
        assert record.trigger["evidence_tier"] == 4
        assert len(captured) == 1
        assert captured[0].source == "website"


class TestDocumentIngestion:
    """Confirms the existing document path still works end to end."""

    def test_txt_and_md_ingested(self, tmp_path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "a.txt").write_text("BPC-157 supports tendon repair in studies.", encoding="utf-8")
        (raw / "b.md").write_text("# Notes\n\nTB-500 discussed for recovery.\n", encoding="utf-8")
        ing = DocumentIngestor(
            raw_dir=raw, output_dir=tmp_path / "out", chroma_persist_dir=str(tmp_path / "chroma")
        )
        docs = ing.load_raw()
        assert len(docs) == 2
        # process() writes normalized JSON even if Chroma is unavailable.
        result = ing.process(docs)
        assert result.count >= 2
        assert result.source_type == "document"

    def test_empty_file_skipped(self, tmp_path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "empty.txt").write_text("   ", encoding="utf-8")
        (raw / "good.txt").write_text("Real peptide content here for ingestion.", encoding="utf-8")
        ing = DocumentIngestor(
            raw_dir=raw, output_dir=tmp_path / "out", chroma_persist_dir=str(tmp_path / "chroma")
        )
        docs = ing.load_raw()
        # Empty file dropped at load time.
        assert len(docs) == 1
        assert docs[0].source_name == "good"

    def test_unsupported_extension_ignored(self, tmp_path) -> None:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "image.png").write_bytes(b"\x89PNG\r\n")
        (raw / "doc.txt").write_text("ingest me", encoding="utf-8")
        ing = DocumentIngestor(
            raw_dir=raw, output_dir=tmp_path / "out", chroma_persist_dir=str(tmp_path / "chroma")
        )
        docs = ing.load_raw()
        assert len(docs) == 1
        assert docs[0].file_path == "doc.txt"
