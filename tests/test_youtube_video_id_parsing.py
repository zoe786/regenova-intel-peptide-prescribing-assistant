"""Tests for YouTube reference normalisation (no network required).

Covers the two ingestion-breaking bugs fixed alongside these tests:
  1. Full URLs (watch / youtu.be / shorts / live / embed) were stored and then
     passed verbatim to youtube_transcript_api as if they were bare video IDs.
  2. Trailing ``  # label`` comments written by the uploader corrupted
     otherwise-valid IDs, because only whole-line comments were skipped.
"""

from __future__ import annotations

import pytest

from pipelines.ingest_youtube import extract_video_id, parse_video_ids_file

VALID_ID = "dQw4w9WgXcQ"


class TestExtractVideoId:
    @pytest.mark.parametrize(
        "value",
        [
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ",
            "http://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=42",
            "youtu.be/dQw4w9WgXcQ",  # scheme-less
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/v/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "  https://youtu.be/dQw4w9WgXcQ  ",  # surrounding whitespace
        ],
    )
    def test_recovers_id(self, value: str) -> None:
        assert extract_video_id(value) == VALID_ID

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not a url and not an id",
            "https://www.youtube.com/",  # channel/home, no v
            "https://www.youtube.com/channel/UCabcdefghijklmnop",
            "https://vimeo.com/123456789",  # wrong host
            "https://example.com/watch?v=dQw4w9WgXcQ",  # v param, wrong host
            "dQw4w9WgX",  # too short (9 chars)
            "dQw4w9WgXcQextra",  # too long
        ],
    )
    def test_returns_none_on_invalid(self, value: str) -> None:
        assert extract_video_id(value) is None

    def test_never_raises_on_garbage(self) -> None:
        # Regression guard: parse_qs(...)['v'][0] style code raised on URLs
        # without a v param. The helper must swallow that and return None.
        assert extract_video_id("https://www.youtube.com/feed/subscriptions") is None


class TestParseVideoIdsFile:
    def test_bare_ids(self) -> None:
        text = "dQw4w9WgXcQ\nZbZSe6N_BXs\n"
        assert parse_video_ids_file(text) == ["dQw4w9WgXcQ", "ZbZSe6N_BXs"]

    def test_strips_trailing_label_comment(self) -> None:
        # Bug 2: the uploader writes "ID  # label"; the bare ID must survive.
        text = "dQw4w9WgXcQ  # peptide lecture\n"
        assert parse_video_ids_file(text) == ["dQw4w9WgXcQ"]

    def test_normalises_full_urls(self) -> None:
        # Bug 1: full URLs must be reduced to bare IDs.
        text = (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
            "https://youtu.be/ZbZSe6N_BXs  # short link with label\n"
        )
        assert parse_video_ids_file(text) == ["dQw4w9WgXcQ", "ZbZSe6N_BXs"]

    def test_skips_whole_line_comments_and_blanks(self) -> None:
        text = "# a heading comment\n\ndQw4w9WgXcQ\n   \n"
        assert parse_video_ids_file(text) == ["dQw4w9WgXcQ"]

    def test_drops_unparseable_lines(self) -> None:
        text = "dQw4w9WgXcQ\ngarbage-not-an-id\nhttps://vimeo.com/1\n"
        assert parse_video_ids_file(text) == ["dQw4w9WgXcQ"]

    def test_dedupes_preserving_order(self) -> None:
        text = (
            "dQw4w9WgXcQ\n"
            "https://youtu.be/dQw4w9WgXcQ  # same vid, different form\n"
            "ZbZSe6N_BXs\n"
        )
        assert parse_video_ids_file(text) == ["dQw4w9WgXcQ", "ZbZSe6N_BXs"]
