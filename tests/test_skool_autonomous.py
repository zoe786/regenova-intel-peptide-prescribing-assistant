"""Tests for Skool community ingestion: defensive autonomous gate + provenance."""

from __future__ import annotations

import json

from pipelines.ingest_skool_community import SkoolCommunityIngestor


class TestAutonomousGate:
    def test_disabled_by_default(self) -> None:
        ing = SkoolCommunityIngestor()
        assert ing.enable_autonomous is False
        assert ing.discover_posts(session_cookies={"x": "y"}) == []

    def test_enabled_still_returns_empty_until_reviewed(self) -> None:
        # Even when opted in, the unreviewed crawler returns nothing rather
        # than shipping a credentialed scraper.
        ing = SkoolCommunityIngestor(enable_autonomous=True)
        assert ing.discover_posts(session_cookies={"x": "y"}) == []


class TestExportProvenance:
    def test_community_and_author_captured(self, tmp_path) -> None:
        community_dir = tmp_path / "community"
        community_dir.mkdir()
        (community_dir / "peptide_group.json").write_text(
            json.dumps({
                "community_name": "Peptide Practitioners",
                "posts": [
                    {"author": "drjones", "content": "BPC-157 protocol discussion."}
                ],
            }),
            encoding="utf-8",
        )
        ing = SkoolCommunityIngestor(raw_dir=community_dir)
        docs = ing.load_raw()
        assert len(docs) == 1
        assert docs[0].extra_metadata["community_name"] == "Peptide Practitioners"
        assert docs[0].extra_metadata["post_author"] == "drjones"

    def test_tier_pinned_to_4(self, tmp_path) -> None:
        community_dir = tmp_path / "community"
        community_dir.mkdir()
        (community_dir / "g.json").write_text(
            json.dumps([{"author": "a", "content": "text here"}]), encoding="utf-8"
        )
        ing = SkoolCommunityIngestor(raw_dir=community_dir, chroma_persist_dir="/tmp/x")
        docs = ing.load_raw()
        result = ing.process(docs)
        # Can't read tier off the record list directly here, but ensure no crash
        # and that the source documents carry tier 4.
        assert docs[0].evidence_tier_default == 4
        assert result.source_type == "skool_community"
