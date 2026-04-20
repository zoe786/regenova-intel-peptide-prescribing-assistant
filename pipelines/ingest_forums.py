"""Forum thread ingestor.

Reads scraped JSON from data/raw/forums/, parses threads and replies,
chunks (evidence_tier_default=4).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from pipelines.common.chunking import chunk_by_paragraph
from pipelines.common.cleaners import normalize_whitespace
from pipelines.common.metadata_enrichment import compute_content_hash, generate_document_id
from pipelines.common.models import IngestionResult, NormalizedRecord, RawDocument
from pipelines.common.storage import save_normalized, save_to_vector_store

logger = logging.getLogger(__name__)
DEFAULT_EVIDENCE_TIER = 4
SOURCE_TYPE = "forum"


class ForumIngestor:
    """Ingestor for scraped forum threads."""

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/forums"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir

    def load_raw(self) -> list[RawDocument]:
        if not self.raw_dir.exists():
            logger.warning("Forums directory not found: %s", self.raw_dir)
            return []

        docs: list[RawDocument] = []
        for json_file in sorted(self.raw_dir.glob("*.json")):
            try:
                threads = json.loads(json_file.read_text(encoding="utf-8"))
                if not isinstance(threads, list):
                    threads = [threads]
                for thread in threads:
                    title = thread.get("title", "Forum Thread")
                    posts = thread.get("posts", [])
                    content_parts = [f"Thread: {title}"]
                    for post in posts:
                        author = post.get("author", "Unknown")
                        text = post.get("content", "")
                        if text:
                            content_parts.append(f"[{author}]: {text}")

                    full_content = "\n\n".join(content_parts)
                    if not full_content.strip():
                        continue

                    docs.append(RawDocument(
                        source_type=SOURCE_TYPE,
                        source_name=json_file.stem,
                        raw_content=normalize_whitespace(full_content),
                        acquired_at=datetime.utcnow(),
                        evidence_tier_default=DEFAULT_EVIDENCE_TIER,
                    ))
            except Exception as e:
                logger.error("Error reading %s: %s", json_file, e)

        return docs

    def process(self, docs: list[RawDocument]) -> IngestionResult:
        result = IngestionResult(source_type=SOURCE_TYPE)
        records: list[NormalizedRecord] = []

        for doc in docs:
            try:
                chunks = chunk_by_paragraph(doc.raw_content, max_paragraphs=4)
                document_id = generate_document_id(doc.source_url, doc.acquired_at, doc.source_name)
                for idx, chunk_text in enumerate(chunks):
                    record = NormalizedRecord(
                        chunk_id=f"{document_id}_{idx:04d}",
                        document_id=document_id,
                        source_type=SOURCE_TYPE,
                        source_name=doc.source_name,
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
    print(ForumIngestor().run())


if __name__ == "__main__":
    main()
