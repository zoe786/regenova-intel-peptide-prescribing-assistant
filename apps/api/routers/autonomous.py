"""Autonomous ingestion endpoints (admin-only).

Exposes the AutonomousIngestionOrchestrator over HTTP:
- POST /autonomous/pubmed   {peptides: [...]}        → search + ingest PubMed
- POST /autonomous/youtube  {channel_name, topic}    → ingest whole channel
- POST /autonomous/skool                              → ingest JSON exports

All endpoints require X-Admin-Key, run the work as a background task, and
record a provenance audit event. They reuse the same LLM the chat API uses
for query building / relevance triage (configured via Settings).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from apps.api.config import Settings, get_settings
from apps.api.services.audit_store import AuditStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/autonomous", tags=["Autonomous Ingestion"])


def _require_admin_key(
    x_admin_key: Annotated[str, Header(description="Admin API key")],
    settings: Settings = Depends(get_settings),
) -> None:
    import secrets
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


def _pubmed_task(settings: Settings, audit_store: AuditStore, peptides: list[str], n: int, ip: str) -> None:
    from pipelines.autonomous_orchestrator import AutonomousIngestionOrchestrator
    orch = AutonomousIngestionOrchestrator(settings, audit_sink=_make_audit_sink(audit_store, ip))
    orch.run_pubmed(peptides, max_results_per_peptide=n)


def _youtube_task(settings: Settings, audit_store: AuditStore, channel: str, topic: str, ip: str) -> None:
    from pipelines.autonomous_orchestrator import AutonomousIngestionOrchestrator
    orch = AutonomousIngestionOrchestrator(settings, audit_sink=_make_audit_sink(audit_store, ip))
    orch.run_youtube(channel, topic=topic)


def _skool_task(settings: Settings, audit_store: AuditStore, ip: str) -> None:
    from pipelines.autonomous_orchestrator import AutonomousIngestionOrchestrator
    orch = AutonomousIngestionOrchestrator(settings, audit_sink=_make_audit_sink(audit_store, ip))
    orch.run_skool_export()


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
    background_tasks.add_task(
        _pubmed_task, settings, audit_store, body.peptides, body.max_results_per_peptide, ip
    )
    return {
        "message": f"Autonomous PubMed ingestion queued for {len(body.peptides)} peptide(s)",
        "peptides": body.peptides,
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
    background_tasks.add_task(_youtube_task, settings, audit_store, body.channel_name, body.topic, ip)
    return {
        "message": f"Autonomous YouTube ingestion queued for channel '{body.channel_name}'",
        "channel_name": body.channel_name,
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
    background_tasks.add_task(_skool_task, settings, audit_store, ip)
    return {
        "message": "Skool export ingestion queued",
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "queued",
    }
