"""Tests for the autonomous ingestion orchestrator.

Ingestors are monkeypatched so no network/Chroma is needed. We verify the
orchestrator records provenance, emits to the audit sink, and isolates
per-source failures.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipelines.autonomous_orchestrator import (
    AutonomousIngestionOrchestrator,
    AutonomousRunRecord,
)


@dataclass
class _FakeSettings:
    chroma_persist_dir: str = "/tmp/x"
    llm_api_key: str = ""
    openai_api_key: str = ""
    llm_model: str = "gpt-4o"
    llm_base_url: str = ""
    llm_temperature: float = 0.0
    pubmed_email: str = "e@x.com"
    pubmed_api_key: str = ""
    youtube_api_key: str = ""


@dataclass
class _FakeResult:
    count: int
    errors: list = field(default_factory=list)
    @property
    def success(self) -> bool:
        return not self.errors


class TestAuditSink:
    def test_sink_receives_record(self, monkeypatch) -> None:
        captured: list[AutonomousRunRecord] = []
        orch = AutonomousIngestionOrchestrator(
            _FakeSettings(), audit_sink=captured.append
        )

        # Patch the PubMed ingestor used inside run_pubmed.
        import pipelines.ingest_pubmed as pm

        class _FakeIngestor:
            def __init__(self, *a, **k):
                self._discovery_provenance = {"111": {"pubmed_query": "q", "peptide": "BPC-157"}}
            def run_autonomous(self, peptides):
                return _FakeResult(count=7)

        monkeypatch.setattr(pm, "PubMedIngestor", _FakeIngestor)

        record = orch.run_pubmed(["BPC-157"])
        assert record.status == "completed"
        assert record.chunks_ingested == 7
        assert len(captured) == 1
        assert captured[0].source == "pubmed"
        assert captured[0].llm_decisions[0]["pubmed_query"] == "q"


class TestErrorIsolation:
    def test_failure_captured_not_raised(self, monkeypatch) -> None:
        orch = AutonomousIngestionOrchestrator(_FakeSettings())
        import pipelines.ingest_youtube as yt

        class _BoomIngestor:
            def __init__(self, *a, **k):
                pass
            def run_autonomous(self, *a, **k):
                raise RuntimeError("api quota exceeded")

        monkeypatch.setattr(yt, "YouTubeIngestor", _BoomIngestor)

        record = orch.run_youtube("Some Channel", topic="peptides")
        assert record.status == "failed"
        assert any("quota" in e for e in record.errors)


class TestRunRecord:
    def test_to_audit_serialisable(self) -> None:
        r = AutonomousRunRecord(source="pubmed", trigger={"peptides": ["x"]}, chunks_ingested=3)
        d = r.to_audit()
        assert d["source"] == "pubmed"
        assert d["chunks_ingested"] == 3
        assert d["trigger"]["peptides"] == ["x"]
