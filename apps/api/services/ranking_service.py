"""Evidence-tier weighted ranking service for retrieved chunks.

Applies a composite scoring formula that weights chunks by evidence tier,
relevance score, and content recency to produce a ranked list.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apps.api.schemas.source import NormalizedChunk

logger = logging.getLogger(__name__)

# ── Scoring Constants ──────────────────────────────────────────────────────────

# Evidence tier weights: higher tier = more authoritative
TIER_WEIGHTS: dict[int, float] = {
    1: 1.00,  # Peer-reviewed research
    2: 0.85,  # Clinical protocol documents
    3: 0.65,  # Educational / practitioner content
    4: 0.40,  # Community / practitioner discussion
    5: 0.15,  # Anecdotal / unverified
}

# Recency boost factors based on document age
RECENCY_BOOST_WITHIN_2_YEARS: float = 1.00
RECENCY_BOOST_WITHIN_5_YEARS: float = 0.90
RECENCY_BOOST_OLDER: float = 0.75
RECENCY_BOOST_UNKNOWN: float = 1.00  # No penalty when date is unavailable

_NOW = datetime.now(tz=timezone.utc)


class RankingService:
    """Ranks retrieved chunks using an evidence-weighted scoring formula.

    Scoring formula:
        score = tier_weight(tier) × relevance_score × recency_boost(published_at)

    Where:
        - tier_weight: Mapped from TIER_WEIGHTS dict (defaults to 0.40 for unknown tiers)
        - relevance_score: Cosine similarity from vector search (0.0–1.0)
        - recency_boost: Multiplier based on document age
    """

    def rank(
        self,
        chunks: list[NormalizedChunk],
        query: str,  # noqa: ARG002 — reserved for future re-ranking with cross-encoder
    ) -> list[tuple[NormalizedChunk, float]]:
        """Rank chunks by composite evidence-weighted score.

        Args:
            chunks: Chunks returned by RetrievalService with similarity_score set.
            query: Original query string (reserved for cross-encoder re-ranking TODO).

        Returns:
            List of (chunk, composite_score) tuples sorted by score descending.
        """
        scored: list[tuple[NormalizedChunk, float]] = []

        for chunk in chunks:
            score = self._compute_score(chunk)
            scored.append((chunk, score))
            logger.debug(
                "chunk=%s tier=%d sim=%.3f recency=%.2f → score=%.4f",
                chunk.chunk_id[:12],
                chunk.metadata.evidence_tier_default,
                chunk.similarity_score or 0.0,
                self._recency_boost(chunk.metadata.published_at),
                score,
            )

        scored.sort(key=lambda x: x[1], reverse=True)
        logger.info("Ranked %d chunks; top score=%.4f", len(scored), scored[0][1] if scored else 0.0)
        return scored

    def _compute_score(self, chunk: NormalizedChunk) -> float:
        """Compute the composite ranking score for a single chunk.

        Args:
            chunk: The chunk to score.

        Returns:
            Composite score in range [0.0, 1.0].
        """
        tier = chunk.metadata.evidence_tier_default
        tier_weight = TIER_WEIGHTS.get(tier, 0.40)
        relevance = chunk.similarity_score if chunk.similarity_score is not None else 0.5
        recency = self._recency_boost(chunk.metadata.published_at)
        return tier_weight * relevance * recency

    @staticmethod
    def _recency_boost(published_at: object | None) -> float:
        """Return recency boost multiplier based on document publication date.

        Args:
            published_at: Publication date (datetime or None).

        Returns:
            Boost multiplier between 0.75 and 1.00.
        """
        if published_at is None:
            return RECENCY_BOOST_UNKNOWN

        try:
            if hasattr(published_at, "year"):
                pub_date = published_at  # type: ignore[assignment]
            else:
                from datetime import datetime as dt
                pub_date = dt.fromisoformat(str(published_at))

            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)

            age_years = (_NOW - pub_date).days / 365.25

            if age_years <= 2:
                return RECENCY_BOOST_WITHIN_2_YEARS
            elif age_years <= 5:
                return RECENCY_BOOST_WITHIN_5_YEARS
            else:
                return RECENCY_BOOST_OLDER
        except Exception:
            return RECENCY_BOOST_UNKNOWN
