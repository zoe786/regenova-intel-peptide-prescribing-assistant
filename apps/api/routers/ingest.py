"""Ingest management endpoints (admin-only).

Provides endpoints to trigger pipeline ingestion and check run status.
All endpoints require the X-Admin-Key header.

Audit persistence:
- Every trigger creates an ingest_job record via AuditStore.
- Job status is updated to running / completed / failed on completion.
"""

from __future__ import annotations

import importlib
import logging
import time
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

from apps.api.config import Settings, get_settings
from apps.api.services.audit_store import AuditStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["Ingest"])

_INGESTOR_MAP: dict[str, tuple[str, str]] = {
    "documents":       ("pipelines.ingest_documents",       "DocumentIngestor"),
    "websites":        ("pipelines.ingest_websites",        "WebsiteIngestor"),
    "youtube":         ("pipelines.ingest_youtube",         "YouTubeIngestor"),
    "pubmed":          ("pipelines.ingest_pubmed",          "PubMedIngestor"),
    "forums":          ("pipelines.ingest_forums",          "ForumIngestor"),
    "skool_courses":   ("pipelines.ingest_skool_courses",   "SkoolCourseIngestor"),
    "skool_community": ("pipelines.ingest_skool_community", "SkoolCommunityIngestor"),
}


def _require_admin_key(
    x_admin_key: Annotated[str, Header(description="Admin API key")],
    settings: Settings = Depends(get_settings),
) -> None:
    """Dependency: validate admin API key from X-Admin-Key header."""
    if x_admin_key != settings.admin_api_key:
        logger.warning("Invalid admin API key attempt")
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Invalid admin API key"},
        )


def _get_audit_store(request: Request) -> AuditStore:
    return request.app.state.audit_store


def _run_all_ingestion_task(audit_store: AuditStore, job_id: str) -> None:
    """Background: run all ingestors and persist results."""
    audit_store.update_ingest_job(job_id, status="running")
    start = time.time()
    try:
        from pipelines.run_all_ingestion import RunAllIngestion  # type: ignore[import]
        summary = RunAllIngestion().run()
        audit_store.update_ingest_job(
            job_id, status="completed",
            total_chunks=summary.get("total_chunks", 0),
            results=summary,
        )
        logger.info("Full ingestion completed in %.1fs", time.time() - start)
    except ImportError:
        logger.warning("Pipeline modules not available — ingestion skipped")
        audit_store.update_ingest_job(
            job_id, status="completed",
            results={"note": "Pipeline modules not installed"},
        )
    except Exception as exc:
        logger.error("Full ingestion failed: %s", exc)
        audit_store.update_ingest_job(job_id, status="failed", error=str(exc))


def _run_single_ingestion_task(
    source_type: str, audit_store: AuditStore, job_id: str
) -> None:
    """Background: run a single named ingestor and persist results."""
    audit_store.update_ingest_job(job_id, status="running")
    start = time.time()
    try:
        module_path, class_name = _INGESTOR_MAP[source_type]
        try:
            mod = importlib.import_module(module_path)
            ingestor_cls = getattr(mod, class_name)
        except (ImportError, AttributeError) as load_exc:
            raise RuntimeError(
                f"Could not load ingestor '{class_name}' from '{module_path}': {load_exc}"
            ) from load_exc
        result = ingestor_cls().run()
        audit_store.update_ingest_job(
            job_id,
            status="completed" if result.success else "failed",
            total_chunks=result.count,
            results={source_type: {
                "count": result.count,
                "errors": result.errors,
                "duration_seconds": round(result.duration_seconds, 2),
            }},
            error=result.errors[0] if result.errors else None,
        )
        logger.info(
            "Ingestor %s: %d chunks in %.1fs", source_type, result.count, time.time() - start
        )
    except Exception as exc:
        logger.error("Ingestor %s failed: %s", source_type, exc)
        audit_store.update_ingest_job(job_id, status="failed", error=str(exc))


@router.post(
    "/trigger",
    summary="Trigger full ingestion pipeline (admin only)",
    description="Starts all ingestors as a background task. Returns immediately.",
)
async def trigger_ingest(
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_admin_key),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Trigger the full data ingestion pipeline in the background."""
    running_jobs = audit_store.list_ingest_jobs(status="running", limit=1)
    if running_jobs:
        raise HTTPException(
            status_code=409,
            detail={"type": "conflict", "title": "Ingestion already in progress"},
        )

    job_id = audit_store.log_ingest_job(source_type="all")
    client_ip = request.client.host if request.client else ""
    audit_store.log_event(
        event_type="ingest_trigger",
        data={"source_type": "all", "job_id": job_id},
        role="admin",
        ip=client_ip,
    )

    logger.info("Full ingestion triggered via API, job_id=%s", job_id)
    background_tasks.add_task(_run_all_ingestion_task, audit_store, job_id)

    return {
        "message": "Ingestion pipeline triggered",
        "job_id": job_id,
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "queued",
    }


@router.post(
    "/trigger/{source_type}",
    summary="Trigger a specific ingestor (admin only)",
    description=(
        "Starts a single named ingestor as a background task. "
        f"source_type must be one of: {', '.join(sorted(_INGESTOR_MAP))}."
    ),
)
async def trigger_ingest_source(
    source_type: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_admin_key),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Trigger a single named ingestor in the background."""
    if source_type not in _INGESTOR_MAP:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Unknown source_type '{source_type}'",
                "valid": sorted(_INGESTOR_MAP.keys()),
            },
        )

    job_id = audit_store.log_ingest_job(source_type=source_type)
    client_ip = request.client.host if request.client else ""
    audit_store.log_event(
        event_type="ingest_trigger",
        data={"source_type": source_type, "job_id": job_id},
        role="admin",
        ip=client_ip,
    )

    logger.info("Ingestor %s triggered, job_id=%s", source_type, job_id)
    background_tasks.add_task(
        _run_single_ingestion_task, source_type, audit_store, job_id
    )

    return {
        "message": f"Ingestor '{source_type}' triggered",
        "job_id": job_id,
        "source_type": source_type,
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "queued",
    }


@router.get(
    "/status",
    summary="Get ingestion pipeline status (admin only)",
    description="Returns the status of the most recent ingestion run.",
)
async def ingest_status(
    _: None = Depends(_require_admin_key),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Return the most recent ingest job as the current pipeline status."""
    jobs = audit_store.list_ingest_jobs(limit=1)
    if not jobs:
        return {
            "status": "idle",
            "last_run_at": None,
            "last_result": None,
            "error": None,
            "queried_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    job = jobs[0]
    return {
        "status": job.get("status", "idle"),
        "last_run_at": job.get("triggered_at"),
        "last_result": job.get("results"),
        "error": job.get("error"),
        "job_id": job.get("job_id"),
        "queried_at": datetime.now(tz=timezone.utc).isoformat(),
    }
