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


async def ingest_pubmed_task(
    ctx: dict, peptides: list[str], max_results_per_peptide: int = 50, job_id: str = ""
) -> dict:
    """Worker task: autonomous PubMed search + ingest."""
    import asyncio

    audit_store = ctx.get("audit_store") or _worker_audit_store()
    if job_id:
        audit_store.update_ingest_job(job_id, status="running")
    orch = _build_orchestrator(audit_store)
    # The orchestrator is synchronous/blocking; run it off the event loop.
    record = await asyncio.to_thread(
        orch.run_pubmed, peptides, max_results_per_peptide
    )
    if job_id:
        audit_store.update_ingest_job(
            job_id,
            status=record.status,
            total_chunks=record.chunks_ingested,
            results={"pubmed": record.to_audit()},
            error=record.errors[0] if record.errors else None,
        )
    return record.to_audit()


async def ingest_youtube_task(
    ctx: dict, channel_name: str, topic: str = "", job_id: str = ""
) -> dict:
    """Worker task: autonomous YouTube channel ingest."""
    import asyncio

    audit_store = ctx.get("audit_store") or _worker_audit_store()
    if job_id:
        audit_store.update_ingest_job(job_id, status="running")
    orch = _build_orchestrator(audit_store)
    record = await asyncio.to_thread(orch.run_youtube, channel_name, topic)
    if job_id:
        audit_store.update_ingest_job(
            job_id,
            status=record.status,
            total_chunks=record.chunks_ingested,
            results={"youtube": record.to_audit()},
            error=record.errors[0] if record.errors else None,
        )
    return record.to_audit()


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
    import asyncio

    audit_store = ctx.get("audit_store") or _worker_audit_store()
    if job_id:
        audit_store.update_ingest_job(job_id, status="running")
    orch = _build_orchestrator(audit_store)
    record = await asyncio.to_thread(
        orch.run_website, seed_url, evidence_tier, render_js, max_pages,
        cookies, login_url, login_username, login_password,
    )
    if job_id:
        audit_store.update_ingest_job(
            job_id,
            status=record.status,
            total_chunks=record.chunks_ingested,
            results={"website": record.to_audit()},
            error=record.errors[0] if record.errors else None,
        )
    return record.to_audit()


async def ingest_skool_task(ctx: dict, job_id: str = "") -> dict:
    """Worker task: Skool export ingest."""
    import asyncio

    audit_store = ctx.get("audit_store") or _worker_audit_store()
    if job_id:
        audit_store.update_ingest_job(job_id, status="running")
    orch = _build_orchestrator(audit_store)
    record = await asyncio.to_thread(orch.run_skool_export)
    if job_id:
        audit_store.update_ingest_job(
            job_id,
            status=record.status,
            total_chunks=record.chunks_ingested,
            results={"skool": record.to_audit()},
            error=record.errors[0] if record.errors else None,
        )
    return record.to_audit()


# ── Worker lifecycle ────────────────────────────────────────────────────────


async def _on_startup(ctx: dict) -> None:
    """Give the worker its own AuditStore (separate process)."""
    ctx["audit_store"] = _worker_audit_store()
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

    functions = [ingest_pubmed_task, ingest_youtube_task, ingest_website_task, ingest_skool_task]
    on_startup = _on_startup
    on_shutdown = _on_shutdown

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
