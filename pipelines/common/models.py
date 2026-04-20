"""Common data models for the ingestion pipeline.

These dataclasses represent the stages of document processing:
RawDocument → NormalizedRecord → IngestionResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RawDocument:
    """Represents a raw, unprocessed source document before any cleaning.

    Attributes:
        source_type: Type of source (pubmed, document, website, etc.)
        source_name: Human-readable name of the source.
        source_url: URL of the source if applicable.
        acquired_at: Timestamp when this document was fetched/read.
        published_at: Original publication date if available.
        evidence_tier_default: Default evidence tier for this source type.
        jurisdiction: Geographic jurisdiction if relevant.
        raw_content: The unprocessed text or HTML content.
        file_path: Local file path if content came from a file.
        extra_metadata: Any additional source-specific metadata.
    """

    source_type: str
    source_name: str
    raw_content: str
    acquired_at: datetime = field(default_factory=datetime.utcnow)
    source_url: Optional[str] = None
    published_at: Optional[datetime] = None
    evidence_tier_default: int = 3
    jurisdiction: Optional[str] = None
    file_path: Optional[str] = None
    extra_metadata: dict = field(default_factory=dict)


@dataclass
class NormalizedRecord:
    """A cleaned, chunked, and metadata-enriched document chunk.

    Attributes:
        chunk_id: Unique identifier for this chunk.
        document_id: Parent document identifier.
        source_type: Type of source.
        source_name: Name of the source.
        source_url: Source URL.
        acquired_at: Acquisition timestamp.
        published_at: Publication date.
        evidence_tier_default: Evidence tier.
        jurisdiction: Geographic jurisdiction.
        content_hash: SHA-256 of the normalized content.
        content: Cleaned and normalized text content.
        chunk_index: Zero-based chunk index within the document.
    """

    chunk_id: str
    document_id: str
    source_type: str
    source_name: str
    content: str
    content_hash: str
    acquired_at: datetime
    evidence_tier_default: int = 3
    source_url: Optional[str] = None
    published_at: Optional[datetime] = None
    jurisdiction: Optional[str] = None
    chunk_index: int = 0


@dataclass
class IngestionResult:
    """Summary result from an ingestion run.

    Attributes:
        source_type: The source type that was ingested.
        count: Number of chunks/documents processed.
        errors: List of error messages encountered.
        duration_seconds: Time taken for the ingestion.
        skipped: Number of documents skipped (e.g. duplicates).
    """

    source_type: str
    count: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    skipped: int = 0

    @property
    def success(self) -> bool:
        """Return True if the ingestion completed without errors."""
        return len(self.errors) == 0

    def __str__(self) -> str:
        status = "✓" if self.success else "✗"
        return (
            f"{status} {self.source_type}: {self.count} chunks, "
            f"{len(self.errors)} errors, {self.duration_seconds:.1f}s"
        )
