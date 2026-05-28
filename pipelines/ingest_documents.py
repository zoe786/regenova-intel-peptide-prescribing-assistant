"""Document ingestor for PDF, TXT, and Markdown files.

Reads files from data/raw/documents/, cleans, chunks, enriches metadata
(evidence_tier_default=2), and saves to data/processed/normalized/.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from pipelines.common.chroma import CHROMA_COLLECTION_NAME, get_collection
from pipelines.common.chunking import chunk_by_tokens
from pipelines.common.cleaners import normalize_whitespace, remove_boilerplate
from pipelines.common.metadata_enrichment import (
    compute_content_hash,
    generate_document_id,
)
from pipelines.common.models import IngestionResult, NormalizedRecord, RawDocument
from pipelines.common.pdf_processing import chunk_pdf_pages_layout, extract_pdf_content
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

    def _read_file(self, path: Path) -> tuple[str, dict]:
        """Read file content and return text with optional extraction diagnostics."""
        suffix = path.suffix.lower()

        if suffix == ".pdf":
            extraction = extract_pdf_content(str(path), ocr_fallback=True)
            quality = extraction["quality"]
            metadata = {
                "extraction_method": extraction["extraction_method"],
                "extraction_quality_status": quality["quality_status"],
                "extraction_warnings_json": extraction["warnings"],
                "pdf_page_count": extraction["page_count"],
                "pdf_alpha_ratio": quality["alpha_ratio"],
                "pdf_weird_char_ratio": quality["weird_char_ratio"],
                "pdf_chars_per_page": quality["chars_per_page"],
                "ocr_attempted": extraction["ocr_attempted"],
                "ocr_used": extraction["ocr_used"],
                "ocr_available": extraction["ocr_available"],
                "extraction_preview_raw": extraction["raw_preview"],
                "extraction_preview_clean": extraction["clean_preview"],
                "_pdf_pages_cleaned": extraction["cleaned_pages"],
            }
            return extraction["cleaned_text"], metadata

        # TXT / MD
        try:
            return path.read_text(encoding="utf-8", errors="replace"), {}
        except Exception as e:
            logger.error("Failed to read file %s: %s", path, e)
            return "", {}

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

            raw_content, extraction_meta = self._read_file(path)
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
                file_path=str(path.relative_to(self.raw_dir)),
                extra_metadata=extraction_meta,
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
        replaced_document_ids: set[str] = set()

        for doc in docs:
            try:
                is_pdf = bool(doc.file_path and Path(doc.file_path).suffix.lower() == ".pdf")
                extraction_status = str(doc.extra_metadata.get("extraction_quality_status", "ok"))
                document_id = generate_document_id(
                    doc.source_url,
                    doc.acquired_at,
                    doc.source_name,
                    file_path=doc.file_path,
                )

                if document_id not in replaced_document_ids:
                    self._delete_existing_document_chunks(document_id)
                    replaced_document_ids.add(document_id)

                if is_pdf and extraction_status == "rejected":
                    result.skipped += 1
                    quarantine_record = {
                        "document_id": document_id,
                        "source_name": doc.source_name,
                        "file_path": doc.file_path,
                        "extraction_method": doc.extra_metadata.get("extraction_method", "unknown"),
                        "warnings": doc.extra_metadata.get("extraction_warnings_json", []),
                    }
                    result.quarantined_documents.append(quarantine_record)
                    result.errors.append(
                        f"Quarantined low-quality PDF extraction for {doc.source_name}: "
                        f"{doc.extra_metadata.get('extraction_warnings_json', [])}"
                    )
                    continue

                if is_pdf:
                    cleaned_pages = doc.extra_metadata.get("_pdf_pages_cleaned") or []
                    chunks = chunk_pdf_pages_layout(
                        cleaned_pages,
                        max_tokens=self.max_tokens,
                        overlap=self.overlap,
                    )
                    clean_text = doc.raw_content.strip()
                else:
                    clean_text = normalize_whitespace(remove_boilerplate(doc.raw_content))
                    chunks = chunk_by_tokens(clean_text, self.max_tokens, self.overlap)

                if not clean_text:
                    result.skipped += 1
                    continue

                if not chunks:
                    result.skipped += 1
                    continue

                for idx, chunk_text in enumerate(chunks):
                    content_hash = compute_content_hash(chunk_text)
                    chunk_id = f"{document_id}_{idx:04d}"
                    chunk_extra_metadata = {
                        key: value
                        for key, value in doc.extra_metadata.items()
                        if not key.startswith("_")
                    }
                    if idx == 0 and clean_text:
                        chunk_extra_metadata["document_clean_preview"] = clean_text[:1200]
                    chunk_extra_metadata["chunking_strategy"] = (
                        "pdf_layout" if is_pdf else "token"
                    )
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
                        extra_metadata=chunk_extra_metadata,
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

    def _delete_existing_document_chunks(self, document_id: str) -> None:
        """Remove existing chunks for a document to avoid stale data on re-ingest."""
        try:
            collection = get_collection(
                chroma_persist_dir=self.chroma_persist_dir,
                collection_name=CHROMA_COLLECTION_NAME,
            )
            existing = collection.get(where={"document_id": document_id}, include=["metadatas"])
            existing_chunk_ids = existing.get("ids") or []
            if existing_chunk_ids:
                collection.delete(ids=existing_chunk_ids)
                logger.info(
                    "Deleted %d existing vector chunks for document_id=%s",
                    len(existing_chunk_ids),
                    document_id,
                )
        except Exception as exc:
            logger.warning("Could not delete existing vector chunks for %s: %s", document_id, exc)
            existing_chunk_ids = []

        deleted_files = 0
        candidate_paths = {self.output_dir / f"{chunk_id}.json" for chunk_id in existing_chunk_ids}
        candidate_paths.update(self.output_dir.glob(f"{document_id}_*.json"))
        for normalized_path in candidate_paths:
            if normalized_path.exists():
                normalized_path.unlink()
                deleted_files += 1
        if deleted_files:
            logger.info(
                "Deleted %d normalized chunk files for document_id=%s",
                deleted_files,
                document_id,
            )

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
