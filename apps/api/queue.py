"""arq task queue for durable autonomous ingestion.

Why this module exists
----------------------
Autonomous ingestion runs are long (a YouTube channel can be hundreds of
videos) and must survive an API restart. FastAPI ``BackgroundTasks`` run
in-process and die with the worker, so they are unsuitable for production.

This module moves the work onto an `arq <https://arq-docs.helpmanual.io/>`_
queue backed by Redis:

- ``ingest_pubmed_task`` / ``ingest_youtube_task`` / ``ingest_skool_task``
  are the queued functions. Each rebuilds the orchestrator from settings and
  records results into the audit store the worker owns.
- ``WorkerSettings`` is what ``arq apps.api.queue.WorkerSettings`` runs.
- ``enqueue_or_run`` is the single decision point used by the API routes:
  if a Redis URL is configured it enqueues; otherwise it falls back to the
  caller-provided in-process runner (dev convenience) so the app still works
  without a worker.

Design choices
--------------
- Tasks take only JSON-serialisable arguments (lists/strings). The worker
  reconstructs Settings, the audit store, and the orchestrator itself — never
  pass live objects through the queue.
- Each task writes its own ``ingest_job`` record so status is queryable via
  the existing audit endpoints, mirroring how the old BackgroundTasks path
  behaved. This keeps the UI's job-history view working unchanged.
- The worker owns its own AuditStore instance (separate process, separate
  SQLite connection). SQLite WAL mode is enabled on open to tolerate the
  worker and API writing concurrently.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────


def _build_orchestrator(audit_store: Any):
    """Construct an orchestrator wired to an audit sink for the worker."""
    from apps.api.config import get_settings
    from pipelines.autonomous_orchestrator import AutonomousIngestionOrchestrator

    settings = get_settings()

    def _sink(record) -> None:
        try:
            audit_store.log_event(
                event_type="autonomous_ingest",
                data=record.to_audit(),
                role="admin",
                ip="",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Worker audit sink failed: %s", exc)

    return AutonomousIngestionOrchestrator(settings, audit_sink=_sink)


def _worker_audit_store() -> Any:
    """Return an AuditStore for the worker process."""
    from apps.api.config import get_settings
    from apps.api.services.audit_store import AuditStore

    return AuditStore(db_path=get_settings().audit_db_path)


# ── arq task functions ──────────────────────────────────────────────────────
# Signature convention: first arg is the arq context dict (ctx), rest are the
# JSON-serialisable job arguments.


async def _run_orchestrated(
    *,
    audit_store: Any,
    job_id: str,
    results_key: str,
    runner: Callable[[], Any],
) -> dict:
    """Run a blocking orchestrator call off-thread, ALWAYS finalising the job.

    The audit ``ingest_job`` row is moved to a terminal status no matter how the
    work ends — normal return, an ordinary exception, or arq cancelling the job
    when it exceeds ``job_timeout`` (which raises ``asyncio.CancelledError``, a
    *BaseException*; we deliberately catch ``BaseException`` rather than
    ``Exception`` so the cancel path still finalises the row, then re-raise so
    arq's own bookkeeping is unchanged).

    Why this exists: the terminal write used to sit *after* the await with no
    guard, so any non-happy path skipped it and left the row stuck at 'running'
    forever. Note this does NOT stop the underlying worker thread — threads are
    not cancellable — so it is paired with the ingestor's own runtime deadline,
    which is what actually halts the work before arq's timeout fires. This guard
    only keeps the audit record honest if something still overruns.
    """
    import asyncio

    if job_id:
        audit_store.update_ingest_job(job_id, status="running")
    finalized = False
    try:
        record = await asyncio.to_thread(runner)
        if job_id:
            audit_store.update_ingest_job(
                job_id,
                status=record.status,
                total_chunks=record.chunks_ingested,
                results={results_key: record.to_audit()},
                error=record.errors[0] if record.errors else None,
            )
            finalized = True
        return record.to_audit()
    except BaseException as exc:  # noqa: BLE001 - must finalise then re-raise
        if job_id and not finalized:
            try:
                audit_store.update_ingest_job(
                    job_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                )
                finalized = True
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Could not finalise job %s after %s", job_id, type(exc).__name__
                )
        raise
    finally:
        # Last-resort guard: never leave a row stuck at 'running'.
        if job_id and not finalized:
            try:
                audit_store.update_ingest_job(
                    job_id,
                    status="failed",
                    error="Job ended without a terminal status "
                    "(worker exit or cancellation).",
                )
            except Exception:  # noqa: BLE001
                logger.exception("Final guard could not finalise job %s", job_id)


async def ingest_pubmed_task(
    ctx: dict, peptides: list[str], max_results_per_peptide: int = 50, job_id: str = ""
) -> dict:
    """Worker task: autonomous PubMed search + ingest."""
    audit_store = ctx.get("audit_store") or _worker_audit_store()
    orch = _build_orchestrator(audit_store)
    return await _run_orchestrated(
        audit_store=audit_store,
        job_id=job_id,
        results_key="pubmed",
        runner=lambda: orch.run_pubmed(peptides, max_results_per_peptide),
    )


async def ingest_youtube_task(
    ctx: dict, channel_name: str, topic: str = "", job_id: str = ""
) -> dict:
    """Worker task: autonomous YouTube channel ingest."""
    audit_store = ctx.get("audit_store") or _worker_audit_store()
    orch = _build_orchestrator(audit_store)
    return await _run_orchestrated(
        audit_store=audit_store,
        job_id=job_id,
        results_key="youtube",
        runner=lambda: orch.run_youtube(channel_name, topic),
    )


async def ingest_website_task(
    ctx: dict,
    seed_url: str,
    evidence_tier: int = 3,
    render_js: bool = False,
    max_pages: int = 200,
    cookies: dict | None = None,
    login_url: str | None = None,
    login_username: str | None = None,
    login_password: str | None = None,
    job_id: str = "",
) -> dict:
    """Worker task: full-site crawl + ingest (optionally authenticated)."""
    audit_store = ctx.get("audit_store") or _worker_audit_store()
    orch = _build_orchestrator(audit_store)
    return await _run_orchestrated(
        audit_store=audit_store,
        job_id=job_id,
        results_key="website",
        runner=lambda: orch.run_website(
            seed_url, evidence_tier, render_js, max_pages,
            cookies, login_url, login_username, login_password,
        ),
    )


async def ingest_skool_task(ctx: dict, job_id: str = "") -> dict:
    """Worker task: Skool export ingest."""
    audit_store = ctx.get("audit_store") or _worker_audit_store()
    orch = _build_orchestrator(audit_store)
    return await _run_orchestrated(
        audit_store=audit_store,
        job_id=job_id,
        results_key="skool",
        runner=lambda: orch.run_skool_export(),
    )


# ── Batched YouTube ingest (planner + per-batch + reporting) ────────────────


def _write_youtube_report(
    audit_store: Any, job_id: str, channel_name: str, reports_dir: str | None = None
) -> str | None:
    """Write a per-video CSV report for a finished channel ingest.

    The ``ingest_video_outcomes`` table is the source of truth; this is a
    human-readable export written to the shared data mount so it can be
    downloaded or grepped. Returns the path, or None if there were no outcomes
    or the write failed.
    """
    import csv
    import os
    from pathlib import Path

    try:
        outcomes = audit_store.get_video_outcomes(job_id)
    except Exception:  # noqa: BLE001
        logger.exception("Report: could not read outcomes for %s", job_id)
        return None
    if not outcomes:
        return None

    base = reports_dir or os.environ.get("YT_REPORT_DIR") or "data/reports"
    try:
        Path(base).mkdir(parents=True, exist_ok=True)
        path = str(Path(base) / f"youtube_{job_id}.csv")
        summary = audit_store.summarize_video_outcomes(job_id)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["# channel", channel_name])
            w.writerow(["# job_id", job_id])
            w.writerow([
                "# ingested", summary.get("ingested", 0),
                "skipped", summary.get("skipped", 0),
                "failed", summary.get("failed", 0),
                "chunks", summary.get("chunks", 0),
            ])
            w.writerow([])
            w.writerow(
                ["batch", "video_id", "status", "reason", "chunks", "url", "recorded_at"]
            )
            for o in outcomes:
                vid = o.get("video_id", "")
                w.writerow([
                    o.get("batch_index", 0), vid, o.get("status", ""),
                    o.get("reason", ""), o.get("chunks", 0),
                    f"https://www.youtube.com/watch?v={vid}", o.get("recorded_at", ""),
                ])
        logger.info(
            "YouTube ingest report written: %s (%d ingested, %d skipped, %d failed)",
            path, summary.get("ingested", 0), summary.get("skipped", 0),
            summary.get("failed", 0),
        )
        return path
    except Exception:  # noqa: BLE001
        logger.exception("Report: could not write CSV for %s", job_id)
        return None


def _handle_batch_result(
    audit_store: Any, parent_job_id: str, batch_index: int, channel_name: str, record: Any
) -> dict:
    """Persist one batch's outcomes and fold its progress into the parent job.

    Writes the per-video rows, bumps the parent's chunk/batch counters, and —
    when this was the final batch — exports the CSV report. Shared by the queued
    batch task and the in-process planner fallback.
    """
    outcomes = list(getattr(record, "outcomes", []) or [])
    counts = {"ingested": 0, "skipped": 0, "failed": 0}
    for o in outcomes:
        s = o.get("status", "failed")
        counts[s] = counts.get(s, 0) + 1
    if parent_job_id and outcomes:
        audit_store.log_video_outcomes(parent_job_id, batch_index, outcomes)
    progress = {"done": 0, "total": 0, "complete": False, "status": "running"}
    if parent_job_id:
        progress = audit_store.bump_ingest_job_progress(
            parent_job_id,
            add_chunks=getattr(record, "chunks_ingested", 0),
            batches_done_increment=1,
            counts=counts,
            errors=list(getattr(record, "errors", []) or []),
        )
        if progress.get("complete"):
            path = _write_youtube_report(audit_store, parent_job_id, channel_name)
            if path:
                audit_store.bump_ingest_job_progress(parent_job_id, report_path=path)
    return progress


async def ingest_youtube_channel_task(
    ctx: dict, channel_name: str, topic: str = "", job_id: str = "", batch_size: int = 0
) -> dict:
    """Planner: discover a channel once, then fan out into bounded batch jobs.

    Discovery can't be batched (it enumerates the whole channel), so it runs
    here once; the IDs are split into ``YT_INGEST_BATCH_SIZE`` chunks (default
    20) and each is enqueued as an ``ingest_youtube_batch_task``. With
    ``max_jobs=1`` the worker runs them strictly one at a time — deliberate,
    since concurrent transcript fetches degrade the shared proxy pool.
    """
    import asyncio
    import os

    audit_store = ctx.get("audit_store") or _worker_audit_store()
    orch = _build_orchestrator(audit_store)
    bs = batch_size or int(os.environ.get("YT_INGEST_BATCH_SIZE", "20"))

    if job_id:
        audit_store.update_ingest_job(job_id, status="running")
    try:
        ids, prov = await asyncio.to_thread(orch.discover_youtube, channel_name, topic)
    except BaseException as exc:  # noqa: BLE001 - finalise then re-raise
        if job_id:
            audit_store.update_ingest_job(
                job_id, status="failed",
                error=f"discovery failed: {type(exc).__name__}: {exc}"[:1000],
            )
        raise

    if not ids:
        if job_id:
            audit_store.update_ingest_job(
                job_id, status="failed",
                error=f"Channel discovery returned no videos for '{channel_name}'.",
            )
        return {"mode": "batched", "videos": 0, "batches": 0, "job_id": job_id}

    batches = [ids[i:i + bs] for i in range(0, len(ids), bs)]
    if job_id:
        audit_store.init_ingest_job_batches(
            job_id, videos_total=len(ids), batches_total=len(batches)
        )

    redis = ctx.get("redis")
    for bi, batch_ids in enumerate(batches):
        batch_videos = [{"id": vid, "prov": prov.get(vid, {})} for vid in batch_ids]
        if redis is not None:
            await redis.enqueue_job(
                "ingest_youtube_batch_task",
                channel_name, topic, batch_videos, job_id, bi, len(batches),
            )
        else:
            # No arq redis in ctx (dev fallback) — run the batch inline.
            record = await asyncio.to_thread(
                orch.ingest_youtube_batch, channel_name, topic, batch_videos
            )
            _handle_batch_result(audit_store, job_id, bi, channel_name, record)

    logger.info(
        "YouTube planner: channel=%s discovered=%d, dispatched %d batch(es) of <=%d",
        channel_name, len(ids), len(batches), bs,
    )
    return {"mode": "batched", "videos": len(ids), "batches": len(batches), "job_id": job_id}


async def ingest_youtube_batch_task(
    ctx: dict,
    channel_name: str,
    topic: str,
    batch_videos: list,
    parent_job_id: str = "",
    batch_index: int = 0,
    batch_total: int = 0,
) -> dict:
    """Ingest one bounded batch of videos and fold its outcome into the parent."""
    import asyncio

    audit_store = ctx.get("audit_store") or _worker_audit_store()
    orch = _build_orchestrator(audit_store)
    try:
        record = await asyncio.to_thread(
            orch.ingest_youtube_batch, channel_name, topic, batch_videos
        )
        _handle_batch_result(audit_store, parent_job_id, batch_index, channel_name, record)
        return record.to_audit()
    except BaseException as exc:  # noqa: BLE001 - record failure, then re-raise
        # Still count the batch as done (failed) so the channel can complete,
        # and record every video in it as failed so the report is complete.
        if parent_job_id:
            try:
                failed = [
                    {"video_id": v.get("id", ""), "status": "failed",
                     "reason": f"batch error: {type(exc).__name__}", "chunks": 0}
                    for v in (batch_videos or [])
                ]
                audit_store.log_video_outcomes(parent_job_id, batch_index, failed)
                progress = audit_store.bump_ingest_job_progress(
                    parent_job_id, batches_done_increment=1,
                    counts={"failed": len(failed)},
                    errors=[f"batch {batch_index} crashed: {type(exc).__name__}: {exc}"],
                )
                if progress.get("complete"):
                    _write_youtube_report(audit_store, parent_job_id, channel_name)
            except Exception:  # noqa: BLE001
                logger.exception("Batch failure bookkeeping failed for %s", parent_job_id)
        raise


# ── Worker lifecycle ────────────────────────────────────────────────────────


async def _on_startup(ctx: dict) -> None:
    """Give the worker its own AuditStore (separate process).

    Also reap any ingest job left in 'running' by a previous worker that exited
    or was restarted mid-job — those rows are orphaned (a fresh worker never
    resumes an in-flight job) and would otherwise sit at 'running' forever.
    """
    store = _worker_audit_store()
    ctx["audit_store"] = store
    try:
        reaped = store.fail_orphaned_running_jobs()
        if reaped:
            logger.warning(
                "Startup reaper: marked %d orphaned 'running' ingest job(s) "
                "as failed (previous worker exit/restart).",
                reaped,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Startup reaper failed; continuing worker startup")
    logger.info("arq worker started; audit store ready")


async def _on_shutdown(ctx: dict) -> None:
    logger.info("arq worker shutting down")


def _redis_settings():
    """Build arq RedisSettings from the configured URL."""
    from arq.connections import RedisSettings  # type: ignore[import]
    from apps.api.config import get_settings

    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    """arq worker entrypoint.

    Run with::

        arq apps.api.queue.WorkerSettings
    """

    functions = [
        ingest_pubmed_task,
        ingest_youtube_task,
        ingest_youtube_channel_task,
        ingest_youtube_batch_task,
        ingest_website_task,
        ingest_skool_task,
    ]
    on_startup = _on_startup
    on_shutdown = _on_shutdown

    # Process one job at a time. Deliberate: concurrent transcript fetches
    # degrade the shared residential-proxy pool — the failure that motivated the
    # batched ingest path. With batches enqueued upfront, max_jobs=1 runs them
    # strictly sequentially while still giving per-batch retries and progress.
    max_jobs = 1

    @property
    def redis_settings(self):  # arq reads this at class level via instance
        return _redis_settings()

    # arq reads job_timeout from the class; resolved lazily at import-free time.
    @staticmethod
    def _timeout() -> int:
        from apps.api.config import get_settings
        return get_settings().arq_job_timeout


# arq inspects class attributes, so expose redis_settings/job_timeout statically.
def _install_worker_class_attrs() -> None:
    """Populate WorkerSettings class attributes from config at import time.

    Done in a function so importing this module never fails when Redis/arq
    config is absent (e.g. in unit tests that only import task functions).
    """
    try:
        WorkerSettings.redis_settings = _redis_settings()  # type: ignore[assignment]
        from apps.api.config import get_settings
        WorkerSettings.job_timeout = get_settings().arq_job_timeout  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Worker class attrs not installed at import: %s", exc)


# ── Enqueue helper (the single decision point used by routes) ───────────────


_pool = None  # cached arq redis pool


async def get_pool():
    """Return a cached arq Redis pool, creating it on first use."""
    global _pool
    if _pool is None:
        from arq import create_pool  # type: ignore[import]
        _pool = await create_pool(_redis_settings())
    return _pool


async def enqueue_or_run(
    *,
    settings: Any,
    task_name: str,
    task_args: tuple,
    fallback: Callable[[], Awaitable[None]] | Callable[[], None],
    background_tasks: Optional[Any] = None,
) -> dict:
    """Enqueue a task on arq, or fall back to in-process execution.

    Args:
        settings: App Settings (used to check ``queue_enabled``).
        task_name: Name of the arq task function (e.g. "ingest_pubmed_task").
        task_args: Positional args passed to the task after ctx.
        fallback: A no-arg callable to run the work in-process when the queue
            is disabled. If ``background_tasks`` is given, it is scheduled
            there; otherwise it is called directly.
        background_tasks: Optional FastAPI BackgroundTasks for the fallback.

    Returns:
        Dict describing how the work was dispatched (mode + job id).
    """
    if settings.queue_enabled:
        pool = await get_pool()
        job = await pool.enqueue_job(task_name, *task_args)
        return {"mode": "queued", "arq_job_id": job.job_id if job else None}

    # Fallback: no Redis configured.
    logger.warning(
        "REDIS_URL not set — running '%s' in-process (dev fallback, not durable).",
        task_name,
    )
    if background_tasks is not None:
        background_tasks.add_task(fallback)
    else:
        result = fallback()
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]
    return {"mode": "in_process"}


_install_worker_class_attrs()
