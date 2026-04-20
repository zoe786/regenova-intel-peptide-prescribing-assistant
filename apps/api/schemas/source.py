"""Pydantic schemas for source documents and normalized chunks."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SourceMetadata(BaseModel):
    """Metadata for a source document or chunk."""

    source_type: str = Field(
        ...,
        description="Type of source: pubmed, document, website, youtube, skool_course, skool_community, forum",
    )
    source_name: str = Field(..., description="Human-readable name of the source")
    source_url: Optional[str] = Field(default=None, description="URL of the source")
    acquired_at: datetime = Field(..., description="Timestamp when content was acquired")
    published_at: Optional[datetime] = Field(
        default=None, description="Original publication date of the content"
    )
    evidence_tier_default: int = Field(
        ..., ge=1, le=5, description="Default evidence tier for this source type"
    )
    jurisdiction: Optional[str] = Field(
        default=None,
        description="Geographic jurisdiction (e.g. US, EU, global)",
    )
    content_hash: str = Field(
        ..., description="SHA-256 hash of the normalized content for deduplication"
    )
    document_id: str = Field(
        ..., description="Unique identifier for the parent document"
    )


class NormalizedChunk(BaseModel):
    """A normalized, chunked piece of content ready for embedding."""

    chunk_id: str = Field(
        ..., description="Unique identifier for this chunk (document_id + chunk_index)"
    )
    document_id: str = Field(
        ..., description="Parent document identifier"
    )
    content: str = Field(
        ..., min_length=1, description="Cleaned text content of this chunk"
    )
    metadata: SourceMetadata = Field(
        ..., description="Source metadata attached to this chunk"
    )
    embedding: Optional[list[float]] = Field(
        default=None,
        description="Embedding vector (populated after embedding step)",
    )
    chunk_index: int = Field(
        default=0, description="Zero-based index of this chunk within the document"
    )
    similarity_score: Optional[float] = Field(
        default=None,
        description="Cosine similarity score from vector search (populated at query time)",
    )
