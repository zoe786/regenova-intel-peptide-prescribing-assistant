"""YouTube transcript ingestor.

Reads video IDs from data/raw/youtube/video_ids.txt,
fetches transcripts via youtube_transcript_api, and chunks them.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pipelines.common.chunking import chunk_by_tokens
from pipelines.common.cleaners import normalize_whitespace
from pipelines.common.metadata_enrichment import compute_content_hash, generate_document_id
from pipelines.common.models import IngestionResult, NormalizedRecord, RawDocument
from pipelines.common.storage import save_normalized, save_to_vector_store

logger = logging.getLogger(__name__)

DEFAULT_EVIDENCE_TIER = 3
SOURCE_TYPE = "youtube"
YOUTUBE_BASE_URL = "https://www.youtube.com/watch?v="

# YouTube video IDs are exactly 11 chars: [A-Za-z0-9_-].
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Host suffixes we recognise as YouTube. Compared against the parsed netloc
# with any leading "www." removed.
_YOUTUBE_HOSTS = {"youtube.com", "m.youtube.com", "youtu.be", "youtube-nocookie.com"}


def extract_video_id(value: str) -> str | None:
    """Normalise a user-supplied YouTube reference to a bare 11-char video ID.

    Accepts, and returns the ID from, any of:
      - a bare ID:                       ``dQw4w9WgXcQ``
      - watch URLs:                      ``https://www.youtube.com/watch?v=ID``
      - short links:                     ``https://youtu.be/ID``
      - shorts / live / embed / v paths: ``.../shorts/ID``, ``.../live/ID``,
                                         ``.../embed/ID``, ``.../v/ID``
    Query strings and fragments (``?t=30s``, ``&list=...``, ``#...``) are ignored.

    Returns the normalised ID, or ``None`` if no valid 11-char ID can be
    recovered — callers decide whether that's a skip (ingestor) or a 400
    (API). Never raises on malformed input.
    """
    if not value:
        return None
    value = value.strip()

    # Bare ID fast-path.
    if _VIDEO_ID_RE.match(value):
        return value

    # Tolerate scheme-less URLs like "youtu.be/ID" so urlparse populates netloc.
    parse_target = value
    if "://" not in value and ("youtube" in value or "youtu.be" in value):
        parse_target = "https://" + value

    parsed = urlparse(parse_target)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    candidate: str | None = None
    if host in _YOUTUBE_HOSTS:
        if host == "youtu.be":
            # Path is /<id>
            candidate = parsed.path.lstrip("/").split("/", 1)[0]
        else:
            qs = parse_qs(parsed.query)
            if "v" in qs and qs["v"]:
                candidate = qs["v"][0]
            else:
                # /shorts/<id>, /live/<id>, /embed/<id>, /v/<id>
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
                    candidate = parts[1]

    if candidate and _VIDEO_ID_RE.match(candidate):
        return candidate
    return None


def parse_video_ids_file(text: str) -> list[str]:
    """Parse the contents of ``video_ids.txt`` into normalised video IDs.

    Robust to two things the writer (apps.api.routers.upload) can produce that
    older parsing got wrong:
      1. Full URLs instead of bare IDs.
      2. Trailing ``  # label`` comments appended to otherwise-valid lines —
         previously only whole-line comments (lines starting with ``#``) were
         skipped, so a trailing label silently corrupted the ID.

    Lines that yield no valid ID are dropped (and logged by the caller via the
    returned-vs-input count). Order is preserved; duplicates are de-duped while
    preserving first-seen order.
    """
    seen: set[str] = set()
    ids: list[str] = []
    for raw_line in text.splitlines():
        # Strip trailing inline comments, then whitespace.
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        vid = extract_video_id(line)
        if vid and vid not in seen:
            seen.add(vid)
            ids.append(vid)
    return ids



class YouTubeIngestor:
    """Ingestor for YouTube video transcripts.

    Supports two modes:
    - File mode (default): read video IDs from video_ids.txt.
    - Autonomous mode: given a channel name or URL, enumerate every uploaded video
      via the YouTube Data API, capture channel + title provenance, optionally
      triage relevance with the LLM, then fetch transcripts and ingest.
    """

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/youtube"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
        max_tokens_per_chunk: int = 512,
        youtube_api_key: str = "",
        llm_assistant: "LLMAssistant | None" = None,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir
        self.max_tokens = max_tokens_per_chunk
        self.ids_file = self.raw_dir / "video_ids.txt"
        self.youtube_api_key = youtube_api_key
        self.llm = llm_assistant
        # video_id -> {channel_name, channel_id, video_title, ...}
        self._discovery_provenance: dict[str, dict[str, str]] = {}
        # Inter-request throttling for transcript fetches. Bursting a whole
        # channel's worth of requests is what triggers YouTube's IP block, so
        # we sleep a randomised interval *between* fetches (not after the last
        # one). Tunable via env without code changes; 0 disables throttling.
        import os
        self._throttle_base_s = float(
            os.environ.get("YT_FETCH_DELAY_SECONDS", "2.0")
        )
        self._throttle_jitter_s = float(
            os.environ.get("YT_FETCH_JITTER_SECONDS", "1.5")
        )

    # ── URL Parsing ────────────────────────────────────────────────────────

    def _parse_channel_url(self, url_or_name: str) -> str | None:
        """Extract channel identifier from YouTube URL or return name as-is.

        Supports:
          https://www.youtube.com/@DrAFroese         → "DrAFroese"
          https://www.youtube.com/channel/UCxxxxxx   → "UCxxxxxx"
          https://www.youtube.com/c/ChannelName      → "ChannelName"
          DrAFroese (display name)                   → "DrAFroese"

        Args:
            url_or_name: YouTube URL or channel display name.

        Returns:
            Channel identifier to pass to YouTube API, or None if invalid.
        """
        url_or_name = url_or_name.strip()
        
        # If it doesn't look like a URL, treat as display name
        if not url_or_name.startswith("http"):
            return url_or_name
        
        try:
            parsed = urllib.parse.urlparse(url_or_name)
        except Exception as e:
            logger.warning("Failed to parse URL: %s", e)
            return None
        
        if "youtube.com" not in parsed.netloc and "youtu.be" not in parsed.netloc:
            logger.warning("Not a YouTube URL: %s", url_or_name)
            return None
        
        path_parts = parsed.path.strip("/").split("/")
        
        if not path_parts or not path_parts[0]:
            logger.warning("Could not extract channel identifier from URL: %s", url_or_name)
            return None
        
        identifier = path_parts[0]
        
        # Handle vanity URLs: @ChannelName
        if identifier.startswith("@"):
            return identifier[1:]  # Remove the @ symbol
        
        # Handle /channel/ID format: channel/UCxxxxxx
        if identifier == "channel" and len(path_parts) > 1:
            return path_parts[1]  # "UCxxxxxx"
        
        # Handle /c/Name format: c/ChannelName
        if identifier == "c" and len(path_parts) > 1:
            return path_parts[1]  # "ChannelName"
        
        # If nothing matched, return the identifier as-is (treat as name)
        return identifier

    # ── Autonomous discovery via YouTube Data API ─────────────────────────

    def discover_channel_videos(self, channel_name_or_url: str, topic: str = "") -> list[str]:
        """Enumerate every uploaded video for a channel name or URL.

        Resolves the channel name/URL to its uploads playlist, paginates all
        items, captures channel/title provenance, and (when an LLM is
        available and a topic is given) drops clearly off-topic videos.

        Args:
            channel_name_or_url: Channel display name, URL, or channel ID.
            topic: Optional topic for relevance triage (e.g. "peptides").

        Returns:
            List of discovered video IDs (after optional triage).
        """
        if not self.youtube_api_key:
            logger.error("YOUTUBE_API_KEY not set — cannot discover channel videos")
            return []
        try:
            from googleapiclient.discovery import build  # type: ignore[import]
        except ImportError:
            logger.error("google-api-python-client not installed — channel discovery unavailable")
            return []

        # Parse URL to extract channel identifier
        channel_identifier = self._parse_channel_url(channel_name_or_url)
        if not channel_identifier:
            logger.error("Could not parse channel from: %s", channel_name_or_url)
            return []

        try:
            youtube = build("youtube", "v3", developerKey=self.youtube_api_key)

            # 1. Resolve channel name/ID → channel id + uploads playlist.
            search = youtube.search().list(
                q=channel_identifier, type="channel", part="snippet", maxResults=1
            ).execute()
            items = search.get("items", [])
            if not items:
                logger.warning("No channel found for '%s'", channel_identifier)
                return []
            channel_id = items[0]["snippet"]["channelId"]
            resolved_name = items[0]["snippet"]["title"]

            channels = youtube.channels().list(
                id=channel_id, part="contentDetails"
            ).execute()
            uploads = (
                channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
            )

            # 2. Paginate the uploads playlist for every video.
            video_ids: list[str] = []
            page_token = None
            from pipelines.common.llm_assistant import LLMAssistant
            assistant = self.llm or LLMAssistant()

            while True:
                pl = youtube.playlistItems().list(
                    playlistId=uploads,
                    part="snippet,contentDetails",
                    maxResults=50,
                    pageToken=page_token,
                ).execute()
                for it in pl.get("items", []):
                    vid = it["contentDetails"]["videoId"]
                    title = it["snippet"]["title"]
                    published = it["snippet"].get("publishedAt", "")

                    # Optional relevance triage before we spend transcript cost.
                    if topic and assistant.available:
                        decision = assistant.is_relevant(topic, title)
                        if not decision.value:
                            logger.info("Triage dropped off-topic video: %s", title)
                            continue

                    self._discovery_provenance[vid] = {
                        "channel_name": resolved_name,
                        "channel_id": channel_id,
                        "video_title": title,
                        "video_published_at": published,
                    }
                    video_ids.append(vid)

                page_token = pl.get("nextPageToken")
                if not page_token:
                    break

            logger.info(
                "Discovered %d videos for channel '%s' (%s)",
                len(video_ids), resolved_name, channel_id,
            )
            return video_ids
        except Exception as exc:
            logger.error("Channel discovery failed for '%s': %s", channel_identifier, exc)
            return []

    def _throttle_sleep(self, extra: float = 0.0) -> None:
        """Sleep a randomised interval to avoid bursting requests.

        Total delay = base + uniform(0, jitter) + extra(backoff). Randomised
        jitter desynchronises the request pattern so it looks less automated;
        ``extra`` carries exponential backoff accumulated from prior failures.
        Set YT_FETCH_DELAY_SECONDS=0 (and jitter=0) to disable.
        """
        import random

        delay = self._throttle_base_s + random.uniform(0, self._throttle_jitter_s)
        delay += max(extra, 0.0)
        if delay > 0:
            logger.debug("Throttling transcript fetch: sleeping %.2fs", delay)
            time.sleep(delay)

    def _build_transcript_api(self):
        """Construct a YouTubeTranscriptApi with a STICKY-IP proxy.

        Why not WebshareProxyConfig? Its `.url` forces a ``-rotate`` suffix on
        the proxy username and sets ``prevent_keeping_connections_alive=True``,
        which gives a *new residential IP on every HTTP request*. A single
        transcript fetch is three sequential requests:

            GET  /watch?v=…                  (establish)
            POST /youtubei/v1/player          (mints a signed timedtext URL,
                                               bound to the requesting IP)
            GET  /api/timedtext?…&signature=… (the actual caption stream)

        Under per-request rotation those three go out on three *different* IPs,
        so the timedtext GET presents the player's IP-bound signature from the
        wrong IP. YouTube then either rejects it (302 → www.google.com/sorry,
        surfacing as RequestBlocked/IpBlocked) or starts the stream and severs
        it mid-transfer (ChunkedEncodingError / IncompleteRead). This is the
        root cause of transcripts failing in bursts while a lone fetch
        occasionally succeeds by luck of the rotation.

        Fix: build the proxy from the *plain* Webshare username (no ``-rotate``)
        as a GenericProxyConfig, which holds ONE IP for the lifetime of the
        TCP connection. All three sub-requests of a fetch then share a single
        coherent IP. Rotation happens *between* fetches/retries instead, because
        each call here returns a fresh api → fresh session → fresh connection →
        fresh sticky IP.

        Priority: Webshare creds → generic YT_TRANSCRIPT_PROXY (SSH-tunnel
        demo) → no proxy.
        """
        import os
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore[import]

        ws_user = os.environ.get("WEBSHARE_PROXY_USERNAME", "").strip()
        ws_pass = os.environ.get("WEBSHARE_PROXY_PASSWORD", "").strip()

        if ws_user and ws_pass:
            from youtube_transcript_api.proxies import GenericProxyConfig
            # Strip any user-supplied -rotate; we deliberately want sticky.
            if ws_user.endswith("-rotate"):
                ws_user = ws_user[: -len("-rotate")]
            host = os.environ.get("WEBSHARE_PROXY_HOST", "p.webshare.io").strip()
            port = os.environ.get("WEBSHARE_PROXY_PORT", "80").strip()
            ws_url = f"http://{ws_user}:{ws_pass}@{host}:{port}/"
            cfg = GenericProxyConfig(http_url=ws_url, https_url=ws_url)
            return YouTubeTranscriptApi(proxy_config=cfg)

        proxy_url = os.environ.get("YT_TRANSCRIPT_PROXY", "").strip()
        if proxy_url:
            from youtube_transcript_api.proxies import GenericProxyConfig
            cfg = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
            return YouTubeTranscriptApi(proxy_config=cfg)

        return YouTubeTranscriptApi()

    def _fetch_transcript(self, video_id: str) -> str | None:
        """Fetch transcript text for a YouTube video ID, with IP rotation.

        Each attempt uses a fresh sticky-IP session (see _build_transcript_api),
        so a failed attempt is retried on a *different* residential IP. Two
        failure classes are retried — explicit blocks (RequestBlocked/IpBlocked)
        and transport tears (ChunkedEncodingError / IncompleteRead / connection
        resets), both of which are symptoms of a bad/throttled exit IP that a
        rotation will move us off. Permanent conditions (no transcript, disabled,
        unavailable, age-restricted) are NOT retried — we bail immediately.
        """
        import os
        import random

        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # noqa: F401
        except ImportError as exc:
            logger.error("youtube_transcript_api not installed: %s", exc)
            return None

        # Library-level "give up, this video has no transcript" errors.
        from youtube_transcript_api._errors import (
            CouldNotRetrieveTranscript,
            RequestBlocked,
            IpBlocked,
        )
        # Transport-level errors that indicate a flaky/throttled exit IP.
        from requests.exceptions import (
            ChunkedEncodingError,
            ConnectionError as RequestsConnectionError,
            ReadTimeout,
            ProxyError,
        )
        from urllib3.exceptions import ProtocolError, IncompleteRead

        retryable_transport = (
            ChunkedEncodingError,
            RequestsConnectionError,
            ReadTimeout,
            ProxyError,
            ProtocolError,
            IncompleteRead,
        )
        retryable_block = (RequestBlocked, IpBlocked)

        max_attempts = int(os.environ.get("YT_FETCH_MAX_ATTEMPTS", "6"))
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                api = self._build_transcript_api()
                raw = api.fetch(video_id).to_raw_data()
                if attempt > 1:
                    logger.info(
                        "Transcript for %s succeeded on attempt %d/%d",
                        video_id, attempt, max_attempts,
                    )
                return " ".join(entry["text"] for entry in raw)

            except retryable_block as exc:
                last_exc = exc
                logger.warning(
                    "Transcript fetch %s blocked (attempt %d/%d: %s) — "
                    "rotating to a fresh IP",
                    video_id, attempt, max_attempts, type(exc).__name__,
                )
            except retryable_transport as exc:
                last_exc = exc
                logger.warning(
                    "Transcript fetch %s transport error (attempt %d/%d: %s) — "
                    "rotating to a fresh IP",
                    video_id, attempt, max_attempts, type(exc).__name__,
                )
            except CouldNotRetrieveTranscript as exc:
                # NoTranscriptFound, TranscriptsDisabled, VideoUnavailable,
                # AgeRestricted, etc. Retrying won't help — bail now.
                logger.info(
                    "Transcript for %s unavailable (%s) — not retrying",
                    video_id, type(exc).__name__,
                )
                return None
            except Exception as exc:  # noqa: BLE001 - unknown; retry defensively
                last_exc = exc
                logger.warning(
                    "Transcript fetch %s unexpected error (attempt %d/%d: %s) — "
                    "rotating to a fresh IP",
                    video_id, attempt, max_attempts, type(exc).__name__,
                )

            # Short randomised backoff before the next IP, capped, to avoid a
            # tight reconnect loop while still moving quickly through bad exits.
            if attempt < max_attempts:
                sleep_s = min(2 ** (attempt - 1), 8) + random.uniform(0, 1.0)
                time.sleep(sleep_s)

        logger.error(
            "Failed to fetch transcript for %s after %d attempts: %s",
            video_id, max_attempts, last_exc,
        )
        return None

    def load_raw(self, video_ids: list[str] | None = None) -> list[RawDocument]:
        if video_ids is None:
            if not self.ids_file.exists():
                logger.warning("Video IDs file not found: %s", self.ids_file)
                return []
            video_ids = parse_video_ids_file(self.ids_file.read_text())
            logger.info("YouTubeIngestor: found %d video IDs", len(video_ids))

        docs: list[RawDocument] = []
        self._last_skipped = 0
        self._last_video_count = len(video_ids)
        backoff_s = 0.0  # grows on consecutive failures, resets on success
        for idx, vid_id in enumerate(video_ids):
            # Throttle between requests to avoid the burst pattern that trips
            # YouTube's rate limiter. Skip the wait before the first video so
            # single-video ingests aren't penalised.
            if idx > 0:
                self._throttle_sleep(extra=backoff_s)

            transcript = self._fetch_transcript(vid_id)
            if not transcript:
                self._last_skipped += 1
                # A failure is the strongest signal we're being throttled, so
                # back off exponentially (capped) before the next attempt.
                backoff_s = min((backoff_s * 2) or 5.0, 60.0)
                logger.warning(
                    "No transcript for video %s — skipping (backoff now %.1fs)",
                    vid_id, backoff_s,
                )
                continue
            backoff_s = 0.0  # recovered; drop back to base throttle
            url = f"{YOUTUBE_BASE_URL}{vid_id}"
            prov = dict(self._discovery_provenance.get(vid_id, {}))
            # Human-readable name for citations; falls back to the ID.
            channel = prov.get("channel_name")
            title = prov.get("video_title")
            if channel and title:
                source_name = f"{channel} — {title}"
            elif title:
                source_name = title
            else:
                source_name = f"YouTube:{vid_id}"
            # Record that this transcript was auto-generated/best-effort, so
            # the tier and provenance make the source quality explicit.
            prov.setdefault("transcript_source", "youtube_transcript_api")

            docs.append(RawDocument(
                source_type=SOURCE_TYPE,
                source_name=source_name,
                raw_content=normalize_whitespace(transcript),
                acquired_at=datetime.utcnow(),
                source_url=url,
                evidence_tier_default=DEFAULT_EVIDENCE_TIER,
                extra_metadata=prov,
            ))

        return docs

    def process(self, docs: list[RawDocument]) -> IngestionResult:
        result = IngestionResult(source_type=SOURCE_TYPE)
        records: list[NormalizedRecord] = []

        for doc in docs:
            try:
                chunks = chunk_by_tokens(doc.raw_content, self.max_tokens)
                document_id = generate_document_id(doc.source_url, doc.acquired_at, doc.source_name)

                for idx, chunk_text in enumerate(chunks):
                    record = NormalizedRecord(
                        chunk_id=f"{document_id}_{idx:04d}",
                        document_id=document_id,
                        source_type=SOURCE_TYPE,
                        source_name=doc.source_name,
                        source_url=doc.source_url,
                        acquired_at=doc.acquired_at,
                        evidence_tier_default=DEFAULT_EVIDENCE_TIER,
                        content_hash=compute_content_hash(chunk_text),
                        content=chunk_text,
                        chunk_index=idx,
                        extra_metadata=dict(doc.extra_metadata or {}),
                    )
                    save_normalized(record, self.output_dir)
                    records.append(record)
                    result.count += 1
            except Exception as exc:
                logger.error("Error processing %s: %s", doc.source_name, exc)
                result.errors.append(str(exc))

        if records:
            save_to_vector_store(records, chroma_persist_dir=self.chroma_persist_dir)
        return result

    def run(self) -> IngestionResult:
        start = time.time()
        docs = self.load_raw()
        result = self.process(docs)
        result.duration_seconds = time.time() - start
        logger.info("%s", result)
        return result

    def run_autonomous(self, channel_name_or_url: str, topic: str = "") -> IngestionResult:
        """Discover and ingest every video from a channel.

        Args:
            channel_name_or_url: Channel display name, URL, or channel ID.
            topic: Optional topic for relevance triage.

        Returns:
            IngestionResult summarising the run.
        """
        start = time.time()
        video_ids = self.discover_channel_videos(channel_name_or_url, topic=topic)
        if not video_ids:
            result = IngestionResult(source_type=SOURCE_TYPE)
            result.errors.append(
                f"Channel discovery returned no videos for '{channel_name_or_url}' "
                "(resolution failed, channel empty, or discovery crashed — "
                "check worker logs)."
            )
            result.duration_seconds = time.time() - start
            logger.warning("%s (autonomous channel=%s)", result, channel_name_or_url)
            return result

        docs = self.load_raw(video_ids=video_ids)
        result = self.process(docs)
        result.skipped = getattr(self, "_last_skipped", 0)

        # A run that discovered videos but ingested zero chunks is a failure,
        # not a clean completion — surface it instead of reporting success.
        discovered = getattr(self, "_last_video_count", len(video_ids))
        if result.count == 0:
            result.errors.append(
                f"Discovered {discovered} video(s) but ingested 0 chunks: "
                f"{result.skipped} skipped (no transcript available or "
                "transcript fetch failed for every video — check worker logs "
                "for per-video errors)."
            )

        result.duration_seconds = time.time() - start
        logger.info(
            "%s (autonomous channel=%s, discovered=%d, skipped=%d)",
            result, channel_name_or_url, discovered, result.skipped,
        )
        return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(YouTubeIngestor().run())


if __name__ == "__main__":
    main()
