"""Semantic grounding service for REGENOVA-Intel.

This service closes the gap that `test_citation_integrity.py` does not cover:
it verifies that the *claims made in a composed answer* are actually supported
by the *content of the retrieved chunks*, rather than merely confirming that
citation objects are well-formed.

Design
------
The answer is split into sentence-level claims. Each claim and each chunk is
embedded with a lightweight, deterministic, dependency-free lexical embedder
(the same `LocalDeterministicEmbeddingFunction` used elsewhere for offline work),
so grounding runs without any network call and is fully reproducible in tests.
Each claim is scored against its best-matching chunk by cosine similarity; a
claim scoring below `threshold` is reported as UNGROUNDED.

This is intentionally a *lexical-semantic* proxy, not a full NLI/entailment
model. It is a meaningful lower bound: a numeric/dosage claim with no lexical
overlap in any chunk will score near zero and be caught. A production-grade
gate should swap `_embed`/`_similarity` for a real entailment model behind the
same interface — `GroundingService.check()` and `GroundingReport` are the stable
contract.

The service NEVER silently passes an ungrounded answer. When ungrounded claims
exist it produces a `SafetyFlag` (severity "warning") that the caller surfaces
in the ChatResponse, so a clinician is never shown a confident, cited claim that
the evidence does not support.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from apps.api.schemas.chat import SafetyFlag
from apps.api.schemas.source import NormalizedChunk
from pipelines.common.chroma import LocalDeterministicEmbeddingFunction

logger = logging.getLogger(__name__)

# Default cosine-similarity threshold below which a claim is "ungrounded".
# Tuned for the deterministic lexical embedder: claims that share substantive
# tokens with a chunk clear it; fabricated specifics (novel doses, invented
# study results) do not.
_DEFAULT_THRESHOLD = 0.25

# Claims shorter than this (in tokens) are treated as connective/boilerplate
# and skipped — they carry no verifiable clinical assertion.
_MIN_CLAIM_TOKENS = 4

# Safety flag code for an ungrounded answer (traceable, matches SR-xxx scheme).
GROUNDING_FLAG_CODE = "GR-001"


@dataclass
class ClaimVerdict:
    """Grounding verdict for a single extracted claim."""

    claim: str
    grounded: bool
    best_score: float
    best_chunk_id: str | None


@dataclass
class GroundingReport:
    """Result of grounding an answer against its supporting chunks."""

    grounded: bool
    verdicts: list[ClaimVerdict] = field(default_factory=list)
    threshold: float = _DEFAULT_THRESHOLD

    @property
    def ungrounded_claims(self) -> list[str]:
        return [v.claim for v in self.verdicts if not v.grounded]

    def to_safety_flag(self) -> SafetyFlag | None:
        """Return a SafetyFlag if any claim is ungrounded, else None."""
        if self.grounded:
            return None
        n = len(self.ungrounded_claims)
        preview = "; ".join(self.ungrounded_claims[:3])
        return SafetyFlag(
            severity="warning",
            code=GROUNDING_FLAG_CODE,
            message=(
                f"{n} statement(s) in this answer are not supported by the "
                f"retrieved evidence and may be unreliable."
            ),
            rationale=(
                "Semantic grounding check: each answer claim was compared against "
                "the cited evidence chunks. The following fell below the support "
                f"threshold and should be verified manually before any clinical "
                f"use: {preview}"
            ),
        )


def _split_claims(answer_text: str) -> list[str]:
    """Split answer text into candidate sentence-level claims.

    Strips an appended '**Sources:**' block (added by CitationService) and any
    inline [N] markers so grounding scores the prose, not the bookkeeping.
    """
    # Drop a trailing Sources block if present.
    text = re.split(r"\n\s*\*\*Sources:\*\*", answer_text, maxsplit=1)[0]
    # Remove inline citation markers like [1], [2].
    text = re.sub(r"\[\d+\]", " ", text)
    # Split on sentence boundaries and newlines/bullets.
    raw = re.split(r"(?<=[.!?])\s+|\n+", text)
    claims: list[str] = []
    for part in raw:
        cleaned = part.strip().lstrip("-*•→ ").strip()
        if len(re.findall(r"[A-Za-z0-9-]+", cleaned)) >= _MIN_CLAIM_TOKENS:
            claims.append(cleaned)
    return claims


class GroundingService:
    """Verifies that answer claims are supported by retrieved evidence."""

    def __init__(self, threshold: float = _DEFAULT_THRESHOLD, dimensions: int = 256) -> None:
        self.threshold = threshold
        self._embedder = LocalDeterministicEmbeddingFunction(dimensions=dimensions)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self._embedder(texts)

    @staticmethod
    def _similarity(a: list[float], b: list[float]) -> float:
        """Cosine similarity of two already-normalised vectors (dot product)."""
        dot = sum(x * y for x, y in zip(a, b))
        # Vectors from the embedder are unit-normalised; clamp for float safety.
        return max(-1.0, min(1.0, dot))

    def check(
        self,
        answer_text: str,
        chunks: list[NormalizedChunk],
    ) -> GroundingReport:
        """Score each claim in the answer against the best-matching chunk.

        Args:
            answer_text: The composed answer (may include a Sources block).
            chunks: The chunks that were retrieved to support the answer.

        Returns:
            GroundingReport with a per-claim verdict and an overall flag.
        """
        claims = _split_claims(answer_text)

        # No verifiable claims, or no evidence at all: nothing to vouch for.
        if not claims:
            return GroundingReport(grounded=True, verdicts=[], threshold=self.threshold)
        if not chunks:
            verdicts = [
                ClaimVerdict(claim=c, grounded=False, best_score=0.0, best_chunk_id=None)
                for c in claims
            ]
            logger.info("Grounding: no chunks provided — %d claims ungrounded", len(claims))
            return GroundingReport(grounded=False, verdicts=verdicts, threshold=self.threshold)

        claim_vecs = self._embed(claims)
        chunk_vecs = self._embed([c.content for c in chunks])

        verdicts: list[ClaimVerdict] = []
        for claim, cvec in zip(claims, claim_vecs):
            best_score = -1.0
            best_id: str | None = None
            for chunk, kvec in zip(chunks, chunk_vecs):
                score = self._similarity(cvec, kvec)
                if score > best_score:
                    best_score = score
                    best_id = chunk.chunk_id
            verdicts.append(
                ClaimVerdict(
                    claim=claim,
                    grounded=best_score >= self.threshold,
                    best_score=round(best_score, 4),
                    best_chunk_id=best_id,
                )
            )

        all_grounded = all(v.grounded for v in verdicts)
        if not all_grounded:
            logger.warning(
                "Grounding: %d/%d claims below threshold %.2f",
                sum(1 for v in verdicts if not v.grounded),
                len(verdicts),
                self.threshold,
            )
        return GroundingReport(grounded=all_grounded, verdicts=verdicts, threshold=self.threshold)
