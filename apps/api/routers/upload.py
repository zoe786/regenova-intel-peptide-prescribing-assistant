"""File upload and URL ingestion endpoints (admin-only).

Provides:
- POST /upload/documents  — upload one or more PDF/TXT/MD files into the
                            raw documents directory and trigger DocumentIngestor.
- POST /upload/url        — register a URL / video ID for website, youtube,
                            pubmed, forum, blog, skool_courses, or
                            skool_community ingestion.

All endpoints require X-Admin-Key header and log audit events.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from apps.api.config import Settings, get_settings
from apps.api.services.audit_store import AuditStore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["Upload"])

_ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# Supported source types and which raw-directory file to append URLs to
_URL_SOURCE_MAP: dict[str, tuple[str, str]] = {
    "website":        ("websites", "urls.txt"),
    "blog":           ("websites", "urls.txt"),
    "youtube":        ("youtube",  "video_ids.txt"),
    "forum":          ("forums",   "urls.txt"),
    "pubmed":         ("pubmed",   "pmids.txt"),
    "skool_courses":  ("skool_courses",   "course_urls.txt"),
    "skool_community":("skool_community", "community_urls.txt"),
}


# ── Auth dependency ────────────────────────────────────────────────────────────

def _require_admin_key(
    x_admin_key: Annotated[str, Header(description="Admin API key")],
    settings: Settings = Depends(get_settings),
) -> None:
    if x_admin_key != settings.admin_api_key:
        logger.warning("Invalid admin key for upload endpoint")
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Invalid admin API key"},
        )


def _get_audit_store(request: Request) -> AuditStore:
    return request.app.state.audit_store


# ── Request schema ─────────────────────────────────────────────────────────────

class UrlIngestRequest(BaseModel):
    """Request body for URL / video-ID / PMID ingestion."""

    url: str = Field(..., min_length=3, description="URL, YouTube video ID, or PubMed PMID")
    source_type: str = Field(
        ...,
        description="One of: website, blog, youtube, forum, pubmed, skool_courses, skool_community",
    )
    evidence_tier_override: int | None = Field(
        default=None, ge=1, le=5,
        description="Override the default evidence tier for this source",
    )
    label: str | None = Field(
        default=None,
        description="Optional human-readable label for this source",
    )


# ── Background tasks ───────────────────────────────────────────────────────────

def _ingest_documents_task(
    raw_dir: str,
    chroma_persist_dir: str,
    audit_store: AuditStore,
    job_id: str,
) -> None:
    """Background task: run DocumentIngestor and record audit result."""
    audit_store.update_ingest_job(job_id, status="running")
    try:
        from pipelines.ingest_documents import DocumentIngestor  # type: ignore[import]
        result = DocumentIngestor(
            raw_dir=Path(raw_dir),
            chroma_persist_dir=chroma_persist_dir,
        ).run()
        audit_store.update_ingest_job(
            job_id,
            status="completed" if result.success else "failed",
            total_chunks=result.count,
            results={"documents": {
                "count": result.count,
                "errors": result.errors,
                "duration_seconds": round(result.duration_seconds, 2),
            }},
            error=result.errors[0] if result.errors else None,
        )
    except Exception as exc:
        logger.error("Document ingest task failed: %s", exc)
        audit_store.update_ingest_job(job_id, status="failed", error=str(exc))


def _ingest_url_task(
    source_type: str,
    chroma_persist_dir: str,
    audit_store: AuditStore,
    job_id: str,
) -> None:
    """Background task: run the appropriate ingestor for a URL source type."""
    audit_store.update_ingest_job(job_id, status="running")
    _INGESTOR_MAP = {
        "website":         "pipelines.ingest_websites:WebsiteIngestor",
        "blog":            "pipelines.ingest_websites:WebsiteIngestor",
        "youtube":         "pipelines.ingest_youtube:YouTubeIngestor",
        "forum":           "pipelines.ingest_forums:ForumIngestor",
        "pubmed":          "pipelines.ingest_pubmed:PubMedIngestor",
        "skool_courses":   "pipelines.ingest_skool_courses:SkoolCourseIngestor",
        "skool_community": "pipelines.ingest_skool_community:SkoolCommunityIngestor",
    }
    try:
        module_path, class_name = _INGESTOR_MAP[source_type].split(":")
        import importlib
        mod = importlib.import_module(module_path)
        ingestor_cls = getattr(mod, class_name)
        result = ingestor_cls(chroma_persist_dir=chroma_persist_dir).run()
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
    except Exception as exc:
        logger.error("URL ingest task for %s failed: %s", source_type, exc)
        audit_store.update_ingest_job(job_id, status="failed", error=str(exc))


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post(
    "/documents",
    summary="Upload document files for RAG ingestion (admin only)",
    description=(
        "Accepts one or more PDF, TXT, or MD files. Files are saved to the raw "
        "documents directory and DocumentIngestor is triggered in the background."
    ),
)
async def upload_documents(
    request: Request,
    background_tasks: BackgroundTasks,
    files: list[UploadFile],
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Accept uploaded files, validate them, save to raw dir, trigger ingest."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    raw_dir = Path(settings.raw_data_dir) / "documents"
    raw_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    rejected: list[dict] = []

    for upload in files:
        filename = upload.filename or "unknown"
        suffix = Path(filename).suffix.lower()

        if suffix not in _ALLOWED_EXTENSIONS:
            rejected.append({"filename": filename, "reason": f"Unsupported extension '{suffix}'"})
            continue

        # Check Content-Length from headers when available to avoid reading oversized files
        content_length = upload.headers.get("content-length") if upload.headers else None
        if content_length and int(content_length) > _MAX_FILE_SIZE:
            rejected.append({"filename": filename, "reason": "File exceeds 50 MB limit"})
            continue

        # Read file content in a bounded way
        content = await upload.read(_MAX_FILE_SIZE + 1)
        if len(content) > _MAX_FILE_SIZE:
            rejected.append({"filename": filename, "reason": "File exceeds 50 MB limit"})
            continue

        dest = raw_dir / filename
        dest.write_bytes(content)
        saved.append({"filename": filename, "size_bytes": len(content)})
        logger.info("Uploaded document: %s (%d bytes)", filename, len(content))

    if not saved:
        raise HTTPException(
            status_code=422,
            detail={"rejected": rejected, "message": "No valid files were saved"},
        )

    job_id = audit_store.log_ingest_job(source_type="documents")

    # Log audit event
    client_ip = request.client.host if request.client else ""
    audit_store.log_event(
        event_type="upload",
        data={"files": saved, "rejected": rejected, "job_id": job_id},
        role="admin",
        ip=client_ip,
    )

    background_tasks.add_task(
        _ingest_documents_task,
        str(raw_dir),
        settings.chroma_persist_dir,
        audit_store,
        job_id,
    )

    return {
        "message": f"{len(saved)} file(s) uploaded and ingestion queued",
        "job_id": job_id,
        "saved": saved,
        "rejected": rejected,
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
    }


@router.post(
    "/url",
    summary="Register a URL/video ID for ingestion (admin only)",
    description=(
        "Appends the supplied URL or identifier to the appropriate raw source "
        "list file and triggers the matching ingestor in the background."
    ),
)
async def upload_url(
    request: Request,
    body: UrlIngestRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Register a URL for ingestion and trigger the appropriate pipeline."""
    if body.source_type not in _URL_SOURCE_MAP:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Unsupported source_type '{body.source_type}'",
                "supported": list(_URL_SOURCE_MAP.keys()),
            },
        )

    subdir, list_filename = _URL_SOURCE_MAP[body.source_type]
    list_path = Path(settings.raw_data_dir) / subdir / list_filename
    list_path.parent.mkdir(parents=True, exist_ok=True)

    # Append URL (avoid duplicates)
    existing: set[str] = set()
    if list_path.exists():
        existing = {ln.strip() for ln in list_path.read_text().splitlines() if ln.strip()}

    if body.url in existing:
        raise HTTPException(
            status_code=409,
            detail={"message": "URL already registered", "url": body.url},
        )

    with list_path.open("a", encoding="utf-8") as fh:
        label_comment = f"  # {body.label}" if body.label else ""
        fh.write(f"\n{body.url}{label_comment}")

    job_id = audit_store.log_ingest_job(source_type=body.source_type)

    client_ip = request.client.host if request.client else ""
    audit_store.log_event(
        event_type="upload",
        data={
            "url": body.url,
            "source_type": body.source_type,
            "evidence_tier_override": body.evidence_tier_override,
            "label": body.label,
            "job_id": job_id,
        },
        role="admin",
        ip=client_ip,
    )

    background_tasks.add_task(
        _ingest_url_task,
        body.source_type,
        settings.chroma_persist_dir,
        audit_store,
        job_id,
    )

    logger.info(
        "URL registered for ingestion: type=%s url=%s job_id=%s",
        body.source_type,
        body.url,
        job_id,
    )

    return {
        "message": f"URL registered and {body.source_type} ingestion queued",
        "job_id": job_id,
        "url": body.url,
        "source_type": body.source_type,
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
    }
