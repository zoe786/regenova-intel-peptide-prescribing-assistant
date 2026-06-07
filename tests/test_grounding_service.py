"""Unit tests for GroundingService — semantic grounding of answer claims.

These exercise the real GroundingService interface directly: a fabricated claim
with no support in the evidence must be reported ungrounded and must yield a
SafetyFlag; a claim that restates the evidence must pass.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from apps.api.services.grounding_service import (
    GROUNDING_FLAG_CODE,
    GroundingService,
)
from apps.api.schemas.source import NormalizedChunk, SourceMetadata


def _chunk(chunk_id: str, content: str) -> NormalizedChunk:
    meta = SourceMetadata(
        source_type="pubmed",
        source_name="Journal of Peptide Research",
        acquired_at=datetime.utcnow(),
        evidence_tier_default=1,
        content_hash=f"hash_{chunk_id}",
        document_id=f"doc_{chunk_id}",
    )
    return NormalizedChunk(
        chunk_id=chunk_id,
        document_id=f"doc_{chunk_id}",
        content=content,
        metadata=meta,
        similarity_score=0.8,
    )


MECHANISM_ONLY = (
    "BPC-157 is a synthetic pentadecapeptide derived from a gastric protein. "
    "Preclinical work suggests effects on angiogenesis and the nitric oxide "
    "pathway. No human pharmacokinetic dosing data is established here."
)

FABRICATED_DOSE = (
    "Administer BPC-157 at 500 micrograms subcutaneously twice daily for four weeks."
)

SUPPORTED_DOSE_CHUNK = (
    "A small case series administered BPC-157 at 500 micrograms subcutaneously "
    "twice daily for four weeks and reported subjective improvement."
)


@pytest.fixture
def service() -> GroundingService:
    return GroundingService()


class TestUngrounded:
    def test_fabricated_dose_is_flagged(self, service):
        chunk = _chunk("c1", MECHANISM_ONLY)
        report = service.check(FABRICATED_DOSE, [chunk])
        assert report.grounded is False
        assert FABRICATED_DOSE.rstrip(".") in report.ungrounded_claims[0] or report.ungrounded_claims

    def test_ungrounded_produces_safety_flag(self, service):
        chunk = _chunk("c1", MECHANISM_ONLY)
        report = service.check(FABRICATED_DOSE, [chunk])
        flag = report.to_safety_flag()
        assert flag is not None
        assert flag.code == GROUNDING_FLAG_CODE
        assert flag.severity == "warning"

    def test_no_chunks_means_ungrounded(self, service):
        report = service.check(FABRICATED_DOSE, [])
        assert report.grounded is False
        assert report.to_safety_flag() is not None


class TestGrounded:
    def test_supported_claim_passes(self, service):
        chunk = _chunk("c1", SUPPORTED_DOSE_CHUNK)
        report = service.check(FABRICATED_DOSE, [chunk])
        assert report.grounded is True
        assert report.to_safety_flag() is None

    def test_mechanism_claim_grounded_by_mechanism_chunk(self, service):
        chunk = _chunk("c1", MECHANISM_ONLY)
        answer = "BPC-157 is a synthetic pentadecapeptide that may affect angiogenesis."
        report = service.check(answer, [chunk])
        assert report.grounded is True


class TestEdgeCases:
    def test_empty_answer_is_vacuously_grounded(self, service):
        chunk = _chunk("c1", MECHANISM_ONLY)
        report = service.check("", [chunk])
        assert report.grounded is True
        assert report.to_safety_flag() is None

    def test_sources_block_is_stripped_before_scoring(self, service):
        """The appended **Sources:** block must not be scored as a claim."""
        chunk = _chunk("c1", SUPPORTED_DOSE_CHUNK)
        answer = FABRICATED_DOSE + "\n\n**Sources:**\n[1] Journal of Peptide Research"
        report = service.check(answer, [chunk])
        # Only the real claim should be evaluated; the sources line is dropped.
        assert all("Journal of Peptide Research" not in v.claim for v in report.verdicts)

    def test_does_not_reject_everything(self, service):
        """Guard against a degenerate check that fails all input."""
        chunk = _chunk("c1", SUPPORTED_DOSE_CHUNK)
        report = service.check("BPC-157 case series reported improvement.", [chunk])
        assert report.grounded is True
