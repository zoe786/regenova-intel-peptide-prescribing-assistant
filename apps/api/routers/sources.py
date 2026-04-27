"""Source and chunk management endpoints (admin-only).

Provides CRUD over the knowledge base:
- GET  /sources                   - list unique source documents
- DELETE /sources/{document_id}   - delete all chunks for a document
- GET  /chunks                    - paginated, filtered chunk list
- GET  /chunks/{chunk_id}         - full chunk detail
- DELETE /chunks/{chunk_id}       - delete a specific chunk
- PATCH  /chunks/{chunk_id}       - update chunk metadata

All mutations are logged to the audit store.
All endpoints require X-Admin-Key header.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from apps.api.config import Settings, get_settings
from apps.api.services.audit_store import AuditStore

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Sources & Chunks"])

_CHROMA_COLLECTION = "regenova_intel_chunks"

# Regex that chunk_ids and document_ids must match before use in file paths.
_SAFE_ID_RE = re.compile(r'^[\w\-:.]+$')


def _validate_id_for_path(value: str, field: str) -> None:
    """Raise HTTPException 422 if value contains path-traversal characters."""
    if not _SAFE_ID_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail={
                "type": "invalid_input",
                "title": f"Invalid characters in {field}",
                "detail": (
                    f"{field} must only contain alphanumeric characters, "
                    "hyphens, underscores, colons, or dots."
                ),
            },
        )


def _safe_chunk_path(norm_dir: Path, chunk_id: str) -> Path | None:
    """Return a resolved path for chunk_id.json confined to norm_dir, or None.

    Resolves the candidate path and checks that it is a child of norm_dir.
    This explicit confinement prevents path traversal even if chunk_id
    somehow bypasses the regex check.
    """
    norm_dir_resolved = norm_dir.resolve()
    candidate = (norm_dir / f"{chunk_id}.json").resolve()
    if norm_dir_resolved in candidate.parents:
        return candidate
    logger.warning("Path traversal attempt blocked for chunk_id %r", chunk_id)
    return None


# -- Auth / dependency helpers -------------------------------------------------

def _require_admin_key(
    x_admin_key: Annotated[str, Header(description="Admin API key")],
    settings: Settings = Depends(get_settings),
) -> None:
    if x_admin_key != settings.admin_api_key:
        logger.warning("Invalid admin key for sources endpoint")
        raise HTTPException(
            status_code=401,
            detail={"type": "auth_failed", "title": "Invalid admin API key"},
        )


def _get_audit_store(request: Request) -> AuditStore:
    return request.app.state.audit_store


def _get_collection(settings: Settings) -> Any:
    """Return the ChromaDB collection, raising 503 if unavailable."""
    try:
        import chromadb  # type: ignore[import]
        client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        return client.get_or_create_collection(
            name=_CHROMA_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as exc:
        logger.error("ChromaDB unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "type": "service_unavailable",
                "title": "Vector store unavailable",
                "detail": str(exc),
            },
        )


# -- Request schemas -----------------------------------------------------------

class ChunkPatchRequest(BaseModel):
    """Fields that can be patched on an existing chunk."""

    evidence_tier_override: int | None = Field(
        default=None, ge=1, le=5,
        description="Override the evidence tier for this specific chunk",
    )
    notes: str | None = Field(
        default=None, max_length=500,
        description="Optional curator notes attached to this chunk",
    )


# -- Source endpoints ----------------------------------------------------------

@router.get(
    "/sources",
    summary="List all ingested source documents (admin only)",
    description="Returns one entry per unique document_id with chunk counts and metadata.",
)
async def list_sources(
    source_type: str | None = None,
    evidence_tier: int | None = None,
    limit: int = 100,
    offset: int = 0,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Aggregate ChromaDB collection by document_id to produce a source list."""
    collection = _get_collection(settings)

    try:
        total = collection.count()
        if total == 0:
            return {"sources": [], "total": 0}

        # Fetch metadata in batches of 500 to avoid unbounded memory usage
        PAGE = 500
        all_metadatas: list[dict] = []
        all_ids: list[str] = []
        fetched = 0
        while fetched < total:
            batch = collection.get(
                include=["metadatas"],
                limit=PAGE,
                offset=fetched,
            )
            batch_ids = batch.get("ids") or []
            batch_metas = batch.get("metadatas") or []
            if not batch_ids:
                break
            all_ids.extend(batch_ids)
            all_metadatas.extend(batch_metas)
            fetched += len(batch_ids)

        # Group by document_id
        docs: dict[str, dict] = {}
        for chunk_id_val, meta in zip(all_ids, all_metadatas):
            doc_id = meta.get("document_id", chunk_id_val)
            src_type = meta.get("source_type", "unknown")
            tier = int(meta.get("evidence_tier_default", 3))

            if source_type and src_type != source_type:
                continue
            if evidence_tier and tier != evidence_tier:
                continue

            if doc_id not in docs:
                docs[doc_id] = {
                    "document_id": doc_id,
                    "source_name": meta.get("source_name", "Unknown"),
                    "source_type": src_type,
                    "source_url": meta.get("source_url") or None,
                    "evidence_tier_default": tier,
                    "acquired_at": meta.get("acquired_at"),
                    "chunk_count": 0,
                }
            docs[doc_id]["chunk_count"] += 1

        sorted_docs = sorted(docs.values(), key=lambda d: d["acquired_at"] or "", reverse=True)
        paginated = sorted_docs[offset: offset + limit]

        return {
            "sources": paginated,
            "total": len(sorted_docs),
            "limit": limit,
            "offset": offset,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("list_sources failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete(
    "/sources/{document_id}",
    summary="Delete all chunks for a source document (admin only)",
)
async def delete_source(
    document_id: str,
    request: Request,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Delete every chunk belonging to document_id from vector store and disk."""
    _validate_id_for_path(document_id, "document_id")
    collection = _get_collection(settings)

    try:
        results = collection.get(
            where={"document_id": document_id},
            include=["metadatas"],
        )
        chunk_ids: list[str] = results.get("ids") or []

        if not chunk_ids:
            raise HTTPException(
                status_code=404,
                detail={"message": f"No chunks found for document_id '{document_id}'"},
            )

        collection.delete(ids=chunk_ids)

        # Delete normalized JSON files — use path confinement to prevent traversal
        norm_dir = Path(settings.processed_data_dir) / "normalized"
        deleted_files = 0
        for cid in chunk_ids:
            fpath = _safe_chunk_path(norm_dir, cid)
            if fpath is not None and fpath.exists():
                fpath.unlink()
                deleted_files += 1

        client_ip = request.client.host if request.client else ""
        audit_store.log_event(
            event_type="admin_action",
            data={
                "action": "delete_source",
                "document_id": document_id,
                "chunks_deleted": len(chunk_ids),
                "files_deleted": deleted_files,
            },
            role="admin",
            ip=client_ip,
        )

        logger.info(
            "Deleted source %s: %d chunks, %d files",
            document_id, len(chunk_ids), deleted_files,
        )

        return {
            "message": f"Deleted {len(chunk_ids)} chunk(s) for document_id '{document_id}'",
            "document_id": document_id,
            "chunks_deleted": len(chunk_ids),
            "files_deleted": deleted_files,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("delete_source failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# -- Chunk endpoints -----------------------------------------------------------

@router.get(
    "/chunks",
    summary="List chunks with optional filters (admin only)",
)
async def list_chunks(
    source_type: str | None = None,
    evidence_tier: int | None = None,
    search: str | None = None,
    document_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Return paginated chunks from ChromaDB with optional filters."""
    collection = _get_collection(settings)

    try:
        where: dict | None = None
        conditions = []
        if source_type:
            conditions.append({"source_type": {"$eq": source_type}})
        if evidence_tier:
            conditions.append({"evidence_tier_default": {"$eq": evidence_tier}})
        if document_id:
            conditions.append({"document_id": {"$eq": document_id}})

        if len(conditions) > 1:
            where = {"$and": conditions}
        elif len(conditions) == 1:
            where = conditions[0]

        if search:
            results = collection.query(
                query_texts=[search],
                n_results=min(limit, max(1, collection.count())),
                include=["documents", "metadatas", "distances"],
                **({"where": where} if where else {}),
            )
            ids = results.get("ids", [[]])[0]
            docs = results.get("documents", [[]])[0]
            metas = results.get("metadatas", [[]])[0]
        else:
            get_kwargs: dict[str, Any] = {
                "include": ["documents", "metadatas"],
                "limit": limit,
                "offset": offset,
            }
            if where:
                get_kwargs["where"] = where

            results = collection.get(**get_kwargs)
            ids = results.get("ids") or []
            docs = results.get("documents") or []
            metas = results.get("metadatas") or []

        items = []
        for cid, doc, meta in zip(ids, docs, metas):
            items.append({
                "chunk_id": cid,
                "document_id": meta.get("document_id", ""),
                "source_name": meta.get("source_name", ""),
                "source_type": meta.get("source_type", ""),
                "source_url": meta.get("source_url") or None,
                "evidence_tier_default": int(meta.get("evidence_tier_default", 3)),
                "acquired_at": meta.get("acquired_at"),
                "snippet": doc[:200] + ("\u2026" if len(doc) > 200 else ""),
            })

        return {
            "chunks": items,
            "count": len(items),
            "limit": limit,
            "offset": offset,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("list_chunks failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/chunks/{chunk_id}",
    summary="Get full content and metadata for a chunk (admin only)",
)
async def get_chunk(
    chunk_id: str,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Return the full content and metadata of a single chunk."""
    _validate_id_for_path(chunk_id, "chunk_id")
    collection = _get_collection(settings)

    try:
        result = collection.get(
            ids=[chunk_id],
            include=["documents", "metadatas"],
        )
        ids = result.get("ids") or []
        if not ids:
            raise HTTPException(
                status_code=404,
                detail={"message": f"Chunk '{chunk_id}' not found"},
            )

        meta = (result.get("metadatas") or [{}])[0]
        doc = (result.get("documents") or [""])[0]

        return {
            "chunk_id": chunk_id,
            "document_id": meta.get("document_id", ""),
            "content": doc,
            "metadata": dict(meta),
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("get_chunk failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete(
    "/chunks/{chunk_id}",
    summary="Delete a specific chunk (admin only)",
)
async def delete_chunk(
    chunk_id: str,
    request: Request,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Delete a chunk from the vector store and its normalized JSON file."""
    _validate_id_for_path(chunk_id, "chunk_id")
    collection = _get_collection(settings)

    try:
        existing = collection.get(ids=[chunk_id], include=["metadatas"])
        if not (existing.get("ids") or []):
            raise HTTPException(
                status_code=404,
                detail={"message": f"Chunk '{chunk_id}' not found"},
            )

        meta = (existing.get("metadatas") or [{}])[0]
        collection.delete(ids=[chunk_id])

        norm_dir = Path(settings.processed_data_dir) / "normalized"
        norm_path = _safe_chunk_path(norm_dir, chunk_id)
        file_deleted = False
        if norm_path is not None and norm_path.exists():
            norm_path.unlink()
            file_deleted = True

        client_ip = request.client.host if request.client else ""
        audit_store.log_event(
            event_type="admin_action",
            data={
                "action": "delete_chunk",
                "chunk_id": chunk_id,
                "document_id": meta.get("document_id", ""),
                "source_name": meta.get("source_name", ""),
                "file_deleted": file_deleted,
            },
            role="admin",
            ip=client_ip,
        )

        return {
            "message": f"Chunk '{chunk_id}' deleted",
            "chunk_id": chunk_id,
            "file_deleted": file_deleted,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("delete_chunk failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.patch(
    "/chunks/{chunk_id}",
    summary="Update chunk metadata (admin only)",
)
async def patch_chunk(
    chunk_id: str,
    body: ChunkPatchRequest,
    request: Request,
    _: None = Depends(_require_admin_key),
    settings: Settings = Depends(get_settings),
    audit_store: AuditStore = Depends(_get_audit_store),
) -> dict:
    """Update mutable metadata fields on a chunk in the vector store and JSON file."""
    _validate_id_for_path(chunk_id, "chunk_id")
    collection = _get_collection(settings)

    try:
        existing = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not (existing.get("ids") or []):
            raise HTTPException(
                status_code=404,
                detail={"message": f"Chunk '{chunk_id}' not found"},
            )

        old_meta: dict = dict((existing.get("metadatas") or [{}])[0])
        new_meta = dict(old_meta)
        changes: dict[str, Any] = {}

        if body.evidence_tier_override is not None:
            changes["evidence_tier_default"] = body.evidence_tier_override
            new_meta["evidence_tier_default"] = body.evidence_tier_override
        if body.notes is not None:
            changes["notes"] = body.notes
            new_meta["notes"] = body.notes

        if not changes:
            return {"message": "No changes requested", "chunk_id": chunk_id}

        collection.update(ids=[chunk_id], metadatas=[new_meta])

        # Update normalized JSON file if it exists — use path confinement
        norm_dir = Path(settings.processed_data_dir) / "normalized"
        norm_path = _safe_chunk_path(norm_dir, chunk_id)
        if norm_path is not None and norm_path.exists():
            try:
                data = json.loads(norm_path.read_text(encoding="utf-8"))
                data["metadata"].update(changes)
                if "evidence_tier_default" in changes:
                    data["evidence_tier_default"] = changes["evidence_tier_default"]
                norm_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception as fe:
                logger.warning("Could not update normalized file for %s: %s", chunk_id, fe)

        client_ip = request.client.host if request.client else ""
        audit_store.log_event(
            event_type="admin_action",
            data={
                "action": "patch_chunk",
                "chunk_id": chunk_id,
                "before": {k: old_meta.get(k) for k in changes},
                "after": changes,
            },
            role="admin",
            ip=client_ip,
        )

        return {
            "message": "Chunk updated",
            "chunk_id": chunk_id,
            "changes": changes,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("patch_chunk failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
