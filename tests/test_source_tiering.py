"""pytest tests for evidence tiering utilities.

Tests tier weights, labels, source-type mapping, and aggregate scoring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from knowledge.scoring.evidence_tiering import (
    TIER_WEIGHTS,
    aggregate_tier_score,
    get_tier_weight,
    source_type_to_tier,
    tier_label,
)


class TestTierWeights:
    def test_tier_weights_range(self):
        """All tier weights should be between 0 and 1 (inclusive)."""
        for tier, weight in TIER_WEIGHTS.items():
            assert 0.0 <= weight <= 1.0, f"Tier {tier} weight {weight} out of range"

    def test_tier_1_highest(self):
        """Tier 1 weight should be strictly higher than tier 5 weight."""
        assert TIER_WEIGHTS[1] > TIER_WEIGHTS[5]

    def test_tier_weights_strictly_decreasing(self):
        """Tier weights should be monotonically decreasing from tier 1 to 5."""
        weights = [TIER_WEIGHTS[t] for t in sorted(TIER_WEIGHTS.keys())]
        for i in range(len(weights) - 1):
            assert weights[i] > weights[i + 1], (
                f"Tier {i+1} weight {weights[i]} not greater than tier {i+2} weight {weights[i+1]}"
            )

    def test_tier_1_weight_is_1(self):
        """Tier 1 should have weight exactly 1.0."""
        assert TIER_WEIGHTS[1] == 1.0

    def test_all_five_tiers_defined(self):
        """All five standard tiers should be present in TIER_WEIGHTS."""
        for t in range(1, 6):
            assert t in TIER_WEIGHTS, f"Tier {t} missing from TIER_WEIGHTS"


class TestGetTierWeight:
    def test_returns_correct_weights(self):
        assert get_tier_weight(1) == 1.0
        assert get_tier_weight(5) == 0.15

    def test_unknown_tier_returns_default(self):
        """Unknown tier (e.g. 99) should return a fallback weight > 0."""
        w = get_tier_weight(99)
        assert 0 < w <= 1.0


class TestTierLabel:
    def test_tier_1_label(self):
        label = tier_label(1)
        assert "peer" in label.lower() or "research" in label.lower() or len(label) > 3

    def test_all_tiers_have_labels(self):
        for t in range(1, 6):
            label = tier_label(t)
            assert isinstance(label, str) and len(label) > 0


class TestSourceTypeToTier:
    def test_pubmed_is_tier_1(self):
        assert source_type_to_tier("pubmed") == 1

    def test_document_is_tier_2(self):
        assert source_type_to_tier("document") == 2

    def test_website_is_tier_3(self):
        assert source_type_to_tier("website") == 3

    def test_forum_is_tier_4(self):
        assert source_type_to_tier("forum") == 4

    def test_youtube_is_tier_3(self):
        assert source_type_to_tier("youtube") == 3

    def test_skool_community_is_tier_4(self):
        assert source_type_to_tier("skool_community") == 4

    def test_unknown_source_type_returns_3(self):
        """Unknown source types should default to tier 3."""
        assert source_type_to_tier("something_unknown") == 3

    def test_case_insensitive(self):
        assert source_type_to_tier("PUBMED") == source_type_to_tier("pubmed")


class TestAggregateScore:
    def test_single_tier_1_returns_1(self):
        assert aggregate_tier_score([1]) == 1.0

    def test_single_tier_5_returns_low_score(self):
        assert aggregate_tier_score([5]) == 0.15

    def test_mixed_tiers_returns_reasonable_aggregate(self):
        score = aggregate_tier_score([1, 3, 4])
        # Expected: mean([1.0, 0.65, 0.40]) = 0.683...
        assert 0.3 < score < 0.9, f"Aggregate score {score} outside expected range"

    def test_empty_list_returns_zero(self):
        assert aggregate_tier_score([]) == 0.0

    def test_all_tier_1_returns_1(self):
        assert aggregate_tier_score([1, 1, 1]) == 1.0

    def test_aggregate_score_is_float(self):
        score = aggregate_tier_score([1, 2, 3])
        assert isinstance(score, float)
