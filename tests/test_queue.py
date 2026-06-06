"""Tests for the arq task queue layer.

These cover the broker-independent behaviour:
- enqueue_or_run falls back to in-process execution when the queue is off,
  and schedules onto BackgroundTasks when provided;
- task functions run the orchestrator and update the job record.

The actual Redis round-trip is verified separately in an end-to-end run and
is not exercised here (no broker in unit tests).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from apps.api import queue as q


@dataclass
class _Settings:
    redis_url: str = ""
    @property
    def queue_enabled(self) -> bool:
        return bool(self.redis_url.strip())


class _FakeBackgroundTasks:
    def __init__(self) -> None:
        self.scheduled = []
    def add_task(self, fn, *a, **k) -> None:
        self.scheduled.append((fn, a, k))


class TestEnqueueOrRun:
    def test_fallback_runs_inline_without_background_tasks(self) -> None:
        ran = {"v": False}

        def work() -> None:
            ran["v"] = True

        result = asyncio.run(
            q.enqueue_or_run(
                settings=_Settings(redis_url=""),
                task_name="ingest_skool_task",
                task_args=(),
                fallback=work,
                background_tasks=None,
            )
        )
        assert result["mode"] == "in_process"
        assert ran["v"] is True

    def test_fallback_uses_background_tasks_when_given(self) -> None:
        bt = _FakeBackgroundTasks()

        def work() -> None:
            pass

        result = asyncio.run(
            q.enqueue_or_run(
                settings=_Settings(redis_url=""),
                task_name="ingest_skool_task",
                task_args=(),
                fallback=work,
                background_tasks=bt,
            )
        )
        assert result["mode"] == "in_process"
        assert len(bt.scheduled) == 1  # scheduled, not run inline

    def test_enqueue_path_calls_pool(self, monkeypatch) -> None:
        class _FakeJob:
            job_id = "abc123"

        class _FakePool:
            async def enqueue_job(self, name, *args):
                assert name == "ingest_pubmed_task"
                return _FakeJob()

        async def _fake_get_pool():
            return _FakePool()

        monkeypatch.setattr(q, "get_pool", _fake_get_pool)

        result = asyncio.run(
            q.enqueue_or_run(
                settings=_Settings(redis_url="redis://localhost:6379/0"),
                task_name="ingest_pubmed_task",
                task_args=(["BPC-157"], 50, "job1"),
                fallback=lambda: None,
            )
        )
        assert result["mode"] == "queued"
        assert result["arq_job_id"] == "abc123"


class _FakeAuditStore:
    def __init__(self) -> None:
        self.updates = []
    def update_ingest_job(self, job_id, **kwargs) -> None:
        self.updates.append((job_id, kwargs))
    def log_event(self, **kwargs) -> None:
        pass


class TestTaskFunctions:
    def test_skool_task_runs_and_updates_job(self, monkeypatch) -> None:
        store = _FakeAuditStore()

        # Patch orchestrator construction to avoid real ingestion.
        import pipelines.autonomous_orchestrator as ao
        from pipelines.autonomous_orchestrator import AutonomousRunRecord

        class _FakeOrch:
            def __init__(self, *a, **k):
                pass
            def run_skool_export(self):
                return AutonomousRunRecord(
                    source="skool", trigger={}, chunks_ingested=5, status="completed"
                )

        monkeypatch.setattr(ao, "AutonomousIngestionOrchestrator", _FakeOrch)

        ctx = {"audit_store": store}
        out = asyncio.run(q.ingest_skool_task(ctx, job_id="job-xyz"))
        assert out["chunks_ingested"] == 5
        assert out["status"] == "completed"
        # Job moved to running then completed.
        statuses = [u[1].get("status") for u in store.updates]
        assert "running" in statuses
        assert "completed" in statuses
