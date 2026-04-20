"""Website ingestor — fetches URLs and extracts text content.

Reads URL list from data/raw/websites/urls.txt, fetches with httpx,
parses with BeautifulSoup, cleans, and chunks (evidence_tier_default=3).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from pipelines.common.cleaners import clean_html, normalize_whitespace, remove_boilerplate
from pipelines.common.chunking import chunk_by_tokens
from pipelines.common.metadata_enrichment import compute_content_hash, generate_document_id
from pipelines.common.models import IngestionResult, NormalizedRecord, RawDocument
from pipelines.common.storage import save_normalized, save_to_vector_store

logger = logging.getLogger(__name__)

DEFAULT_EVIDENCE_TIER = 3
SOURCE_TYPE = "website"
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 1.0  # polite crawl delay in seconds


class WebsiteIngestor:
    """Ingestor for web pages specified in a URL list file."""

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/websites"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
        max_tokens_per_chunk: int = 512,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir
        self.max_tokens = max_tokens_per_chunk
        self.urls_file = self.raw_dir / "urls.txt"

    def _fetch_url(self, url: str) -> str | None:
        """Fetch a URL and return the HTML content."""
        try:
            import httpx  # type: ignore[import]
            with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
                response = client.get(url, headers={"User-Agent": "REGENOVA-Intel/0.1 (research bot)"})
                response.raise_for_status()
                return response.text
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", url, exc)
            return None

    def load_raw(self) -> list[RawDocument]:
        """Read URL list and fetch each page."""
        if not self.urls_file.exists():
            logger.warning("URL list file not found: %s", self.urls_file)
            return []

        urls = [
            line.strip() for line in self.urls_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        logger.info("WebsiteIngestor: found %d URLs", len(urls))

        docs: list[RawDocument] = []
        for url in urls:
            html = self._fetch_url(url)
            if not html:
                continue
            text = clean_html(html)
            if not text.strip():
                continue
            docs.append(RawDocument(
                source_type=SOURCE_TYPE,
                source_name=url.split("/")[2],  # domain as name
                raw_content=text,
                acquired_at=datetime.utcnow(),
                source_url=url,
                evidence_tier_default=DEFAULT_EVIDENCE_TIER,
            ))
            time.sleep(REQUEST_DELAY)

        return docs

    def process(self, docs: list[RawDocument]) -> IngestionResult:
        """Clean, chunk, and store website content."""
        result = IngestionResult(source_type=SOURCE_TYPE)
        records: list[NormalizedRecord] = []

        for doc in docs:
            try:
                clean_text = normalize_whitespace(remove_boilerplate(doc.raw_content))
                if not clean_text:
                    result.skipped += 1
                    continue

                chunks = chunk_by_tokens(clean_text, self.max_tokens)
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
                logger.error("Error processing %s: %s", doc.source_url, exc)
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
    print(WebsiteIngestor().run())


if __name__ == "__main__":
    main()
