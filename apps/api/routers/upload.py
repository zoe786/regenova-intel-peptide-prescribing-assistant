"""File upload and URL ingestion endpoints (admin-only).

Provides:
- POST /upload/documents  — upload one or more PDF/TXT/MD files into the
                            raw documents directory and trigger DocumentIngestor.
- POST /upload/url        — register a URL / video ID for website, youtube,
                            pubmed, forum, or blog ingestion.
                            (Skool ingestion is file-export based in Phase 1.)

All endpoints require X-Admin-Key header and log audit events.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlparse

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
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_PMID_RE = re.compile(r"^\d{4,10}$")

# Supported source types and which raw-directory file to append URLs to
_URL_SOURCE_MAP: dict[str, tuple[str, str]] = {
    "website":        ("websites", "urls.txt"),
    "blog":           ("websites", "urls.txt"),
    "youtube":        ("youtube",  "video_ids.txt"),
    "forum":          ("forums",   "urls.txt"),
    "pubmed":         ("pubmed",   "pmids.txt"),
}

_SOURCE_TYPE_ALIASES: dict[str, str] = {
    "skool_course": "skool_courses",
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
        description=(
            "One of: website, blog, youtube, forum, pubmed. "
            "Skool sources are file-export based and should be ingested from "
            "data/raw/skool/courses or data/raw/skool/community."
        ),
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
        quarantined_documents = list(getattr(result, "quarantined_documents", []) or [])
        if quarantined_documents:
            audit_store.log_pdf_quarantine_records(quarantined_documents, job_id=job_id)
        audit_store.update_ingest_job(
            job_id,
            status="completed" if result.success else "failed",
            total_chunks=result.count,
            results={"documents": {
                "count": result.count,
                "errors": result.errors,
                "quarantined_documents": quarantined_documents,
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
                "skipped": result.skipped,
                "success": result.success,
                "error_count": len(result.errors),
                "errors": result.errors,
                "duration_seconds": round(result.duration_seconds, 2),
            }},
            error=result.errors[0] if result.errors else None,
        )
    except Exception as exc:
        logger.error("URL ingest task for %s failed: %s", source_type, exc)
        audit_store.update_ingest_job(job_id, status="failed", error=str(exc))


# ── Endpoints ──────────────────────────────────────────────────────────────────

def _canonical_source_type(source_type: str) -> str:
    return _SOURCE_TYPE_ALIASES.get(source_type.strip(), source_type.strip())


def _parse_existing_list_values(list_path: Path) -> set[str]:
    if not list_path.exists():
        return set()
    values: set[str] = set()
    for line in list_path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            values.add(value)
    return values


def _normalize_registration_value(value: str, source_type: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise HTTPException(
            status_code=422,
            detail={"message": "Input cannot be empty", "source_type": source_type},
        )

    if source_type in {"website", "blog", "forum"}:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Expected a valid http(s) URL for this source type",
                    "source_type": source_type,
                    "value": candidate,
                },
            )
        return candidate

    if source_type == "youtube":
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            host = parsed.netloc.lower()
            if "youtu.be" in host:
                candidate = parsed.path.strip("/")
            else:
                candidate = parse_qs(parsed.query).get("v", [candidate])[0]
        if not _YOUTUBE_ID_RE.fullmatch(candidate):
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Expected a YouTube video ID (or URL containing a valid video ID)",
                    "source_type": source_type,
                    "value": value.strip(),
                },
            )
        return candidate

    if source_type == "pubmed":
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            parts = [p for p in parsed.path.split("/") if p]
            if parsed.hostname and parsed.hostname.lower() == "pubmed.ncbi.nlm.nih.gov" and parts:
                candidate = parts[0]
        if not _PMID_RE.fullmatch(candidate):
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Expected a numeric PubMed PMID (4-10 digits)",
                    "source_type": source_type,
                    "value": value.strip(),
                },
            )
        return candidate

    return candidate

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
    canonical_source_type = _canonical_source_type(body.source_type)
    if canonical_source_type in {"skool_courses", "skool_community"}:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"'{canonical_source_type}' ingestion uses exported files, not URL registration. "
                    "Place exports under data/raw/skool/courses or data/raw/skool/community and trigger ingestion."
                ),
                "source_type": canonical_source_type,
            },
        )

    if canonical_source_type not in _URL_SOURCE_MAP:
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Unsupported source_type '{body.source_type}'",
                "supported": list(_URL_SOURCE_MAP.keys()),
            },
        )

    normalized_value = _normalize_registration_value(body.url, canonical_source_type)

    subdir, list_filename = _URL_SOURCE_MAP[canonical_source_type]
    list_path = Path(settings.raw_data_dir) / subdir / list_filename
    list_path.parent.mkdir(parents=True, exist_ok=True)

    # Append URL (avoid duplicates)
    existing = _parse_existing_list_values(list_path)
    existing_casefolded = {item.casefold() for item in existing}

    if normalized_value.casefold() in existing_casefolded:
        return {
            "message": "Source already registered; skipping duplicate ingestion trigger",
            "url": normalized_value,
            "source_type": canonical_source_type,
            "status": "skipped_duplicate",
            "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    with list_path.open("a", encoding="utf-8") as fh:
        label_comment = f"  # {body.label}" if body.label else ""
        fh.write(f"\n{normalized_value}{label_comment}")

    job_id = audit_store.log_ingest_job(source_type=canonical_source_type)

    client_ip = request.client.host if request.client else ""
    audit_store.log_event(
        event_type="upload",
        data={
            "url": normalized_value,
            "source_type": canonical_source_type,
            "evidence_tier_override": body.evidence_tier_override,
            "label": body.label,
            "job_id": job_id,
        },
        role="admin",
        ip=client_ip,
    )

    background_tasks.add_task(
        _ingest_url_task,
        canonical_source_type,
        settings.chroma_persist_dir,
        audit_store,
        job_id,
    )

    logger.info(
        "URL registered for ingestion: type=%s url=%s job_id=%s",
        canonical_source_type,
        normalized_value,
        job_id,
    )

    return {
        "message": f"Source registered and {canonical_source_type} ingestion queued",
        "job_id": job_id,
        "url": normalized_value,
        "source_type": canonical_source_type,
        "triggered_at": datetime.now(tz=timezone.utc).isoformat(),
    }
