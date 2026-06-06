"""Tests for citation provenance: channel/video/query attribution.

Verifies that source-specific provenance carried in extra_metadata reaches
the Citation (both as a structured field and in the display name).
"""

from __future__ import annotations

from datetime import datetime, timezone

from apps.api.schemas.source import NormalizedChunk, SourceMetadata
from apps.api.services.citation_service import CitationService


def _chunk(source_type: str, source_name: str, extra: dict, doc_id: str = "doc1") -> NormalizedChunk:
    return NormalizedChunk(
        chunk_id=f"{doc_id}_0000",
        document_id=doc_id,
        content="BPC-157 was administered at 250 mcg twice daily.",
        chunk_index=0,
        metadata=SourceMetadata(
            source_type=source_type,
            source_name=source_name,
            source_url="https://example.com/x",
            acquired_at=datetime.now(timezone.utc),
            evidence_tier_default=3,
            content_hash="abc",
            document_id=doc_id,
            extra_metadata=extra,
        ),
    )


class TestYouTubeProvenance:
    def test_channel_and_title_in_display_name(self) -> None:
        chunk = _chunk(
            "youtube",
            "YouTube:abc123",
            {"channel_name": "Peptide Science", "video_title": "BPC-157 Explained"},
        )
        _, citations = CitationService().attach_citations([chunk], "answer")
        assert len(citations) == 1
        assert citations[0].source_name == "Peptide Science — BPC-157 Explained"

    def test_provenance_field_populated(self) -> None:
        chunk = _chunk(
            "youtube",
            "YouTube:abc123",
            {"channel_name": "Peptide Science", "video_title": "BPC-157 Explained"},
        )
        _, citations = CitationService().attach_citations([chunk], "answer")
        assert citations[0].provenance["channel_name"] == "Peptide Science"
        assert citations[0].provenance["video_title"] == "BPC-157 Explained"


class TestPubMedProvenance:
    def test_search_query_in_display_name(self) -> None:
        chunk = _chunk(
            "pubmed",
            "PubMed:12345",
            {"pubmed_query": '"BPC-157"[tiab]'},
        )
        _, citations = CitationService().attach_citations([chunk], "answer")
        assert "via search:" in citations[0].source_name
        assert "BPC-157" in citations[0].source_name


class TestFallback:
    def test_no_provenance_uses_source_name(self) -> None:
        chunk = _chunk("website", "Example Blog", {})
        _, citations = CitationService().attach_citations([chunk], "answer")
        assert citations[0].source_name == "Example Blog"
        assert citations[0].provenance == {}
