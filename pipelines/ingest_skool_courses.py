"""Skool course ingestor.

Reads exported JSON/HTML from data/raw/skool/courses/,
parses course modules and lessons, chunks (evidence_tier_default=3).

TODO: Implement Skool API integration when API credentials are available.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from pipelines.common.chunking import chunk_by_tokens
from pipelines.common.cleaners import clean_html, normalize_whitespace
from pipelines.common.metadata_enrichment import compute_content_hash, generate_document_id
from pipelines.common.models import IngestionResult, NormalizedRecord, RawDocument
from pipelines.common.storage import save_normalized, save_to_vector_store

logger = logging.getLogger(__name__)
DEFAULT_EVIDENCE_TIER = 3
SOURCE_TYPE = "skool_course"


class SkoolCourseIngestor:
    """Ingestor for Skool course exports (JSON or HTML).

    TODO: Add Skool API integration:
    - Authenticate via Skool API key
    - Fetch course list and module content programmatically
    - Replace file-based export with live API ingestion
    """

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/skool/courses"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
        max_tokens_per_chunk: int = 512,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir
        self.max_tokens = max_tokens_per_chunk

    def _parse_json_export(self, path: Path) -> list[RawDocument]:
        """Parse a Skool JSON export file into RawDocuments."""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = [data]

        docs: list[RawDocument] = []
        for item in data:
            course_name = item.get("course_name", path.stem)
            modules = item.get("modules", [item])
            for module in modules:
                lessons = module.get("lessons", [module])
                for lesson in lessons:
                    content = lesson.get("content", lesson.get("text", ""))
                    if not content:
                        continue
                    docs.append(RawDocument(
                        source_type=SOURCE_TYPE,
                        source_name=f"{course_name}: {lesson.get('title', 'Lesson')}",
                        raw_content=normalize_whitespace(content),
                        acquired_at=datetime.utcnow(),
                        evidence_tier_default=DEFAULT_EVIDENCE_TIER,
                    ))
        return docs

    def _parse_html_export(self, path: Path) -> list[RawDocument]:
        """Parse a Skool HTML export file into RawDocuments."""
        html = path.read_text(encoding="utf-8", errors="replace")
        text = clean_html(html)
        if not text.strip():
            return []
        return [RawDocument(
            source_type=SOURCE_TYPE,
            source_name=path.stem,
            raw_content=normalize_whitespace(text),
            acquired_at=datetime.utcnow(),
            evidence_tier_default=DEFAULT_EVIDENCE_TIER,
        )]

    def load_raw(self) -> list[RawDocument]:
        if not self.raw_dir.exists():
            logger.warning("Skool courses directory not found: %s", self.raw_dir)
            return []

        docs: list[RawDocument] = []
        for path in sorted(self.raw_dir.iterdir()):
            if path.suffix.lower() == ".json":
                try:
                    docs.extend(self._parse_json_export(path))
                except Exception as e:
                    logger.error("Error parsing %s: %s", path, e)
            elif path.suffix.lower() in {".html", ".htm"}:
                try:
                    docs.extend(self._parse_html_export(path))
                except Exception as e:
                    logger.error("Error parsing %s: %s", path, e)

        logger.info("SkoolCourseIngestor: loaded %d documents", len(docs))
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
    print(SkoolCourseIngestor().run())


if __name__ == "__main__":
    main()
