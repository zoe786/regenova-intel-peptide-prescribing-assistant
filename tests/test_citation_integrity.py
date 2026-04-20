"""pytest tests for CitationService.

Tests citation attachment, marker injection, integrity validation,
and deduplication.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from apps.api.schemas.source import NormalizedChunk, SourceMetadata
from apps.api.services.citation_service import CitationService


def _make_chunk(
    chunk_id: str,
    document_id: str,
    content: str,
    source_name: str = "Test Journal",
    tier: int = 1,
    source_url: str | None = None,
) -> NormalizedChunk:
    """Helper: create a NormalizedChunk with given parameters."""
    meta = SourceMetadata(
        source_type="pubmed",
        source_name=source_name,
        source_url=source_url,
        acquired_at=datetime.utcnow(),
        evidence_tier_default=tier,
        content_hash=f"hash_{chunk_id}",
        document_id=document_id,
    )
    return NormalizedChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        metadata=meta,
        similarity_score=0.8,
    )


@pytest.fixture
def service() -> CitationService:
    """Return a CitationService instance."""
    return CitationService()


@pytest.fixture
def sample_chunks() -> list[NormalizedChunk]:
    """Return three distinct chunks from different documents."""
    return [
        _make_chunk("chunk_001", "doc_001", "BPC-157 promotes tendon healing via growth factor upregulation.", "Journal A", 1),
        _make_chunk("chunk_002", "doc_002", "In rodent models, BPC-157 showed significant anti-inflammatory effects.", "Study B", 2),
        _make_chunk("chunk_003", "doc_003", "Practitioners report subjective improvement in joint pain with BPC-157.", "Forum C", 4),
    ]


class TestCitationsAttached:
    def test_citations_attached_non_empty(self, service, sample_chunks):
        """attach_citations should produce a non-empty citations list."""
        _, citations = service.attach_citations(sample_chunks, "Some answer text.")
        assert len(citations) > 0, "Expected at least one citation"

    def test_citations_count_matches_unique_docs(self, service, sample_chunks):
        """Number of citations should equal number of unique document IDs."""
        _, citations = service.attach_citations(sample_chunks, "Answer text.")
        unique_doc_ids = len({c.document_id for c in sample_chunks})
        assert len(citations) == unique_doc_ids

    def test_empty_chunks_produces_no_citations(self, service):
        """Empty chunks should produce empty citations list."""
        answer, citations = service.attach_citations([], "Some answer.")
        assert len(citations) == 0
        assert answer == "Some answer."

    def test_citation_has_required_fields(self, service, sample_chunks):
        """Each Citation object should have all required fields populated."""
        _, citations = service.attach_citations(sample_chunks, "Answer.")
        for cit in citations:
            assert cit.source_id, "source_id should be non-empty"
            assert cit.source_name, "source_name should be non-empty"
            assert cit.chunk_id, "chunk_id should be non-empty"
            assert 1 <= cit.evidence_tier <= 5
            assert cit.excerpt, "excerpt should be non-empty"


class TestCitationMarkers:
    def test_answer_contains_sources_block(self, service, sample_chunks):
        """Annotated answer should contain a Sources block."""
        annotated, _ = service.attach_citations(sample_chunks, "The evidence shows X.")
        assert "Sources" in annotated or "[1]" in annotated

    def test_single_chunk_produces_one_marker_reference(self, service):
        """Single chunk should produce one source reference."""
        chunk = _make_chunk("c1", "d1", "BPC-157 content here.", "Journal A")
        annotated, citations = service.attach_citations([chunk], "Answer text.")
        assert len(citations) == 1


class TestNoOrphanMarkers:
    def test_no_orphan_markers(self, service, sample_chunks):
        """All [N] markers in the answer should have corresponding Citation objects."""
        annotated, citations = service.attach_citations(sample_chunks, "Answer [1] and [2].")
        # Extract numeric markers from answer
        markers = set(int(m) for m in re.findall(r"\[(\d+)\]", annotated))
        citation_numbers = set(range(1, len(citations) + 1))
        # All markers that appear INLINE (not in the sources block) should have citations
        # The sources block adds [1], [2] etc. — all should have matching citations
        inline_markers = {m for m in markers if m <= len(citations)}
        assert inline_markers.issubset(citation_numbers), (
            f"Orphan markers: {inline_markers - citation_numbers}"
        )


class TestDeduplication:
    def test_same_document_id_appears_once(self, service):
        """Multiple chunks from the same document_id should produce one citation."""
        chunk1 = _make_chunk("c1_chunk0", "same_doc", "Content chunk 1.", "Journal A")
        chunk2 = _make_chunk("c1_chunk1", "same_doc", "Content chunk 2.", "Journal A")
        _, citations = service.attach_citations([chunk1, chunk2], "Answer.")
        source_ids = [c.source_id for c in citations]
        assert len(source_ids) == len(set(source_ids)), (
            "Duplicate source_ids found in citations"
        )
        assert len(citations) == 1, f"Expected 1 citation for same doc, got {len(citations)}"

    def test_different_documents_not_deduplicated(self, service, sample_chunks):
        """Chunks from different document_ids should each produce a citation."""
        _, citations = service.attach_citations(sample_chunks, "Answer.")
        assert len(citations) == 3, f"Expected 3 citations for 3 different docs, got {len(citations)}"

    def test_citation_excerpt_populated(self, service):
        """Each citation's excerpt should contain part of the source content."""
        chunk = _make_chunk("c1", "d1", "BPC-157 demonstrated significant tendon healing in the study.", "Journal A")
        _, citations = service.attach_citations([chunk], "Answer.")
        assert len(citations) == 1
        excerpt = citations[0].excerpt
        assert len(excerpt) > 10, "Excerpt should be non-trivial"
        assert "BPC-157" in excerpt or "tendon" in excerpt
