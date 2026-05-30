"""Typed models for the collectors framework."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SourceType = Literal["pubmed", "website", "youtube", "skool_community", "forum"]


class SourceDefinition(BaseModel):
    """Approved source definition loaded from source_registry.yaml."""

    id: str
    type: SourceType
    enabled: bool = True
    name: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class SourceRegistry(BaseModel):
    """Top-level source registry model."""

    sources: list[SourceDefinition] = Field(default_factory=list)


class CollectionArtifact(BaseModel):
    """Artifact produced by a collector and written to data/raw."""

    path: str
    content_hash: str
    changed: bool = True
    record_count: int = 0


class CollectionResult(BaseModel):
    """Collector execution result for a single source."""

    source_id: str
    source_type: SourceType
    success: bool = True
    artifacts: list[CollectionArtifact] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    records_collected: int = 0
    triggered_ingestion: bool = False


class RunSummary(BaseModel):
    """Runner summary for all executed sources."""

    total_sources: int = 0
    successful_sources: int = 0
    failed_sources: int = 0
    changed_sources: int = 0
    results: list[CollectionResult] = Field(default_factory=list)
