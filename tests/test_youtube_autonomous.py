"""Tests for autonomous YouTube ingestion (no network required).

The Data API discovery and transcript fetch are stubbed; we verify that
channel/title provenance is captured and threaded into the RawDocument with
a human-readable source_name.
"""

from __future__ import annotations

from pipelines.ingest_youtube import YouTubeIngestor


def _ingestor() -> YouTubeIngestor:
    ing = YouTubeIngestor(chroma_persist_dir="/tmp/does-not-exist")
    ing._discovery_provenance = {
        "vid123": {
            "channel_name": "Peptide Science",
            "channel_id": "UC_abc",
            "video_title": "BPC-157 Deep Dive",
            "video_published_at": "2023-01-01T00:00:00Z",
        }
    }
    ing._fetch_transcript = lambda v: "BPC-157 was dosed at 250 micrograms."  # type: ignore[assignment]
    return ing


class TestYouTubeProvenance:
    def test_human_source_name(self) -> None:
        docs = _ingestor().load_raw(video_ids=["vid123"])
        assert len(docs) == 1
        assert docs[0].source_name == "Peptide Science — BPC-157 Deep Dive"

    def test_channel_in_extra_metadata(self) -> None:
        docs = _ingestor().load_raw(video_ids=["vid123"])
        assert docs[0].extra_metadata["channel_name"] == "Peptide Science"
        assert docs[0].extra_metadata["video_title"] == "BPC-157 Deep Dive"

    def test_transcript_source_recorded(self) -> None:
        docs = _ingestor().load_raw(video_ids=["vid123"])
        assert docs[0].extra_metadata["transcript_source"] == "youtube_transcript_api"

    def test_url_built(self) -> None:
        docs = _ingestor().load_raw(video_ids=["vid123"])
        assert docs[0].source_url == "https://www.youtube.com/watch?v=vid123"

    def test_fallback_name_without_provenance(self) -> None:
        ing = YouTubeIngestor(chroma_persist_dir="/tmp/x")
        ing._fetch_transcript = lambda v: "some transcript"  # type: ignore[assignment]
        docs = ing.load_raw(video_ids=["lonelyvid"])
        assert docs[0].source_name == "YouTube:lonelyvid"

    def test_discovery_without_api_key_returns_empty(self) -> None:
        ing = YouTubeIngestor(youtube_api_key="")
        assert ing.discover_channel_videos("anything") == []


class TestYouTubeSilentFailureHardening:
    """A run that discovers videos but ingests nothing must not look clean."""

    def test_all_transcripts_failed_surfaces_error(self) -> None:
        ing = YouTubeIngestor(chroma_persist_dir="/tmp/x")
        ing.discover_channel_videos = lambda c, topic="": ["a", "b", "c"]  # type: ignore[assignment]
        ing._fetch_transcript = lambda v: None  # type: ignore[assignment]
        result = ing.run_autonomous("SomeChannel", topic="")
        assert result.count == 0
        assert result.skipped == 3
        assert not result.success
        assert result.errors

    def test_empty_discovery_surfaces_error(self) -> None:
        ing = YouTubeIngestor(chroma_persist_dir="/tmp/x")
        ing.discover_channel_videos = lambda c, topic="": []  # type: ignore[assignment]
        result = ing.run_autonomous("SomeChannel", topic="")
        assert result.count == 0
        assert not result.success
        assert result.errors

    def test_successful_ingest_has_no_error(self) -> None:
        ing = YouTubeIngestor(chroma_persist_dir="/tmp/x")
        ing.discover_channel_videos = lambda c, topic="": ["vid123"]  # type: ignore[assignment]
        ing._fetch_transcript = lambda v: "BPC-157 was dosed at 250 micrograms."  # type: ignore[assignment]
        result = ing.run_autonomous("SomeChannel", topic="")
        assert result.count > 0
        assert result.success
        assert result.errors == []
