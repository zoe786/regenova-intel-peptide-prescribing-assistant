"""PubMed abstract ingestor using Biopython Entrez.

Reads PubMed IDs from data/raw/pubmed/pmids.txt,
fetches abstracts via NCBI Entrez API, chunks (evidence_tier_default=1).
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
DEFAULT_EVIDENCE_TIER = 1
SOURCE_TYPE = "pubmed"
PUBMED_BASE_URL = "https://pubmed.ncbi.nlm.nih.gov/"
ENTREZ_BATCH_SIZE = 20
ENTREZ_DELAY = 0.34  # Respect NCBI rate limits (3 req/sec without API key)


class PubMedIngestor:
    """Ingestor for PubMed abstracts via NCBI Entrez."""

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/pubmed"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
        email: str = "research@regenova-intel.example.com",
        api_key: str = "",
        max_tokens_per_chunk: int = 512,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir
        self.email = email
        self.api_key = api_key
        self.max_tokens = max_tokens_per_chunk
        self.pmids_file = self.raw_dir / "pmids.txt"

    def _setup_entrez(self) -> None:
        """Configure Biopython Entrez with email and API key."""
        try:
            from Bio import Entrez  # type: ignore[import]
            Entrez.email = self.email
            if self.api_key:
                Entrez.api_key = self.api_key
        except ImportError:
            logger.error("biopython not installed — PubMed ingestion unavailable")

    def _fetch_abstracts(self, pmids: list[str]) -> list[dict]:
        """Fetch abstracts for a list of PubMed IDs.

        Returns list of dicts with keys: pmid, title, abstract, authors, pub_date.
        """
        try:
            from Bio import Entrez, Medline  # type: ignore[import]
        except ImportError:
            logger.error("biopython not installed")
            return []

        results: list[dict] = []
        for i in range(0, len(pmids), ENTREZ_BATCH_SIZE):
            batch = pmids[i:i + ENTREZ_BATCH_SIZE]
            try:
                handle = Entrez.efetch(
                    db="pubmed",
                    id=",".join(batch),
                    rettype="medline",
                    retmode="text",
                )
                records = list(Medline.parse(handle))
                handle.close()

                for rec in records:
                    abstract = rec.get("AB", "")
                    title = rec.get("TI", "")
                    if not abstract and not title:
                        continue
                    results.append({
                        "pmid": rec.get("PMID", ""),
                        "title": title,
                        "abstract": abstract,
                        "authors": rec.get("AU", []),
                        "pub_date": rec.get("DP", ""),
                        "journal": rec.get("TA", ""),
                    })

                logger.info("Fetched batch %d-%d (%d records)", i+1, i+len(batch), len(records))
                time.sleep(ENTREZ_DELAY)

            except Exception as exc:
                logger.error("Entrez fetch failed for batch %d: %s", i, exc)

        return results

    def load_raw(self) -> list[RawDocument]:
        if not self.pmids_file.exists():
            logger.warning("PubMed IDs file not found: %s", self.pmids_file)
            return []

        pmids = [
            line.strip() for line in self.pmids_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        logger.info("PubMedIngestor: found %d PMIDs", len(pmids))

        self._setup_entrez()
        abstracts = self._fetch_abstracts(pmids)

        docs: list[RawDocument] = []
        for ab in abstracts:
            content = f"Title: {ab['title']}\n\nAbstract: {ab['abstract']}"
            if ab.get("authors"):
                content += f"\n\nAuthors: {', '.join(ab['authors'][:5])}"
            if ab.get("journal"):
                content += f"\nJournal: {ab['journal']}"

            url = f"{PUBMED_BASE_URL}{ab['pmid']}/" if ab.get("pmid") else None
            docs.append(RawDocument(
                source_type=SOURCE_TYPE,
                source_name=f"PubMed:{ab['pmid']}",
                raw_content=normalize_whitespace(content),
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
    print(PubMedIngestor().run())


if __name__ == "__main__":
    main()
