"""Confidence scoring for REGENOVA-Intel query responses.

Computes a confidence score from retrieved chunks and evidence quality,
and generates human-readable confidence labels and evidence summaries.
"""

from __future__ import annotations

from knowledge.scoring.evidence_tiering import aggregate_tier_score, tier_label


def compute_confidence(
    chunks: list,
    query: str,  # noqa: ARG001 — reserved for semantic similarity scoring
    tier_scores: list[int],
) -> float:
    """Compute a confidence score for a response based on evidence quality.

    Formula:
        confidence = aggregate_tier_score × retrieval_coverage_factor

    Where retrieval_coverage_factor penalises sparse retrieval:
        - ≥5 chunks: 1.00
        - 3-4 chunks: 0.85
        - 1-2 chunks: 0.70
        - 0 chunks: 0.00

    Args:
        chunks: Retrieved NormalizedChunk objects.
        query: Original query (reserved for future semantic scoring).
        tier_scores: Evidence tier integers for each retrieved chunk.

    Returns:
        Float confidence score in range [0.0, 1.0].
    """
    if not chunks or not tier_scores:
        return 0.0

    coverage_factor = (
        1.00 if len(chunks) >= 5
        else 0.85 if len(chunks) >= 3
        else 0.70
    )

    tier_aggregate = aggregate_tier_score(tier_scores)
    return round(min(1.0, tier_aggregate * coverage_factor), 4)


def confidence_label(score: float) -> str:
    """Return a human-readable confidence label.

    Args:
        score: Float confidence score [0.0, 1.0].

    Returns:
        One of: "high", "medium", "low", "insufficient".
    """
    if score >= 0.70:
        return "high"
    elif score >= 0.45:
        return "medium"
    elif score >= 0.20:
        return "low"
    return "insufficient"


def evidence_summary(chunks: list, confidence: float) -> str:
    """Generate a human-readable summary of the evidence sources used.

    Args:
        chunks: Retrieved NormalizedChunk objects.
        confidence: Computed confidence score.

    Returns:
        Formatted summary string (e.g. "3 tier-1 sources, 2 tier-3 sources | Confidence: high").
    """
    if not chunks:
        return "No evidence sources retrieved"

    tier_counts: dict[int, int] = {}
    for chunk in chunks:
        tier = getattr(getattr(chunk, "metadata", None), "evidence_tier_default", None)
        if tier is None:
            tier = chunk.get("evidence_tier_default", 3) if isinstance(chunk, dict) else 3
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    parts = [
        f"{count} {tier_label(tier).lower()} source{'s' if count != 1 else ''}"
        for tier, count in sorted(tier_counts.items())
    ]
    conf_label = confidence_label(confidence)
    return " | ".join(parts) + f" | Confidence: {conf_label} ({confidence:.0%})"
