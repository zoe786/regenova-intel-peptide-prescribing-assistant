"""Metadata enrichment functions for the ingestion pipeline.

Computes content hashes, generates document IDs, infers evidence tiers,
and produces complete SourceMetadata from RawDocument instances.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Mapping from source_type to default evidence tier
_SOURCE_TYPE_TIER_MAP: dict[str, int] = {
    "pubmed": 1,
    "document": 2,
    "pdf": 2,
    "txt": 2,
    "md": 2,
    "website": 3,
    "youtube": 3,
    "skool_course": 3,
    "skool_community": 4,
    "forum": 4,
    "anecdotal": 5,
    "testimonial": 5,
}

# Source names that always map to specific tiers (regardless of source_type)
_SOURCE_NAME_TIER_OVERRIDES: dict[str, int] = {
    "pubmed": 1,
    "cochrane": 1,
    "ncbi": 1,
}


def compute_content_hash(text: str) -> str:
    """Compute a SHA-256 hash of the text content for deduplication.

    Args:
        text: The normalized text content to hash.

    Returns:
        Lowercase hex string of SHA-256 hash (64 characters).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_document_id(
    source_url: Optional[str],
    acquired_at: datetime,
    source_name: str = "",
) -> str:
    """Generate a deterministic document ID from source URL and acquisition time.

    Uses URL-based UUID5 when a URL is available, otherwise uses UUID4.

    Args:
        source_url: URL of the source document (may be None).
        acquired_at: Timestamp when the document was acquired.
        source_name: Name of the source (used as fallback namespace).

    Returns:
        UUID string suitable for use as a document identifier.
    """
    if source_url:
        # Deterministic ID based on URL (same URL always gives same ID)
        namespace = uuid.NAMESPACE_URL
        return str(uuid.uuid5(namespace, source_url))

    # Fallback: combine source_name and acquisition date for semi-determinism
    seed = f"{source_name}::{acquired_at.isoformat()}"
    namespace = uuid.NAMESPACE_DNS
    return str(uuid.uuid5(namespace, seed))


def infer_evidence_tier(source_type: str, source_name: str = "") -> int:
    """Infer the default evidence tier from source type and name.

    Args:
        source_type: Type of source (e.g. 'pubmed', 'website', 'forum').
        source_name: Name of the source (used for override checks).

    Returns:
        Integer evidence tier from 1 (highest) to 5 (lowest).
    """
    # Check source name overrides first
    source_name_lower = source_name.lower()
    for name_key, tier in _SOURCE_NAME_TIER_OVERRIDES.items():
        if name_key in source_name_lower:
            logger.debug("Tier override by source name: %s → tier %d", source_name, tier)
            return tier

    # Look up source type
    tier = _SOURCE_TYPE_TIER_MAP.get(source_type.lower(), 3)
    logger.debug("Inferred tier %d for source_type=%s", tier, source_type)
    return tier


def enrich_metadata(raw_doc: object) -> dict:
    """Produce a complete metadata dictionary from a RawDocument.

    Computes content hash and document ID, infers evidence tier,
    and returns a dict compatible with SourceMetadata schema.

    Args:
        raw_doc: A RawDocument instance with source information.

    Returns:
        Dictionary of metadata fields matching SourceMetadata schema.
    """
    content_hash = compute_content_hash(raw_doc.raw_content)  # type: ignore[attr-defined]
    document_id = generate_document_id(
        source_url=raw_doc.source_url,  # type: ignore[attr-defined]
        acquired_at=raw_doc.acquired_at,  # type: ignore[attr-defined]
        source_name=raw_doc.source_name,  # type: ignore[attr-defined]
    )
    tier = infer_evidence_tier(
        source_type=raw_doc.source_type,  # type: ignore[attr-defined]
        source_name=raw_doc.source_name,  # type: ignore[attr-defined]
    )

    return {
        "source_type": raw_doc.source_type,  # type: ignore[attr-defined]
        "source_name": raw_doc.source_name,  # type: ignore[attr-defined]
        "source_url": raw_doc.source_url,  # type: ignore[attr-defined]
        "acquired_at": raw_doc.acquired_at.isoformat(),  # type: ignore[attr-defined]
        "published_at": raw_doc.published_at.isoformat() if raw_doc.published_at else None,  # type: ignore[attr-defined]
        "evidence_tier_default": tier,
        "jurisdiction": getattr(raw_doc, "jurisdiction", None),
        "content_hash": content_hash,
        "document_id": document_id,
    }
