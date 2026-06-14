"""Tests for transcript-fetch retry + IP-rotation behaviour (no network).

These cover the fix for the burst-failure root cause: WebshareProxyConfig's
forced per-request IP rotation broke the multi-request timedtext flow, surfacing
as RequestBlocked (/sorry 429) and transport tears (ChunkedEncodingError /
IncompleteRead). _fetch_transcript now uses a fresh sticky-IP session per attempt
and retries those failure classes, while bailing immediately on permanent ones.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from pipelines.ingest_youtube import YouTubeIngestor

VID = "dQw4w9WgXcQ"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Don't actually sleep during retry backoff.
    monkeypatch.setattr(time, "sleep", lambda *_: None)


def _ingestor() -> YouTubeIngestor:
    return YouTubeIngestor(chroma_persist_dir="/tmp/does-not-exist")


def _fake_fetch_result(text: str):
    """Mimic FetchedTranscript: .fetch(vid).to_raw_data() → list[dict]."""
    res = MagicMock()
    res.to_raw_data.return_value = [{"text": text}]
    return res


def _api_returning(*side_effects):
    """Build a fake api whose .fetch() applies the given side effects in order."""
    api = MagicMock()
    api.fetch.side_effect = side_effects
    return api


class TestFetchRetry:
    def test_succeeds_first_try(self):
        ing = _ingestor()
        api = _api_returning(_fake_fetch_result("hello world"))
        ing._build_transcript_api = lambda: api  # type: ignore[assignment]
        assert ing._fetch_transcript(VID) == "hello world"
        assert api.fetch.call_count == 1

    def test_retries_on_chunked_encoding_error_then_succeeds(self):
        from requests.exceptions import ChunkedEncodingError

        ing = _ingestor()
        # First call tears mid-stream, second call (fresh IP) succeeds.
        calls = {"n": 0}

        def build():
            calls["n"] += 1
            if calls["n"] == 1:
                return _api_returning(ChunkedEncodingError("Connection broken"))
            return _api_returning(_fake_fetch_result("recovered"))

        ing._build_transcript_api = build  # type: ignore[assignment]
        assert ing._fetch_transcript(VID) == "recovered"
        # Two fresh sessions were built → IP rotation happened.
        assert calls["n"] == 2

    def test_retries_on_request_blocked_then_succeeds(self):
        from youtube_transcript_api._errors import RequestBlocked

        ing = _ingestor()
        calls = {"n": 0}

        def build():
            calls["n"] += 1
            if calls["n"] < 3:
                return _api_returning(RequestBlocked(VID))
            return _api_returning(_fake_fetch_result("third time lucky"))

        ing._build_transcript_api = build  # type: ignore[assignment]
        assert ing._fetch_transcript(VID) == "third time lucky"
        assert calls["n"] == 3

    def test_does_not_retry_on_transcripts_disabled(self):
        from youtube_transcript_api._errors import TranscriptsDisabled

        ing = _ingestor()
        api = _api_returning(TranscriptsDisabled(VID))
        ing._build_transcript_api = lambda: api  # type: ignore[assignment]
        assert ing._fetch_transcript(VID) is None
        # Permanent condition → exactly one attempt, no rotation.
        assert api.fetch.call_count == 1

    def test_does_not_retry_on_no_transcript_found(self):
        from youtube_transcript_api._errors import NoTranscriptFound

        ing = _ingestor()
        # NoTranscriptFound signature varies by version; build defensively.
        try:
            exc = NoTranscriptFound(VID, ["en"], {})
        except TypeError:
            exc = NoTranscriptFound(VID)
        api = _api_returning(exc)
        ing._build_transcript_api = lambda: api  # type: ignore[assignment]
        assert ing._fetch_transcript(VID) is None
        assert api.fetch.call_count == 1

    def test_gives_up_after_max_attempts(self, monkeypatch):
        from requests.exceptions import ChunkedEncodingError

        monkeypatch.setenv("YT_FETCH_MAX_ATTEMPTS", "3")
        ing = _ingestor()
        calls = {"n": 0}

        def build():
            calls["n"] += 1
            return _api_returning(ChunkedEncodingError("still broken"))

        ing._build_transcript_api = build  # type: ignore[assignment]
        assert ing._fetch_transcript(VID) is None
        # Exhausted exactly max_attempts fresh sessions.
        assert calls["n"] == 3


class TestStickyProxyUrl:
    def test_builds_sticky_url_without_rotate_suffix(self, monkeypatch):
        """The Webshare proxy URL must NOT carry the -rotate suffix."""
        monkeypatch.setenv("WEBSHARE_PROXY_USERNAME", "myuser")
        monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "mypass")

        captured = {}

        # Intercept GenericProxyConfig to inspect the URL the code builds.
        import youtube_transcript_api.proxies as proxies_mod

        real_generic = proxies_mod.GenericProxyConfig

        def spy(http_url=None, https_url=None):
            captured["http_url"] = http_url
            return real_generic(http_url=http_url, https_url=https_url)

        monkeypatch.setattr(proxies_mod, "GenericProxyConfig", spy)

        ing = _ingestor()
        ing._build_transcript_api()

        assert "http_url" in captured, "GenericProxyConfig was not used"
        url = captured["http_url"]
        assert "myuser" in url
        assert "-rotate" not in url, f"sticky URL must not rotate per request: {url}"
        assert "p.webshare.io" in url

    def test_strips_user_supplied_rotate_suffix(self, monkeypatch):
        monkeypatch.setenv("WEBSHARE_PROXY_USERNAME", "myuser-rotate")
        monkeypatch.setenv("WEBSHARE_PROXY_PASSWORD", "mypass")

        captured = {}
        import youtube_transcript_api.proxies as proxies_mod
        real_generic = proxies_mod.GenericProxyConfig

        def spy(http_url=None, https_url=None):
            captured["http_url"] = http_url
            return real_generic(http_url=http_url, https_url=https_url)

        monkeypatch.setattr(proxies_mod, "GenericProxyConfig", spy)

        ing = _ingestor()
        ing._build_transcript_api()
        assert "-rotate" not in captured["http_url"]
        # The bare username (rotate stripped) is present.
        assert "myuser:" in captured["http_url"]
