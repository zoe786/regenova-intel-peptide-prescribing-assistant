"""Tests for SEMANTIC GROUNDING of citations — not just bookkeeping.

`test_citation_integrity.py` already verifies the *structural* properties of
citations: that they attach, deduplicate by document, carry non-empty excerpts,
and that every [N] marker has a matching Citation object.

What that file does NOT test — and what this file exists to expose — is whether
the *claims made in the answer text* are actually supported by the content of
the cited chunks. This is the difference between:

    "a citation is well-formed"          (already tested)
    "a citation justifies the claim"     (NOT tested — tested here)

For a peptide *prescribing* assistant this is the load-bearing property. A model
can hallucinate a dose, attach a perfectly well-formed citation pointing at a
chunk that says nothing about that dose, and every existing test still passes.

These tests construct exactly that scenario and probe for it. The first test
documents the current behaviour (citations attach regardless of support). The
second defines the contract a real grounding check must satisfy and is marked
xfail until that check exists — so the suite stays green while the gap stays
visible and tracked.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from apps.api.schemas.source import NormalizedChunk, SourceMetadata
from apps.api.services.citation_service import CitationService


def _make_chunk(chunk_id: str, document_id: str, content: str, tier: int = 1) -> NormalizedChunk:
    meta = SourceMetadata(
        source_type="pubmed",
        source_name="Journal of Peptide Research",
        source_url="https://example.org/article",
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


# The retrieved evidence says NOTHING about dosage. It is purely a
# mechanism-of-action statement.
UNRELATED_CHUNK_CONTENT = (
    "BPC-157 is a synthetic pentadecapeptide derived from a protein found in "
    "gastric juice. Preclinical studies suggest it influences angiogenesis and "
    "may modulate the nitric oxide pathway. No human pharmacokinetic dosing "
    "data is established in these sources."
)

# The answer asserts a SPECIFIC, CLINICALLY ACTIONABLE dose that the evidence
# above does not contain. This is the canonical hallucination-with-citation case.
HALLUCINATED_DOSE_ANSWER = (
    "For tendon repair, administer BPC-157 at 500 micrograms subcutaneously "
    "twice daily for four weeks."
)


def _claim_is_supported_by_chunks(claim: str, chunks: list[NormalizedChunk]) -> bool:
    """A deliberately GENEROUS proxy for grounding.

    Real grounding needs an NLI/entailment model. But even this lenient lexical
    proxy — does the specific, load-bearing token of the claim appear anywhere
    in the cited evidence? — is enough to catch a fabricated dose. If "500"
    and "micrograms" appear nowhere in any chunk, the dose is ungrounded.

    The point of using a weak proxy is fairness: if the system fails even this,
    it would certainly fail a stricter semantic check.
    """
    haystack = " ".join(c.content.lower() for c in chunks)
    # Load-bearing tokens from the claim that a citation must support.
    load_bearing = ["500", "micrograms", "twice daily", "four weeks"]
    return all(token in haystack for token in load_bearing)


class TestCurrentBehaviourIsUngrounded:
    """Documents the flaw as it exists today. These tests PASS — that is the point.

    They prove the current system will attach citations to a fabricated claim.
    """

    def test_citation_attaches_to_unsupporting_chunk(self):
        service = CitationService()
        chunk = _make_chunk("c1", "doc_1", UNRELATED_CHUNK_CONTENT)

        annotated, citations = service.attach_citations([chunk], HALLUCINATED_DOSE_ANSWER)

        # A citation IS produced, and the answer IS annotated with a source —
        # despite the chunk containing no dosing information whatsoever.
        assert len(citations) == 1
        assert "Sources" in annotated

    def test_the_claim_is_in_fact_unsupported(self):
        """Independent confirmation that the evidence does not support the dose.

        This makes the flaw unambiguous: the chunk genuinely lacks the claim,
        yet the system above still cited it.
        """
        chunk = _make_chunk("c1", "doc_1", UNRELATED_CHUNK_CONTENT)
        assert _claim_is_supported_by_chunks(HALLUCINATED_DOSE_ANSWER, [chunk]) is False


class TestGroundingContract:
    """Verifies the GroundingService closes the loop the citation layer left open.

    Previously xfail (no grounding existed). Now a live guardrail.
    """

    def test_unsupported_claim_is_flagged(self):
        from apps.api.services.grounding_service import GroundingService

        service = GroundingService()
        chunk = _make_chunk("c1", "doc_1", UNRELATED_CHUNK_CONTENT)

        report = service.check(HALLUCINATED_DOSE_ANSWER, [chunk])

        # The fabricated dose has no support in the mechanism-only chunk, so
        # the answer must be reported ungrounded and yield a safety flag.
        assert report.grounded is False
        assert report.to_safety_flag() is not None

    def test_supported_claim_passes_grounding(self):
        from apps.api.services.grounding_service import GroundingService

        service = GroundingService()
        supporting = _make_chunk(
            "c2",
            "doc_2",
            "A protocol described BPC-157 at 500 micrograms subcutaneously "
            "twice daily for four weeks in a small case series.",
        )
        report = service.check(HALLUCINATED_DOSE_ANSWER, [supporting])
        assert report.grounded is True
        assert report.to_safety_flag() is None
