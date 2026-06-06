"""Autonomous ingestion endpoints (admin-only).

Exposes the AutonomousIngestionOrchestrator over HTTP. Long-running work is
dispatched to the arq task queue when REDIS_URL is configured (durable,
survives restarts); otherwise it falls back to in-process BackgroundTasks for
local development.

- POST /autonomous/pubmed   {peptides: [...]}        -> search + ingest PubMed
- POST /autonomous/youtube  {channel_name, topic}    -> ingest whole channel
- POST /autonomous/skool                              -> ingest JSON exports

All endpoints require X-Admin-Key, create an ingest_job record so progress is
visible via /audit/ingest-jobs, and record a provenance audit event.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from apps.api.config import Settings, get_settings
from apps.api.queue import enqueue_or_run
from apps.api.services.audit_store import AuditStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/autonomous", tags=["Autonomous Ingestion"])


def _require_admin_key(
    x_admin_key: Annotated[str, Header(description="Admin API key")],
    settings: Settings = Depends(get_settings),
) -> None:
    if not secrets.compare_digest(x_admin_key or "", settings.admin_api_key):
        logger.warning("Invalid admin key for autonomous endpoint")
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Invalid admin API key"},
        )


def _get_audit_store(request: Request) -> AuditStore:
    return request.app.state.audit_store


class PubMedAutonomousRequest(BaseModel):
    peptides: list[str] = Field(..., min_length=1, description="Peptides to search PubMed for")
    max_results_per_peptide: int = Field(default=50, ge=1, le=200)


class YouTubeAutonomousRequest(BaseModel):
    channel_name: str = Field(..., min_length=2, description="Channel display name to scrape")
    topic: str = Field(default="", description="Optional topic for relevance triage")


def _make_audit_sink(audit_store: AuditStore, request_ip: str):
    def _sink(record) -> None:
        audit_store.log_event(
            event_type="autonomous_ingest",
            data=record.to_audit(),
            role="admin",
            ip=request_ip,
        )
    return _sink


# In-process fallback runners (used only when REDIS_URL is unset).

def _pubmed_fallback(settings, audit_store, peptides, n, ip, job_id):
    from pipelines.autonomous_orchestrator import AutonomousIngestionOrchestrator
    audit_store.update_ingest_job(job_id, status="running")
    orch = AutonomousIngestionOrchestrator(settings, audit_sink=_make_audit_sink(audit_store, ip))
    record = orch.run_pubmed(peptides, max_results_per_peptide=n)
    audit_store.update_ingest_job(
        job_id, status=record.status, total_chunks=record.chunks_ingested,
        results={"pubmed": record.to_audit()},
        error=record.errors[0] if record.errors else None,
    )


def _youtube_fallback(settings, audit_store, channel, topic, ip, job_id):
    from pipelines.autonomous_orchestrator import AutonomousIngestionOrchestrator
    audit_store.update_ingest_job(job_id, status="running")
    orch = AutonomousIngestionOrchestrator(settings, audit_sink=_make_audit_sink(audit_store, ip))
    record = orch.run_youtube(channel, topic=topic)
    audit_store.update_ingest_job(
        job_id, status=record.status, total_chunks=record.chunks_ingested,
        results={"youtube": record.to_audit()},
        error=record.errors[0] if record.errors else None,
    )


def _skool_fallback(settings, audit_store, ip, job_id):
    from pipelines.autonomous_orchestrator import AutonomousIngestionOrchestrator
    audit_store.update_ingest_job(job_id, status="running")
    orch = AutonomousIngestionOrchestrator(settings, audit_sink=_make_audit_sink(audit_store, ip))
    record = orch.run_skool_export()
    audit_store.update_ingest_job(
        job_id, status=record.status, total_chunks=record.chunks_ingested,
        results={"skool": record.to_audit()},
        error=record.errors[0] if record.errors else None,
    )


@router.post("/pubmed", summary="Autonomously search + ingest PubMed for peptides (admin only)")
async def autonomous_pubmed(
    request: Request,
    body: PubMedAutonomousRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    ip = request.client.host if request.client else ""
    job_id = audit_store.log_ingest_job(source_type="autonomous_pubmed")
    audit_store.log_event(
        event_type="autonomous_trigger",
        data={"source": "pubmed", "peptides": body.peptides, "job_id": job_id},
        role="admin", ip=ip,
    )
    dispatch = await enqueue_or_run(
        settings=settings,
        task_name="ingest_pubmed_task",
        task_args=(body.peptides, body.max_results_per_peptide, job_id),
        fallback=lambda: _pubmed_fallback(
            settings, audit_store, body.peptides, body.max_results_per_peptide, ip, job_id
        ),
        background_tasks=background_tasks,
    )
    return {
        "message": f"Autonomous PubMed ingestion {dispatch['mode']} for {len(body.peptides)} peptide(s)",
        "peptides": body.peptides,
        "job_id": job_id,
        "dispatch": dispatch,
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "queued",
    }


@router.post("/youtube", summary="Autonomously ingest an entire YouTube channel (admin only)")
async def autonomous_youtube(
    request: Request,
    body: YouTubeAutonomousRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    if not settings.youtube_api_key:
        raise HTTPException(
            status_code=422,
            detail={"type": "config_error", "title": "YOUTUBE_API_KEY is not configured"},
        )
    ip = request.client.host if request.client else ""
    job_id = audit_store.log_ingest_job(source_type="autonomous_youtube")
    audit_store.log_event(
        event_type="autonomous_trigger",
        data={"source": "youtube", "channel_name": body.channel_name, "job_id": job_id},
        role="admin", ip=ip,
    )
    dispatch = await enqueue_or_run(
        settings=settings,
        task_name="ingest_youtube_task",
        task_args=(body.channel_name, body.topic, job_id),
        fallback=lambda: _youtube_fallback(
            settings, audit_store, body.channel_name, body.topic, ip, job_id
        ),
        background_tasks=background_tasks,
    )
    return {
        "message": f"Autonomous YouTube ingestion {dispatch['mode']} for channel '{body.channel_name}'",
        "channel_name": body.channel_name,
        "job_id": job_id,
        "dispatch": dispatch,
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "queued",
    }


@router.post("/skool", summary="Ingest Skool community JSON exports (admin only)")
async def autonomous_skool(
    request: Request,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    ip = request.client.host if request.client else ""
    job_id = audit_store.log_ingest_job(source_type="autonomous_skool")
    audit_store.log_event(
        event_type="autonomous_trigger",
        data={"source": "skool", "job_id": job_id},
        role="admin", ip=ip,
    )
    dispatch = await enqueue_or_run(
        settings=settings,
        task_name="ingest_skool_task",
        task_args=(job_id,),
        fallback=lambda: _skool_fallback(settings, audit_store, ip, job_id),
        background_tasks=background_tasks,
    )
    return {
        "message": f"Skool export ingestion {dispatch['mode']}",
        "job_id": job_id,
        "dispatch": dispatch,
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "queued",
    }
