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
    """Ingestor for YouTube video transcripts."""

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/youtube"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
        max_tokens_per_chunk: int = 512,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir
        self.max_tokens = max_tokens_per_chunk
        self.ids_file = self.raw_dir / "video_ids.txt"

    def _fetch_transcript(self, video_id: str) -> str | None:
        """Fetch transcript text for a YouTube video ID."""
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore[import]
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
            return " ".join(entry["text"] for entry in transcript_list)
        except Exception as exc:
            logger.error("Failed to fetch transcript for %s: %s", video_id, exc)
            return None

    def load_raw(self) -> list[RawDocument]:
        if not self.ids_file.exists():
            logger.warning("Video IDs file not found: %s", self.ids_file)
            return []

        video_ids = [
            line.strip() for line in self.ids_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        logger.info("YouTubeIngestor: found %d video IDs", len(video_ids))

        docs: list[RawDocument] = []
        for vid_id in video_ids:
            transcript = self._fetch_transcript(vid_id)
            if not transcript:
                continue
            url = f"{YOUTUBE_BASE_URL}{vid_id}"
            docs.append(RawDocument(
                source_type=SOURCE_TYPE,
                source_name=f"YouTube:{vid_id}",
                raw_content=normalize_whitespace(transcript),
                acquired_at=datetime.utcnow(),
                source_url=url,
                evidence_tier_default=DEFAULT_EVIDENCE_TIER,
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(YouTubeIngestor().run())


if __name__ == "__main__":
    main()
