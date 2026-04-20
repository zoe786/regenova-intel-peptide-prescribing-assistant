"""Ingest management endpoints (admin-only).

Provides endpoints to trigger pipeline ingestion and check run status.
All endpoints require the X-Admin-Key header.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException

from apps.api.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["Ingest"])

# Simple in-memory status tracker (replaced by persistent store in production)
_ingest_status: dict[str, Any] = {
    "last_run_at": None,
    "status": "idle",
    "last_result": None,
    "error": None,
}


def _require_admin_key(
    x_admin_key: str = Header(..., description="Admin API key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Dependency: validate admin API key from X-Admin-Key header.

    Raises:
        HTTPException 401: If key is missing or invalid.
    """
    if x_admin_key != settings.admin_api_key:
        logger.warning("Invalid admin API key attempt")
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Invalid admin API key"},
        )


def _run_ingestion_task() -> None:
    """Background task that executes the full ingestion pipeline.

    Updates _ingest_status with timing and result information.
    Errors are caught and recorded rather than propagated.
    """
    global _ingest_status
    _ingest_status["status"] = "running"
    _ingest_status["last_run_at"] = datetime.now(tz=timezone.utc).isoformat()
    _ingest_status["error"] = None
    start = time.time()

    try:
        # Import here to avoid circular dependencies at startup
        from pipelines.run_all_ingestion import RunAllIngestion  # type: ignore[import]

        orchestrator = RunAllIngestion()
        result = orchestrator.run()
        _ingest_status["status"] = "completed"
        _ingest_status["last_result"] = result
        logger.info("Ingestion completed in %.1fs: %s", time.time() - start, result)
    except ImportError:
        logger.warning("Pipeline modules not available — ingestion skipped")
        _ingest_status["status"] = "completed"
        _ingest_status["last_result"] = {"note": "Pipeline modules not installed"}
    except Exception as exc:
        logger.error("Ingestion failed: %s", exc)
        _ingest_status["status"] = "failed"
        _ingest_status["error"] = str(exc)


@router.post(
    "/trigger",
    summary="Trigger full ingestion pipeline (admin only)",
    description="Starts all ingestors as a background task. Returns immediately.",
)
async def trigger_ingest(
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_admin_key),
) -> dict:
    """Trigger the full data ingestion pipeline in the background.

    Returns immediately with a job ID. Poll /ingest/status for progress.

    Args:
        background_tasks: FastAPI background task manager.
        _: Admin key validation dependency.

    Returns:
        Job acknowledgement with current timestamp.
    """
    if _ingest_status.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail={"type": "conflict", "title": "Ingestion already in progress"},
        )

    logger.info("Ingestion triggered via API")
    background_tasks.add_task(_run_ingestion_task)

    return {
        "message": "Ingestion pipeline triggered",
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": "queued",
    }


@router.get(
    "/status",
    summary="Get ingestion pipeline status (admin only)",
    description="Returns the status of the last ingestion run.",
)
async def ingest_status(
    _: None = Depends(_require_admin_key),
) -> dict:
    """Return the current ingestion pipeline status.

    Args:
        _: Admin key validation dependency.

    Returns:
        Dict with status, last_run_at, last_result, and error fields.
    """
    return {
        "status": _ingest_status.get("status", "idle"),
        "last_run_at": _ingest_status.get("last_run_at"),
        "last_result": _ingest_status.get("last_result"),
        "error": _ingest_status.get("error"),
        "queried_at": datetime.now(tz=timezone.utc).isoformat(),
    }
