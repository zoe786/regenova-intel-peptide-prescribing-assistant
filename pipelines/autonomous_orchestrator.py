"""Autonomous ingestion orchestrator.

Coordinates the three autonomous sources (PubMed search, YouTube channel,
Skool export) behind one interface, records a provenance audit trail for
every run, and exposes a clean seam where a real job queue (arq / Celery /
RQ) can be plugged in later.

Why this exists:
- The existing ``RunAllIngestion`` runs the *file-based* ingestors in
  sequence. Autonomous ingestion needs different inputs (a peptide list, a
  channel name) and must record *why* each run happened.
- In-process FastAPI BackgroundTasks are not durable. This orchestrator is
  deliberately transport-agnostic: ``run_*`` methods are plain callables you
  can invoke directly today and enqueue tomorrow without changing them.

The orchestrator never raises into the caller for an individual source
failure — it captures errors per source so one flaky source can't sink a
whole run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class AutonomousRunRecord:
    """Auditable record of a single autonomous ingestion run."""

    source: str                       # "pubmed" | "youtube" | "skool"
    trigger: dict                     # what was requested (peptides / channel)
    chunks_ingested: int = 0
    status: str = "pending"           # pending | completed | failed
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    llm_decisions: list[dict] = field(default_factory=list)

    def to_audit(self) -> dict:
        return asdict(self)


class AutonomousIngestionOrchestrator:
    """Runs autonomous ingestion for each source with provenance auditing.

    Args:
        settings: An object exposing the relevant config attributes
            (chroma_persist_dir, llm_api_key/openai_api_key, llm_model,
            llm_base_url, llm_temperature, pubmed_email, pubmed_api_key,
            youtube_api_key). The app ``Settings`` object satisfies this.
        audit_sink: Optional callable invoked with each AutonomousRunRecord
            (e.g. ``audit_store.log_event``-style). Kept generic so the
            orchestrator has no hard dependency on the audit store.
    """

    def __init__(
        self,
        settings: Any,
        audit_sink: Optional[Callable[[AutonomousRunRecord], None]] = None,
    ) -> None:
        self.settings = settings
        self.audit_sink = audit_sink

    # ── Shared helpers ────────────────────────────────────────────────────

    def _build_llm_assistant(self):
        from pipelines.common.llm_assistant import LLMAssistant
        return LLMAssistant(
            model=getattr(self.settings, "llm_model", "gpt-4o"),
            api_key=(
                getattr(self.settings, "llm_api_key", "")
                or getattr(self.settings, "openai_api_key", "")
            ),
            base_url=getattr(self.settings, "llm_base_url", ""),
            temperature=0.0,
        )

    def _emit(self, record: AutonomousRunRecord) -> None:
        """Send a run record to the audit sink (best-effort)."""
        logger.info(
            "AUTONOMOUS_RUN source=%s status=%s chunks=%d errors=%d",
            record.source, record.status, record.chunks_ingested, len(record.errors),
        )
        if self.audit_sink is not None:
            try:
                self.audit_sink(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Audit sink failed (non-fatal): %s", exc)

    # ── PubMed ────────────────────────────────────────────────────────────

    def run_pubmed(self, peptides: list[str], max_results_per_peptide: int = 50) -> AutonomousRunRecord:
        """Discover and ingest PubMed abstracts for a list of peptides."""
        record = AutonomousRunRecord(source="pubmed", trigger={"peptides": peptides})
        start = time.time()
        try:
            from pipelines.ingest_pubmed import PubMedIngestor
            ingestor = PubMedIngestor(
                chroma_persist_dir=getattr(self.settings, "chroma_persist_dir", "./data/chroma_db"),
                email=getattr(self.settings, "pubmed_email", "") or "research@example.com",
                api_key=getattr(self.settings, "pubmed_api_key", ""),
                llm_assistant=self._build_llm_assistant(),
                max_results_per_peptide=max_results_per_peptide,
            )
            result = ingestor.run_autonomous(peptides)
            record.chunks_ingested = result.count
            record.errors = list(result.errors)
            # Capture which query found things, for the provenance trail.
            record.llm_decisions = [
                {"pmid": pmid, **prov}
                for pmid, prov in list(ingestor._discovery_provenance.items())[:200]
            ]
            record.status = "completed" if result.success else "failed"
        except Exception as exc:  # noqa: BLE001
            logger.error("Autonomous PubMed run failed: %s", exc)
            record.status = "failed"
            record.errors.append(str(exc))
        record.duration_seconds = round(time.time() - start, 2)
        self._emit(record)
        return record

    # ── YouTube ───────────────────────────────────────────────────────────

    def run_youtube(self, channel_name: str, topic: str = "") -> AutonomousRunRecord:
        """Discover and ingest every video from a channel."""
        record = AutonomousRunRecord(
            source="youtube", trigger={"channel_name": channel_name, "topic": topic}
        )
        start = time.time()
        try:
            from pipelines.ingest_youtube import YouTubeIngestor
            ingestor = YouTubeIngestor(
                chroma_persist_dir=getattr(self.settings, "chroma_persist_dir", "./data/chroma_db"),
                youtube_api_key=getattr(self.settings, "youtube_api_key", ""),
                llm_assistant=self._build_llm_assistant(),
            )
            result = ingestor.run_autonomous(channel_name, topic=topic)
            record.chunks_ingested = result.count
            record.errors = list(result.errors)
            record.llm_decisions = [
                {"video_id": vid, **prov}
                for vid, prov in list(ingestor._discovery_provenance.items())[:200]
            ]
            record.status = "completed" if result.success else "failed"
        except Exception as exc:  # noqa: BLE001
            logger.error("Autonomous YouTube run failed: %s", exc)
            record.status = "failed"
            record.errors.append(str(exc))
        record.duration_seconds = round(time.time() - start, 2)
        self._emit(record)
        return record

    # ── Skool ─────────────────────────────────────────────────────────────

    def run_skool_export(self) -> AutonomousRunRecord:
        """Ingest Skool community JSON exports (the safe, default path)."""
        record = AutonomousRunRecord(source="skool", trigger={"mode": "export"})
        start = time.time()
        try:
            from pipelines.ingest_skool_community import SkoolCommunityIngestor
            ingestor = SkoolCommunityIngestor(
                chroma_persist_dir=getattr(self.settings, "chroma_persist_dir", "./data/chroma_db"),
            )
            result = ingestor.run()
            record.chunks_ingested = result.count
            record.errors = list(result.errors)
            record.status = "completed" if result.success else "failed"
        except Exception as exc:  # noqa: BLE001
            logger.error("Skool export run failed: %s", exc)
            record.status = "failed"
            record.errors.append(str(exc))
        record.duration_seconds = round(time.time() - start, 2)
        self._emit(record)
        return record
