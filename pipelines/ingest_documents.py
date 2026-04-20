"""Document ingestor for PDF, TXT, and Markdown files.

Reads files from data/raw/documents/, cleans, chunks, enriches metadata
(evidence_tier_default=2), and saves to data/processed/normalized/.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from pipelines.common.cleaners import normalize_whitespace, remove_boilerplate
from pipelines.common.chunking import chunk_by_tokens
from pipelines.common.metadata_enrichment import (
    compute_content_hash,
    generate_document_id,
    infer_evidence_tier,
)
from pipelines.common.models import IngestionResult, NormalizedRecord, RawDocument
from pipelines.common.storage import save_normalized, save_to_vector_store

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".md"}
DEFAULT_EVIDENCE_TIER = 2
SOURCE_TYPE = "document"


class DocumentIngestor:
    """Ingestor for local document files (PDF, TXT, Markdown).

    Reads all supported files from raw_dir, cleans and chunks the content,
    enriches metadata, and writes normalized JSON to output_dir.
    """

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/documents"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
        max_tokens_per_chunk: int = 512,
        chunk_overlap: int = 50,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir
        self.max_tokens = max_tokens_per_chunk
        self.overlap = chunk_overlap

    def _read_file(self, path: Path) -> str:
        """Read file content, handling PDF extraction if available.

        Args:
            path: Path to the file.

        Returns:
            Raw text content of the file.
        """
        suffix = path.suffix.lower()

        if suffix == ".pdf":
            try:
                import pypdf  # type: ignore[import]
                reader = pypdf.PdfReader(str(path))
                pages = [page.extract_text() or "" for page in reader.pages]
                return "\n\n".join(pages)
            except ImportError:
                logger.warning("pypdf not installed — reading PDF as text fallback")
            except Exception as e:
                logger.error("Failed to read PDF %s: %s", path, e)
                return ""

        # TXT / MD
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.error("Failed to read file %s: %s", path, e)
            return ""

    def load_raw(self) -> list[RawDocument]:
        """Discover and load all supported files from raw_dir.

        Returns:
            List of RawDocument objects for each discovered file.
        """
        if not self.raw_dir.exists():
            logger.warning("Raw documents directory does not exist: %s", self.raw_dir)
            return []

        docs: list[RawDocument] = []
        for path in sorted(self.raw_dir.iterdir()):
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if path.name.startswith("."):
                continue

            raw_content = self._read_file(path)
            if not raw_content.strip():
                logger.warning("Empty content in file: %s", path)
                continue

            doc = RawDocument(
                source_type=SOURCE_TYPE,
                source_name=path.stem,
                raw_content=raw_content,
                acquired_at=datetime.utcnow(),
                source_url=None,
                evidence_tier_default=DEFAULT_EVIDENCE_TIER,
                file_path=str(path),
            )
            docs.append(doc)
            logger.info("Loaded document: %s (%d chars)", path.name, len(raw_content))

        return docs

    def process(self, docs: list[RawDocument]) -> IngestionResult:
        """Clean, chunk, enrich, and store all raw documents.

        Args:
            docs: List of RawDocument objects to process.

        Returns:
            IngestionResult with counts and timing.
        """
        result = IngestionResult(source_type=SOURCE_TYPE)
        records: list[NormalizedRecord] = []

        for doc in docs:
            try:
                clean_text = normalize_whitespace(remove_boilerplate(doc.raw_content))
                if not clean_text:
                    result.skipped += 1
                    continue

                chunks = chunk_by_tokens(clean_text, self.max_tokens, self.overlap)
                document_id = generate_document_id(doc.source_url, doc.acquired_at, doc.source_name)

                for idx, chunk_text in enumerate(chunks):
                    content_hash = compute_content_hash(chunk_text)
                    chunk_id = f"{document_id}_{idx:04d}"
                    record = NormalizedRecord(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        source_type=SOURCE_TYPE,
                        source_name=doc.source_name,
                        source_url=doc.source_url,
                        acquired_at=doc.acquired_at,
                        published_at=doc.published_at,
                        evidence_tier_default=DEFAULT_EVIDENCE_TIER,
                        content_hash=content_hash,
                        content=chunk_text,
                        chunk_index=idx,
                    )
                    save_normalized(record, self.output_dir)
                    records.append(record)
                    result.count += 1

            except Exception as exc:
                logger.error("Error processing document %s: %s", doc.source_name, exc)
                result.errors.append(str(exc))

        # Upsert to vector store
        if records:
            save_to_vector_store(records, chroma_persist_dir=self.chroma_persist_dir)

        return result

    def run(self) -> IngestionResult:
        """Run the full document ingestion pipeline.

        Returns:
            IngestionResult summary.
        """
        start = time.time()
        docs = self.load_raw()
        logger.info("DocumentIngestor: found %d files", len(docs))
        result = self.process(docs)
        result.duration_seconds = time.time() - start
        logger.info("%s", result)
        return result


def main() -> None:
    """Entry point for running document ingestion as a script."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = DocumentIngestor().run()
    print(result)


if __name__ == "__main__":
    main()
