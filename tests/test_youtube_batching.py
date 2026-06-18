"""Tests for the batched YouTube ingest path and per-video reporting.

Covers, without a live Redis/arq round-trip (verified end-to-end on the VPS):
- load_raw classifying each video as ingested / skipped (no captions) / failed;
- AuditStore batch-progress accounting and per-video outcome storage;
- the report writer and the shared batch-result handler;
- the planner fanning a channel into bounded batch jobs (fake arq redis);
- the batch task folding success/failure into the parent job + report.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from apps.api import queue as q
from apps.api.services.audit_store import AuditStore
from pipelines.autonomous_orchestrator import (
    AutonomousIngestionOrchestrator,
    AutonomousRunRecord,
)
from pipelines.common.models import IngestionResult
from pipelines.ingest_youtube import YouTubeIngestor, YOUTUBE_BASE_URL


# ── load_raw: per-video outcome classification ──────────────────────────────
class TestLoadRawOutcomes:
    def _ingestor(self, mapping):
        """mapping: vid -> (fetch_status, text_or_None, reason)."""
        ing = YouTubeIngestor()
        ing._throttle_sleep = lambda extra=0.0: None  # type: ignore[assignment]

        def fake_fetch(vid):
            status, text, reason = mapping[vid]
            ing._last_fetch_status = status
            ing._last_fetch_reason = reason
            return text

        ing._fetch_transcript = fake_fetch  # type: ignore[assignment]
        # Avoid the real vector store; we only care about outcomes here.
        ing.process = lambda docs: IngestionResult(source_type="youtube")  # type: ignore[assignment]
        return ing

    def test_three_way_classification(self):
        mapping = {
            "ok1": ("ok", "a transcript", ""),
            "nocap": ("unavailable", None, "no transcript: NoTranscriptFound"),
            "blocked": ("failed", None, "blocked after 6 attempts: IpBlocked"),
        }
        ing = self._ingestor(mapping)
        ing.load_raw(video_ids=["ok1", "nocap", "blocked"], deadline=None)
        by_id = {o["video_id"]: o for o in ing._last_outcomes}
        assert by_id["ok1"]["status"] == "ingested"
        assert by_id["nocap"]["status"] == "skipped"  # no captions != failure
        assert by_id["blocked"]["status"] == "failed"
        assert "IpBlocked" in by_id["blocked"]["reason"]

    def test_run_batch_annotates_chunks(self):
        mapping = {"v1": ("ok", "text one", ""), "v2": ("ok", "text two", "")}
        ing = self._ingestor(mapping)

        def fake_process(docs):
            r = IngestionResult(source_type="youtube")
            for d in docs:
                r.per_source[d.source_url] = 4
                r.count += 4
            return r

        ing.process = fake_process  # type: ignore[assignment]
        result = ing.run_batch(["v1", "v2"], topic="")
        assert result.count == 8
        assert result.per_source[f"{YOUTUBE_BASE_URL}v1"] == 4


# ── AuditStore: batch progress + per-video outcomes ─────────────────────────
class TestAuditBatchProgress:
    def _store(self, tmp_path):
        return AuditStore(db_path=str(tmp_path / "audit.db"))

    def test_progress_accumulates_and_completes(self, tmp_path):
        store = self._store(tmp_path)
        jid = store.log_ingest_job(source_type="autonomous_youtube")
        store.init_ingest_job_batches(jid, videos_total=5, batches_total=3)

        p1 = store.bump_ingest_job_progress(jid, add_chunks=10, batches_done_increment=1,
                                            counts={"ingested": 2})
        assert p1 == {"done": 1, "total": 3, "complete": False, "status": "running"}
        store.bump_ingest_job_progress(jid, add_chunks=5, batches_done_increment=1,
                                       counts={"ingested": 1, "failed": 1})
        p3 = store.bump_ingest_job_progress(jid, add_chunks=3, batches_done_increment=1,
                                            counts={"ingested": 1})
        assert p3["complete"] is True
        assert p3["status"] == "completed"

        row = store.get_ingest_job(jid)
        assert row["status"] == "completed"
        assert row["total_chunks"] == 18
        assert row["completed_at"]
        assert row["results"]["counts"] == {"ingested": 4, "skipped": 0, "failed": 1}
        assert row["results"]["batches"] == {"total": 3, "done": 3}

    def test_zero_chunks_completes_as_failed(self, tmp_path):
        store = self._store(tmp_path)
        jid = store.log_ingest_job(source_type="autonomous_youtube")
        store.init_ingest_job_batches(jid, videos_total=2, batches_total=1)
        p = store.bump_ingest_job_progress(jid, add_chunks=0, batches_done_increment=1,
                                           counts={"failed": 2})
        assert p["complete"] is True
        assert p["status"] == "failed"  # discovered videos but ingested nothing

    def test_outcomes_storage_and_summary(self, tmp_path):
        store = self._store(tmp_path)
        jid = store.log_ingest_job(source_type="autonomous_youtube")
        store.log_video_outcomes(jid, 0, [
            {"video_id": "a", "status": "ingested", "reason": "", "chunks": 3},
            {"video_id": "b", "status": "failed", "reason": "blocked", "chunks": 0},
        ])
        store.log_video_outcomes(jid, 1, [
            {"video_id": "c", "status": "skipped", "reason": "no captions", "chunks": 0},
        ])
        rows = store.get_video_outcomes(jid)
        assert [r["video_id"] for r in rows] == ["a", "b", "c"]  # batch then insert order
        summary = store.summarize_video_outcomes(jid)
        assert summary == {"ingested": 1, "skipped": 1, "failed": 1, "chunks": 3}


# ── Report writer + shared batch handler ────────────────────────────────────
class TestReporting:
    def test_write_report_csv(self, tmp_path):
        store = AuditStore(db_path=str(tmp_path / "audit.db"))
        jid = store.log_ingest_job(source_type="autonomous_youtube")
        store.log_video_outcomes(jid, 0, [
            {"video_id": "vid_a", "status": "ingested", "reason": "", "chunks": 7},
            {"video_id": "vid_b", "status": "failed", "reason": "blocked x6", "chunks": 0},
        ])
        path = q._write_youtube_report(store, jid, "My Channel", reports_dir=str(tmp_path / "r"))
        assert path and os.path.exists(path)
        text = open(path, encoding="utf-8").read()
        assert "vid_a" in text and "vid_b" in text
        assert "blocked x6" in text
        assert "My Channel" in text

    def test_handle_batch_result_completes_and_reports(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YT_REPORT_DIR", str(tmp_path / "reports"))
        store = AuditStore(db_path=str(tmp_path / "audit.db"))
        jid = store.log_ingest_job(source_type="autonomous_youtube")
        store.init_ingest_job_batches(jid, videos_total=2, batches_total=1)

        rec = AutonomousRunRecord(source="youtube", trigger={}, chunks_ingested=9, status="completed")
        rec.outcomes = [
            {"video_id": "x", "status": "ingested", "reason": "", "chunks": 5},
            {"video_id": "y", "status": "ingested", "reason": "", "chunks": 4},
        ]
        progress = q._handle_batch_result(store, jid, 0, "Chan", rec)
        assert progress["complete"] is True
        row = store.get_ingest_job(jid)
        assert row["status"] == "completed"
        assert row["total_chunks"] == 9
        assert row["results"].get("report_path")
        assert os.path.exists(row["results"]["report_path"])


# ── Planner: fan-out into bounded batches ───────────────────────────────────
class _FakeRedis:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, name, *args):
        self.enqueued.append((name, args))
        return type("J", (), {"job_id": f"job{len(self.enqueued)}"})()


class TestPlanner:
    def test_splits_into_batches_and_enqueues(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YT_INGEST_BATCH_SIZE", "20")
        store = AuditStore(db_path=str(tmp_path / "audit.db"))
        jid = store.log_ingest_job(source_type="autonomous_youtube")

        ids = [f"v{i}" for i in range(45)]  # -> 3 batches (20, 20, 5)
        prov = {vid: {"video_title": f"title {vid}"} for vid in ids}
        monkeypatch.setattr(
            AutonomousIngestionOrchestrator, "discover_youtube",
            lambda self, channel, topic="": (ids, prov),
        )

        redis = _FakeRedis()
        ctx = {"audit_store": store, "redis": redis}
        out = asyncio.run(q.ingest_youtube_channel_task(ctx, "Chan", "peptides", jid))

        assert out["batches"] == 3 and out["videos"] == 45
        assert len(redis.enqueued) == 3
        # enqueue args = (channel, topic, batch_videos, job_id, batch_index, total)
        sizes = [len(args[2]) for _, args in redis.enqueued]  # args[2] = batch_videos
        assert sizes == [20, 20, 5]
        name, args = redis.enqueued[0]
        assert name == "ingest_youtube_batch_task"
        assert args[3] == jid and args[5] == 3  # job_id, total batches
        first_video = args[2][0]
        assert first_video["id"] == "v0"
        assert first_video["prov"]["video_title"] == "title v0"
        # Parent row seeded with the plan.
        row = store.get_ingest_job(jid)
        assert row["status"] == "running"
        assert row["results"]["batches"] == {"total": 3, "done": 0}

    def test_no_videos_marks_failed(self, tmp_path, monkeypatch):
        store = AuditStore(db_path=str(tmp_path / "audit.db"))
        jid = store.log_ingest_job(source_type="autonomous_youtube")
        monkeypatch.setattr(
            AutonomousIngestionOrchestrator, "discover_youtube",
            lambda self, channel, topic="": ([], {}),
        )
        redis = _FakeRedis()
        out = asyncio.run(
            q.ingest_youtube_channel_task({"audit_store": store, "redis": redis}, "Chan", "", jid)
        )
        assert out["batches"] == 0
        assert redis.enqueued == []
        assert store.get_ingest_job(jid)["status"] == "failed"


# ── Batch task: success + failure folding into the parent ───────────────────
class TestBatchTask:
    def _setup(self, tmp_path, monkeypatch, batches_total=1):
        monkeypatch.setenv("YT_REPORT_DIR", str(tmp_path / "reports"))
        store = AuditStore(db_path=str(tmp_path / "audit.db"))
        jid = store.log_ingest_job(source_type="autonomous_youtube")
        store.init_ingest_job_batches(jid, videos_total=2, batches_total=batches_total)
        return store, jid

    def test_success_records_outcomes_and_completes(self, tmp_path, monkeypatch):
        store, jid = self._setup(tmp_path, monkeypatch)
        rec = AutonomousRunRecord(source="youtube", trigger={}, chunks_ingested=6, status="completed")
        rec.outcomes = [
            {"video_id": "a", "status": "ingested", "reason": "", "chunks": 6},
            {"video_id": "b", "status": "skipped", "reason": "no captions", "chunks": 0},
        ]
        monkeypatch.setattr(
            AutonomousIngestionOrchestrator, "ingest_youtube_batch",
            lambda self, ch, topic, bv: rec,
        )
        batch_videos = [{"id": "a", "prov": {}}, {"id": "b", "prov": {}}]
        out = asyncio.run(q.ingest_youtube_batch_task(
            {"audit_store": store}, "Chan", "", batch_videos, jid, 0, 1
        ))
        assert out["status"] == "completed"
        assert store.summarize_video_outcomes(jid) == {
            "ingested": 1, "skipped": 1, "failed": 0, "chunks": 6
        }
        row = store.get_ingest_job(jid)
        assert row["status"] == "completed"
        assert os.path.exists(row["results"]["report_path"])

    def test_failure_marks_videos_failed_counts_batch_and_reraises(self, tmp_path, monkeypatch):
        store, jid = self._setup(tmp_path, monkeypatch)

        def boom(self, ch, topic, bv):
            raise RuntimeError("batch exploded")

        monkeypatch.setattr(AutonomousIngestionOrchestrator, "ingest_youtube_batch", boom)
        batch_videos = [{"id": "a", "prov": {}}, {"id": "b", "prov": {}}]
        with pytest.raises(RuntimeError):
            asyncio.run(q.ingest_youtube_batch_task(
                {"audit_store": store}, "Chan", "", batch_videos, jid, 0, 1
            ))
        # Both videos recorded failed; the (only) batch counted done -> channel
        # completes; zero chunks -> status failed.
        summary = store.summarize_video_outcomes(jid)
        assert summary["failed"] == 2
        row = store.get_ingest_job(jid)
        assert row["results"]["batches"]["done"] == 1
        assert row["status"] == "failed"
