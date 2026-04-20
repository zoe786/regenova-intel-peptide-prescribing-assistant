"""Evidence tiering utilities for REGENOVA-Intel.

Provides tier weight lookups, labels, source-type-to-tier mapping,
and aggregate tier score computation.
"""

from __future__ import annotations

# Tier weights: tier 1 = highest quality, tier 5 = lowest quality
TIER_WEIGHTS: dict[int, float] = {
    1: 1.00,  # Peer-reviewed research
    2: 0.85,  # Clinical protocol documents
    3: 0.65,  # Educational / practitioner content
    4: 0.40,  # Community / practitioner discussion
    5: 0.15,  # Anecdotal / unverified
}

# Human-readable tier labels
TIER_LABELS: dict[int, str] = {
    1: "Peer-Reviewed Research",
    2: "Clinical Protocol",
    3: "Educational Content",
    4: "Community Discussion",
    5: "Anecdotal",
}

# Source type to default tier mapping
SOURCE_TYPE_TIER_MAP: dict[str, int] = {
    "pubmed": 1,
    "document": 2,
    "pdf": 2,
    "txt": 2,
    "md": 2,
    "website": 3,
    "youtube": 3,
    "skool_course": 3,
    "skool_community": 4,
    "forum": 4,
    "anecdotal": 5,
    "testimonial": 5,
    "unknown": 3,
}

_DEFAULT_WEIGHT = 0.40  # Fallback weight for unknown tiers


def get_tier_weight(tier: int) -> float:
    """Return the evidence weight for a given tier (1–5).

    Args:
        tier: Evidence tier integer (1=highest, 5=lowest).

    Returns:
        Float weight in range [0.0, 1.0]. Returns _DEFAULT_WEIGHT for unknown tiers.
    """
    return TIER_WEIGHTS.get(tier, _DEFAULT_WEIGHT)


def tier_label(tier: int) -> str:
    """Return the human-readable label for a tier.

    Args:
        tier: Evidence tier integer.

    Returns:
        Label string (e.g. "Peer-Reviewed Research").
    """
    return TIER_LABELS.get(tier, f"Tier {tier}")


def source_type_to_tier(source_type: str) -> int:
    """Map a source type string to its default evidence tier.

    Args:
        source_type: Source type identifier (e.g. 'pubmed', 'forum').

    Returns:
        Evidence tier integer (1–5).
    """
    return SOURCE_TYPE_TIER_MAP.get(source_type.lower(), 3)


def aggregate_tier_score(tiers: list[int]) -> float:
    """Compute an aggregate evidence quality score from a list of tier values.

    Uses the mean of individual tier weights, providing a single float
    representing the overall evidence quality of a result set.

    Args:
        tiers: List of evidence tier integers (1–5).

    Returns:
        Float aggregate score in range [0.0, 1.0].
        Returns 0.0 for empty input.
    """
    if not tiers:
        return 0.0
    weights = [get_tier_weight(t) for t in tiers]
    return sum(weights) / len(weights)
