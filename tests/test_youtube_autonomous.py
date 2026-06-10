"""Tests for autonomous YouTube ingestion (no network required).

The Data API discovery and transcript fetch are stubbed; we verify that
channel/title provenance is captured and threaded into the RawDocument with
a human-readable source_name.
"""

from __future__ import annotations

from pipelines.ingest_youtube import YouTubeIngestor


def _disable_throttle(ing: YouTubeIngestor) -> None:
    """Zero out inter-request delays so unit tests don't sleep for real."""
    ing._throttle_base_s = 0.0
    ing._throttle_jitter_s = 0.0


def _ingestor() -> YouTubeIngestor:
    ing = YouTubeIngestor(chroma_persist_dir="/tmp/does-not-exist")
    _disable_throttle(ing)
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
        _disable_throttle(ing)
        ing._fetch_transcript = lambda v: "some transcript"  # type: ignore[assignment]
        docs = ing.load_raw(video_ids=["lonelyvid"])
        assert docs[0].source_name == "YouTube:lonelyvid"

    def test_discovery_without_api_key_returns_empty(self) -> None:
        ing = YouTubeIngestor(youtube_api_key="")
        assert ing.discover_channel_videos("anything") == []


class TestYouTubeSilentFailureHardening:
    """A run that discovers videos but ingests nothing must not look clean."""

    def test_all_transcripts_failed_surfaces_error(self, monkeypatch) -> None:
        ing = YouTubeIngestor(chroma_persist_dir="/tmp/x")
        _disable_throttle(ing)
        # Backoff still fires on failures (independent of throttle config), so
        # neutralise real sleeps to keep the test fast.
        monkeypatch.setattr("pipelines.ingest_youtube.time.sleep", lambda *_: None)
        ing.discover_channel_videos = lambda c, topic="": ["a", "b", "c"]  # type: ignore[assignment]
        ing._fetch_transcript = lambda v: None  # type: ignore[assignment]
        result = ing.run_autonomous("SomeChannel", topic="")
        assert result.count == 0
        assert result.skipped == 3
        assert not result.success
        assert result.errors

    def test_empty_discovery_surfaces_error(self) -> None:
        ing = YouTubeIngestor(chroma_persist_dir="/tmp/x")
        _disable_throttle(ing)
        ing.discover_channel_videos = lambda c, topic="": []  # type: ignore[assignment]
        result = ing.run_autonomous("SomeChannel", topic="")
        assert result.count == 0
        assert not result.success
        assert result.errors

    def test_successful_ingest_has_no_error(self) -> None:
        ing = YouTubeIngestor(chroma_persist_dir="/tmp/x")
        _disable_throttle(ing)
        ing.discover_channel_videos = lambda c, topic="": ["vid123"]  # type: ignore[assignment]
        ing._fetch_transcript = lambda v: "BPC-157 was dosed at 250 micrograms."  # type: ignore[assignment]
        result = ing.run_autonomous("SomeChannel", topic="")
        assert result.count > 0
        assert result.success
        assert result.errors == []


class TestThrottling:
    """Inter-request throttling and failure backoff in load_raw."""

    def test_first_request_not_delayed_then_one_sleep_per_gap(self, monkeypatch) -> None:
        ing = _ingestor()
        ing._throttle_base_s = 2.0
        ing._throttle_jitter_s = 0.0
        sleeps: list[float] = []
        monkeypatch.setattr("pipelines.ingest_youtube.time.sleep", sleeps.append)
        ing._fetch_transcript = lambda v: "text"  # type: ignore[assignment]
        ing.load_raw(video_ids=["a", "b", "c"])
        assert sleeps == [2.0, 2.0]  # 3 videos -> 2 inter-request waits

    def test_single_video_no_sleep(self, monkeypatch) -> None:
        ing = _ingestor()
        ing._throttle_base_s = 2.0
        sleeps: list[float] = []
        monkeypatch.setattr("pipelines.ingest_youtube.time.sleep", sleeps.append)
        ing._fetch_transcript = lambda v: "text"  # type: ignore[assignment]
        ing.load_raw(video_ids=["solo"])
        assert sleeps == []

    def test_failure_triggers_exponential_backoff(self, monkeypatch) -> None:
        ing = _ingestor()
        ing._throttle_base_s = 0.0
        ing._throttle_jitter_s = 0.0
        sleeps: list[float] = []
        monkeypatch.setattr("pipelines.ingest_youtube.time.sleep", sleeps.append)
        ing._fetch_transcript = lambda v: None  # type: ignore[assignment]
        ing.load_raw(video_ids=["a", "b", "c", "d"])
        # base=0: only backoff produces real sleeps, entering vids 2/3/4.
        assert sleeps == [5.0, 10.0, 20.0]

    def test_backoff_resets_after_success(self, monkeypatch) -> None:
        ing = _ingestor()
        ing._throttle_base_s = 0.0
        ing._throttle_jitter_s = 0.0
        sleeps: list[float] = []
        monkeypatch.setattr("pipelines.ingest_youtube.time.sleep", sleeps.append)
        seq = iter([None, None, "ok", None])  # fail, fail, succeed, fail
        ing._fetch_transcript = lambda v: next(seq)  # type: ignore[assignment]
        ing.load_raw(video_ids=["a", "b", "c", "d"])
        # enter b: 5 (a failed); enter c: 10 (b failed); enter d: 0 (c ok -> reset)
        assert sleeps == [5.0, 10.0]
