"""Pydantic schemas for the chat API endpoint.

Defines request/response models for the /chat route, including
citations, safety flags, and the full ChatResponse structure.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Incoming chat query from a client."""

    query: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="The clinical or research query",
    )
    role: Literal["researcher", "clinician", "admin"] = Field(
        default="clinician",
        description="Role of the requesting user; controls access to features",
    )
    context_window_size: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of top-K chunks to retrieve and include as context",
    )
    include_reconstitution: bool = Field(
        default=False,
        description="Whether to include reconstitution guidance (clinician role + feature flag required)",
    )
    session_id: str | None = Field(
        default=None,
        description="Optional session identifier for audit tracking",
    )


class Citation(BaseModel):
    """A single source citation attached to a response."""

    source_id: str = Field(..., description="Unique identifier for the source document")
    source_name: str = Field(..., description="Human-readable name of the source")
    url: str | None = Field(default=None, description="URL of the source if available")
    chunk_id: str = Field(..., description="ID of the specific chunk cited")
    evidence_tier: int = Field(
        ..., ge=1, le=5, description="Evidence tier (1=highest, 5=lowest)"
    )
    excerpt: str = Field(
        ..., max_length=500, description="Short excerpt from the cited chunk"
    )


class SafetyFlag(BaseModel):
    """A safety concern raised by the SafetyRuleEngine."""

    severity: Literal["info", "warning", "critical"] = Field(
        ..., description="Severity level of the safety flag"
    )
    code: str = Field(
        ..., description="Rule code (e.g. SR-001) for traceability"
    )
    message: str = Field(..., description="Human-readable safety message")
    rationale: str = Field(
        ..., description="Clinical rationale for this flag"
    )


class ChatResponse(BaseModel):
    """Full response from the /chat endpoint."""

    answer: str = Field(..., description="The synthesised clinical answer")
    recommendations: list[str] = Field(
        default_factory=list,
        description="Actionable recommendations for the clinician",
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description="Source citations supporting the answer",
    )
    safety_flags: list[SafetyFlag] = Field(
        default_factory=list,
        description="Safety flags raised during evaluation",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Overall confidence score (0.0–1.0) based on evidence quality",
    )
    evidence_summary: str = Field(
        ..., description="Human-readable summary of evidence sources used"
    )
    disclaimer: str = Field(
        default=(
            "This is clinical decision support only. Review all information with a "
            "qualified healthcare professional before clinical application."
        ),
        description="Mandatory disclaimer appended to all responses",
    )
    request_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique request identifier for audit tracking",
    )
    latency_ms: int = Field(
        default=0, description="Total request processing time in milliseconds"
    )
