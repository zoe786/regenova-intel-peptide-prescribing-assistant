"""Regression tests for the ingest-job lifecycle hardening.

Context: a YouTube channel ingest sat at status='running' in the admin UI for
24h+. Root cause was two-fold and these tests lock in both fixes:

1. The worker task wrote the terminal status *after* an unguarded ``await``, so
   any exception — or arq cancelling the job on ``job_timeout`` (a
   ``CancelledError``, i.e. a BaseException) — skipped it and pinned the row at
   'running' forever. Now finalisation happens in a try/except/finally.
2. The fetch loop ran in an uncancellable worker thread with no runtime bound,
   so it kept hammering YouTube past the timeout. ``load_raw`` now honours a
   deadline and stops early with a partial (failed) result.

Plus a startup reaper that fails orphaned 'running' rows on worker boot.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

from apps.api import queue as q
from apps.api.services.audit_store import AuditStore
from pipelines.ingest_youtube import YouTubeIngestor


class _FakeAuditStore:
    def __init__(self) -> None:
        self.updates = []

    def update_ingest_job(self, job_id, **kwargs) -> None:
        self.updates.append((job_id, kwargs))

    def log_event(self, **kwargs) -> None:
        pass

    def statuses(self):
        return [u[1].get("status") for u in self.updates]


def _patch_orch(monkeypatch, **methods):
    """Replace AutonomousIngestionOrchestrator with a fake exposing ``methods``."""
    import pipelines.autonomous_orchestrator as ao

    class _FakeOrch:
        def __init__(self, *a, **k):
            pass

    for name, fn in methods.items():
        setattr(_FakeOrch, name, staticmethod(fn))
    monkeypatch.setattr(ao, "AutonomousIngestionOrchestrator", _FakeOrch)


class TestTaskAlwaysFinalises:
    def test_exception_finalises_failed_and_reraises(self, monkeypatch) -> None:
        store = _FakeAuditStore()

        def boom(channel_name, topic=""):
            raise RuntimeError("ingest blew up")

        _patch_orch(monkeypatch, run_youtube=boom)
        ctx = {"audit_store": store}

        with pytest.raises(RuntimeError):
            asyncio.run(q.ingest_youtube_task(ctx, "SomeChannel", job_id="j1"))

        # Critical: row must NOT be left at 'running'.
        assert store.statuses() == ["running", "failed"]
        assert "ingest blew up" in (store.updates[-1][1].get("error") or "")

    def test_cancellation_finalises_failed_and_reraises(self, monkeypatch) -> None:
        """The arq job_timeout path raises CancelledError (a BaseException)."""
        store = _FakeAuditStore()

        def cancelled(channel_name, topic=""):
            raise asyncio.CancelledError()

        _patch_orch(monkeypatch, run_youtube=cancelled)
        ctx = {"audit_store": store}

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(q.ingest_youtube_task(ctx, "SomeChannel", job_id="j2"))

        assert store.statuses() == ["running", "failed"]

    def test_success_path_unchanged(self, monkeypatch) -> None:
        from pipelines.autonomous_orchestrator import AutonomousRunRecord

        def ok(channel_name, topic=""):
            return AutonomousRunRecord(
                source="youtube", trigger={}, chunks_ingested=7, status="completed"
            )

        store = _FakeAuditStore()
        _patch_orch(monkeypatch, run_youtube=ok)
        out = asyncio.run(q.ingest_youtube_task({"audit_store": store}, "C", job_id="j3"))
        assert out["status"] == "completed"
        assert store.statuses() == ["running", "completed"]


class TestStartupReaper:
    def test_fails_orphaned_running_jobs(self, tmp_path) -> None:
        store = AuditStore(db_path=str(tmp_path / "audit.db"))
        running_a = store.log_ingest_job(source_type="youtube")
        running_b = store.log_ingest_job(source_type="pubmed")
        done = store.log_ingest_job(source_type="website")
        store.update_ingest_job(running_a, status="running")
        store.update_ingest_job(running_b, status="running")
        store.update_ingest_job(done, status="completed", total_chunks=3)

        assert store.fail_orphaned_running_jobs() == 2
        assert store.get_ingest_job(running_a)["status"] == "failed"
        assert store.get_ingest_job(running_b)["status"] == "failed"
        assert store.get_ingest_job(done)["status"] == "completed"  # untouched
        assert store.get_ingest_job(running_a)["completed_at"]
        # Idempotent: nothing left to reap.
        assert store.fail_orphaned_running_jobs() == 0


class TestRuntimeDeadline:
    def _ingestor(self):
        ing = YouTubeIngestor()
        ing._throttle_sleep = lambda extra=0.0: None  # type: ignore[assignment]
        ing._fetch_transcript = lambda vid: f"transcript {vid}"  # type: ignore[assignment]
        return ing

    def test_past_deadline_fetches_nothing(self) -> None:
        ing = self._ingestor()
        docs = ing.load_raw(video_ids=["a", "b", "c"], deadline=time.monotonic() - 1)
        assert docs == []
        assert ing._hit_deadline is True
        assert ing._last_attempted == 0

    def test_generous_deadline_processes_all(self) -> None:
        ing = self._ingestor()
        docs = ing.load_raw(video_ids=["a", "b", "c"], deadline=time.monotonic() + 100)
        assert len(docs) == 3
        assert ing._hit_deadline is False

    def test_no_deadline_processes_all(self) -> None:
        ing = self._ingestor()
        docs = ing.load_raw(video_ids=["a", "b"])  # deadline=None
        assert len(docs) == 2
        assert ing._hit_deadline is False

    def test_run_autonomous_surfaces_partial(self) -> None:
        ing = YouTubeIngestor()
        ing.discover_channel_videos = lambda *a, **k: ["v1", "v2", "v3"]  # type: ignore[assignment]

        def fake_load_raw(self, video_ids=None, deadline=None):
            self._last_skipped = 0
            self._last_video_count = len(video_ids)
            self._hit_deadline = True
            self._last_attempted = 1
            return []

        ing.load_raw = types.MethodType(fake_load_raw, ing)
        res = ing.run_autonomous("SomeChannel", topic="peptides")
        assert "runtime deadline" in " ".join(res.errors)
        assert res.success is False  # -> orchestrator maps to status 'failed'
