"""Health check endpoints.

Provides liveness (/health) and readiness (/health/ready) probes
suitable for use with Docker, Kubernetes, or load balancers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from apps.api.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["Health"])


@router.get("", summary="Liveness probe")
async def health() -> dict:
    """Return basic liveness status.

    Always returns 200 OK if the API process is running.
    Use /health/ready for deeper service checks.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "version": settings.version,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "environment": settings.environment,
    }


@router.get("/ready", summary="Readiness probe")
async def health_ready() -> dict:
    """Return readiness status by checking downstream service connectivity.

    Checks:
    - Vector store (ChromaDB) accessibility

    Returns 200 if all services are ready, or a degraded status if any
    service is unavailable (does not raise 500 — caller decides on action).
    """
    settings = get_settings()
    checks: dict[str, str] = {}

    # Check vector store
    try:
        from apps.api.services.retrieval_service import RetrievalService
        svc = RetrievalService(chroma_persist_dir=settings.chroma_persist_dir)
        if svc.is_ready():
            checks["vector_store"] = "ok"
        else:
            checks["vector_store"] = "degraded"
    except Exception as exc:
        logger.warning("Vector store readiness check failed: %s", exc)
        checks["vector_store"] = f"unavailable: {exc}"

    # TODO: Add Neo4j connectivity check when graph retrieval is enabled

    all_ok = all(v == "ok" for v in checks.values())
    overall = "ready" if all_ok else "degraded"

    return {
        "status": overall,
        "checks": checks,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
