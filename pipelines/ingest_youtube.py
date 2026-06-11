"""YouTube transcript ingestor.

Reads video IDs from data/raw/youtube/video_ids.txt,
fetches transcripts via youtube_transcript_api, and chunks them.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from pipelines.common.chunking import chunk_by_tokens
from pipelines.common.cleaners import normalize_whitespace
from pipelines.common.metadata_enrichment import compute_content_hash, generate_document_id
from pipelines.common.models import IngestionResult, NormalizedRecord, RawDocument
from pipelines.common.storage import save_normalized, save_to_vector_store

logger = logging.getLogger(__name__)

DEFAULT_EVIDENCE_TIER = 3
SOURCE_TYPE = "youtube"
YOUTUBE_BASE_URL = "https://www.youtube.com/watch?v="


class YouTubeIngestor:
    """Ingestor for YouTube video transcripts.

    Supports two modes:
    - File mode (default): read video IDs from video_ids.txt.
    - Autonomous mode: given a channel name, enumerate every uploaded video
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

    # ── Autonomous discovery via YouTube Data API ─────────────────────────

    def discover_channel_videos(self, channel_name: str, topic: str = "") -> list[str]:
        """Enumerate every uploaded video for a channel name.

        Resolves the channel name to its uploads playlist, paginates all
        items, captures channel/title provenance, and (when an LLM is
        available and a topic is given) drops clearly off-topic videos.

        Args:
            channel_name: Channel display name to resolve.
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

        try:
            youtube = build("youtube", "v3", developerKey=self.youtube_api_key)

            # 1. Resolve channel name → channel id + uploads playlist.
            search = youtube.search().list(
                q=channel_name, type="channel", part="snippet", maxResults=1
            ).execute()
            items = search.get("items", [])
            if not items:
                logger.warning("No channel found for '%s'", channel_name)
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
            logger.error("Channel discovery failed for '%s': %s", channel_name, exc)
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

    def _fetch_transcript(self, video_id: str) -> str | None:
        """Fetch transcript text for a YouTube video ID.

        Supports both the legacy static API (``get_transcript``, pre-1.0) and
        the current instance API (``YouTubeTranscriptApi().fetch(...)``, 1.0+).
        """
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore[import]
        except ImportError as exc:
            logger.error("youtube_transcript_api not installed: %s", exc)
            return None

        try:
            if hasattr(YouTubeTranscriptApi, "get_transcript"):
                # Legacy (<1.0) static API.
                raw = YouTubeTranscriptApi.get_transcript(video_id)
            else:
                # Current (>=1.0) instance API; .fetch() returns a
                # FetchedTranscript, normalised back to list-of-dicts.
                # Optionally route through a proxy to bypass YouTube's
                # datacenter-IP block. Two mechanisms, in priority order:
                #   1. Webshare rotating residential proxies, configured via
                #      WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD. This
                #      is the production-grade path: the library rotates IPs
                #      and retries on block automatically.
                #   2. A single generic proxy URL via YT_TRANSCRIPT_PROXY
                #      (the legacy SSH-tunnel demo workaround).
                import os
                proxy_config = None
                ws_user = os.environ.get("WEBSHARE_PROXY_USERNAME", "").strip()
                ws_pass = os.environ.get("WEBSHARE_PROXY_PASSWORD", "").strip()
                if ws_user and ws_pass:
                    from youtube_transcript_api.proxies import WebshareProxyConfig
                    # Optional comma-separated ISO country codes (e.g. "us,gb")
                    # to constrain the residential IP pool.
                    locations_raw = os.environ.get(
                        "WEBSHARE_PROXY_LOCATIONS", ""
                    ).strip()
                    filter_locations = (
                        [c.strip().lower() for c in locations_raw.split(",") if c.strip()]
                        or None
                    )
                    retries = int(os.environ.get("WEBSHARE_PROXY_RETRIES", "10"))
                    proxy_config = WebshareProxyConfig(
                        proxy_username=ws_user,
                        proxy_password=ws_pass,
                        filter_ip_locations=filter_locations,
                        retries_when_blocked=retries,
                    )
                else:
                    proxy_url = os.environ.get("YT_TRANSCRIPT_PROXY", "").strip()
                    if proxy_url:
                        from youtube_transcript_api.proxies import GenericProxyConfig
                        proxy_config = GenericProxyConfig(
                            http_url=proxy_url, https_url=proxy_url
                        )

                if proxy_config is not None:
                    api = YouTubeTranscriptApi(proxy_config=proxy_config)
                else:
                    api = YouTubeTranscriptApi()
                raw = api.fetch(video_id).to_raw_data()
            return " ".join(entry["text"] for entry in raw)
        except Exception as exc:
            logger.error("Failed to fetch transcript for %s: %s", video_id, exc)
            return None

    def load_raw(self, video_ids: list[str] | None = None) -> list[RawDocument]:
        if video_ids is None:
            if not self.ids_file.exists():
                logger.warning("Video IDs file not found: %s", self.ids_file)
                return []
            video_ids = [
                line.strip() for line in self.ids_file.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            ]
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

    def run_autonomous(self, channel_name: str, topic: str = "") -> IngestionResult:
        """Discover and ingest every video from a channel.

        Args:
            channel_name: Channel display name (as pasted by the user).
            topic: Optional topic for relevance triage.

        Returns:
            IngestionResult summarising the run.
        """
        start = time.time()
        video_ids = self.discover_channel_videos(channel_name, topic=topic)
        if not video_ids:
            result = IngestionResult(source_type=SOURCE_TYPE)
            result.errors.append(
                f"Channel discovery returned no videos for '{channel_name}' "
                "(resolution failed, channel empty, or discovery crashed — "
                "check worker logs)."
            )
            result.duration_seconds = time.time() - start
            logger.warning("%s (autonomous channel=%s)", result, channel_name)
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
            result, channel_name, discovered, result.skipped,
        )
        return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(YouTubeIngestor().run())


if __name__ == "__main__":
    main()
