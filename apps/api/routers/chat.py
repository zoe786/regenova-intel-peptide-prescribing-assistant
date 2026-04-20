"""Chat endpoint router for REGENOVA-Intel.

Orchestrates the full RAG pipeline: retrieval → ranking → safety evaluation
→ citation attachment → answer composition.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.api.config import Settings, get_settings
from apps.api.schemas.chat import ChatRequest, ChatResponse
from apps.api.services.answer_composer import AnswerComposer
from apps.api.services.citation_service import CitationService
from apps.api.services.ranking_service import RankingService
from apps.api.services.retrieval_service import RetrievalService
from apps.api.services.safety_rules import SafetyRuleEngine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["Chat"])

# Module-level service singletons (initialised lazily on first request)
_retrieval_service: RetrievalService | None = None
_ranking_service = RankingService()
_safety_engine = SafetyRuleEngine()
_citation_service = CitationService()


def _get_retrieval_service(settings: Settings) -> RetrievalService:
    """Return a cached RetrievalService instance."""
    global _retrieval_service
    if _retrieval_service is None:
        _retrieval_service = RetrievalService(
            chroma_persist_dir=settings.chroma_persist_dir
        )
    return _retrieval_service


def _audit_log(
    request_id: str,
    role: str,
    query: str,
    flags: list,
    confidence: float,
    latency_ms: int,
) -> None:
    """Write a structured audit log entry.

    Query text is hashed (SHA-256) before logging — never log raw queries.
    """
    query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
    logger.info(
        "AUDIT event=chat_query request_id=%s role=%s query_hash=%s "
        "safety_flags=%s confidence=%.3f latency_ms=%d",
        request_id,
        role,
        query_hash,
        [f.code for f in flags],
        confidence,
        latency_ms,
    )


def _check_reconstitution_access(
    include_reconstitution: bool,
    role: str,
    settings: Settings,
) -> None:
    """Gate reconstitution guidance behind role and feature flag checks.

    Raises:
        HTTPException 403: If reconstitution is requested but not permitted.
    """
    if not include_reconstitution:
        return
    if not settings.enable_reconstitution_guidance:
        raise HTTPException(
            status_code=403,
            detail={
                "type": "access_denied",
                "title": "Reconstitution guidance is disabled",
                "detail": "ENABLE_RECONSTITUTION_GUIDANCE feature flag is off.",
            },
        )
    if role not in ("clinician", "admin"):
        raise HTTPException(
            status_code=403,
            detail={
                "type": "access_denied",
                "title": "Reconstitution guidance requires clinician or admin role",
                "detail": f"Current role: {role}",
            },
        )


@router.post(
    "",
    response_model=ChatResponse,
    summary="Submit a clinical query to the RAG pipeline",
    description=(
        "Retrieves relevant evidence chunks, applies safety rules, "
        "attaches citations, and returns an LLM-synthesised answer. "
        "All responses carry X-Decision-Support-Only: true."
    ),
)
async def chat(
    request_body: ChatRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    """Process a clinical query through the full RAG pipeline.

    Steps:
    1. Validate request and access permissions
    2. Retrieve relevant chunks (vector search)
    3. Rank chunks by evidence tier + relevance + recency
    4. Evaluate safety rules
    5. Attach citations
    6. Compose LLM answer
    7. Log audit event
    8. Return ChatResponse

    Args:
        request_body: Validated ChatRequest.
        request: FastAPI Request object (for IP logging etc.).
        settings: Application settings.

    Returns:
        ChatResponse with answer, citations, safety flags, and confidence.
    """
    start_ms = int(time.time() * 1000)
    request_id = str(uuid.uuid4())

    logger.info(
        "Chat request: request_id=%s role=%s top_k=%d",
        request_id,
        request_body.role,
        request_body.context_window_size,
    )

    # Gate reconstitution guidance
    _check_reconstitution_access(
        request_body.include_reconstitution,
        request_body.role,
        settings,
    )

    # 1. Retrieve chunks
    retrieval_svc = _get_retrieval_service(settings)
    chunks = retrieval_svc.retrieve(
        query=request_body.query,
        top_k=request_body.context_window_size,
    )

    # 2. Rank chunks
    ranked = _ranking_service.rank(chunks=chunks, query=request_body.query)

    # 3. Safety evaluation
    flags = _safety_engine.evaluate(
        query=request_body.query,
        patient_case=None,  # TODO: accept PatientCase in request body
        chunks=chunks,
    )

    # 4. Attach citations
    ranked_chunks = [chunk for chunk, _ in ranked]
    annotated_answer_placeholder = " ".join(
        f"[{i+1}]" for i in range(len(ranked_chunks))
    )
    annotated_answer, citations = _citation_service.attach_citations(
        chunks=ranked_chunks,
        answer_text=annotated_answer_placeholder,
    )

    # 5. Compose answer
    composer = AnswerComposer(
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        openai_api_key=settings.openai_api_key,
    )
    response = composer.compose(
        query=request_body.query,
        ranked_chunks=ranked,
        citations=citations,
        safety_flags=flags,
        patient_case=None,
        request_id=request_id,
    )

    response.latency_ms = int(time.time() * 1000) - start_ms

    # 6. Audit log
    _audit_log(
        request_id=request_id,
        role=request_body.role,
        query=request_body.query,
        flags=flags,
        confidence=response.confidence,
        latency_ms=response.latency_ms,
    )

    return response
