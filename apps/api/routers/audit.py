"""Audit log and ingest job query endpoints (admin-only).

Provides:
- GET /audit/logs                    — paginated audit event list with filters
- GET /audit/ingest-jobs             — paginated ingest job history
- GET /audit/ingest-jobs/{job_id}    — single ingest job detail

All endpoints require X-Admin-Key header.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from apps.api.config import Settings, get_settings
from apps.api.services.audit_store import AuditStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/audit", tags=["Audit"])


# ── Auth dependency ────────────────────────────────────────────────────────────

def _require_admin_key(
    x_admin_key: Annotated[str, Header(description="Admin API key")],
    settings: Settings = Depends(get_settings),
) -> None:
    if x_admin_key != settings.admin_api_key:
        logger.warning("Invalid admin key for audit endpoint")
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Invalid admin API key"},
        )


def _get_audit_store(request: Request) -> AuditStore:
    return request.app.state.audit_store


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get(
    "/logs",
    summary="Query audit event log (admin only)",
    description=(
        "Returns paginated audit events.  Filterable by event_type, role, "
        "ISO-8601 date range, and request_id prefix."
    ),
)
async def get_audit_logs(
    event_type: str | None = None,
    role: str | None = None,
    since: str | None = None,
    until: str | None = None,
    request_id_prefix: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _: None = Depends(_require_admin_key),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Return filtered, paginated audit events.

    Query params:
        event_type: chat_query | upload | ingest_trigger | admin_action
        role: clinician | researcher | admin
        since: ISO-8601 lower-bound timestamp (inclusive)
        until: ISO-8601 upper-bound timestamp (inclusive)
        request_id_prefix: Filter events whose request_id starts with this string
        limit: Page size (max 500)
        offset: Pagination offset
    """
    limit = min(max(1, limit), 500)
    offset = max(0, offset)

    events = audit_store.list_events(
        event_type=event_type,
        role=role,
        since=since,
        until=until,
        request_id_prefix=request_id_prefix,
        limit=limit,
        offset=offset,
    )
    total = audit_store.count_events(
        event_type=event_type,
        role=role,
        since=since,
        until=until,
    )

    # Parse the stored JSON data column back to dict for the response
    import json
    for ev in events:
        try:
            ev["data"] = json.loads(ev.get("data") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "events": events,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/ingest-jobs",
    summary="List ingest job history (admin only)",
)
async def list_ingest_jobs(
    source_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(_require_admin_key),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Return paginated ingest job records.

    Query params:
        source_type: Filter by source type (all | documents | websites | youtube | …)
        status: Filter by status (queued | running | completed | failed)
        limit: Page size (max 200)
        offset: Pagination offset
    """
    limit = min(max(1, limit), 200)
    offset = max(0, offset)

    jobs = audit_store.list_ingest_jobs(
        source_type=source_type,
        status=status,
        limit=limit,
        offset=offset,
    )

    return {
        "jobs": jobs,
        "count": len(jobs),
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/ingest-jobs/{job_id}",
    summary="Get a single ingest job by ID (admin only)",
)
async def get_ingest_job(
    job_id: str,
    _: None = Depends(_require_admin_key),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Return full detail for a single ingest job, including per-source breakdown."""
    job = audit_store.get_ingest_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"message": f"Ingest job '{job_id}' not found"},
        )
    return job
