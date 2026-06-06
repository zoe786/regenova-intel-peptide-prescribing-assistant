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


def _parse_pub_date(dp: str) -> "datetime | None":
    """Best-effort parse of a PubMed DP date string (e.g. '2021 Jun 15').

    Returns None if the date cannot be parsed; PubMed dates are highly
    irregular so partial/failed parses are expected and non-fatal.
    """
    if not dp:
        return None
    import re as _re
    # Try year, then year+month, then year+month+day.
    year_match = _re.match(r"(\d{4})", dp.strip())
    if not year_match:
        return None
    year = int(year_match.group(1))
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    month = 1
    day = 1
    parts = dp.split()
    if len(parts) >= 2:
        month = months.get(parts[1][:3].lower(), 1)
    if len(parts) >= 3 and parts[2].isdigit():
        day = min(int(parts[2]), 28)  # clamp to avoid invalid dates
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


class PubMedIngestor:
    """Ingestor for PubMed abstracts via NCBI Entrez.

    Supports two modes:
    - File mode (default): read PMIDs from pmids.txt.
    - Autonomous mode: given a list of peptides, build Entrez queries with the
      LLM, run esearch to discover PMIDs, then fetch + ingest, recording the
      search query as citation provenance.
    """

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/pubmed"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
        email: str = "research@regenova-intel.example.com",
        api_key: str = "",
        max_tokens_per_chunk: int = 512,
        llm_assistant: "LLMAssistant | None" = None,
        max_results_per_peptide: int = 50,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir
        self.email = email
        self.api_key = api_key
        self.max_tokens = max_tokens_per_chunk
        self.pmids_file = self.raw_dir / "pmids.txt"
        self.llm = llm_assistant
        self.max_results_per_peptide = max_results_per_peptide
        # Populated during autonomous discovery: pmid -> {"query": ..., "peptide": ...}
        self._discovery_provenance: dict[str, dict[str, str]] = {}

    def _setup_entrez(self) -> None:
        """Configure Biopython Entrez with email and API key."""
        try:
            from Bio import Entrez  # type: ignore[import]
            Entrez.email = self.email
            if self.api_key:
                Entrez.api_key = self.api_key
        except ImportError:
            logger.error("biopython not installed — PubMed ingestion unavailable")

    @property
    def _entrez_delay(self) -> float:
        """NCBI allows 10 req/s with an API key, 3 req/s without."""
        return 0.11 if self.api_key else ENTREZ_DELAY

    def discover_pmids(self, peptides: list[str]) -> list[str]:
        """Autonomously discover PMIDs for a list of peptides.

        For each peptide, build an Entrez query (LLM-assisted when available,
        deterministic otherwise), run esearch, and collect PMIDs. The query
        that found each PMID is recorded for citation provenance.

        Args:
            peptides: Peptide/compound names to search for.

        Returns:
            Deduplicated list of discovered PMIDs.
        """
        try:
            from Bio import Entrez  # type: ignore[import]
        except ImportError:
            logger.error("biopython not installed — cannot discover PMIDs")
            return []

        self._setup_entrez()
        from pipelines.common.llm_assistant import LLMAssistant
        assistant = self.llm or LLMAssistant()

        discovered: list[str] = []
        for peptide in peptides:
            decision = assistant.build_pubmed_query(peptide)
            query = decision.value
            logger.info(
                "PubMed discovery: peptide=%s used_llm=%s query=%s",
                peptide, decision.used_llm, query,
            )
            try:
                handle = Entrez.esearch(
                    db="pubmed",
                    term=query,
                    retmax=self.max_results_per_peptide,
                    sort="relevance",
                )
                record = Entrez.read(handle)
                handle.close()
                pmids = list(record.get("IdList", []))
                for pmid in pmids:
                    # First query to find a PMID wins for provenance.
                    if pmid not in self._discovery_provenance:
                        self._discovery_provenance[pmid] = {
                            "pubmed_query": query,
                            "peptide": peptide,
                        }
                        discovered.append(pmid)
                logger.info("  → %d PMIDs for %s", len(pmids), peptide)
                time.sleep(self._entrez_delay)
            except Exception as exc:
                logger.error("esearch failed for %s: %s", peptide, exc)

        logger.info("PubMed discovery complete: %d unique PMIDs", len(discovered))
        return discovered

    def _existing_document_ids(self) -> set[str]:
        """Return document_ids already present in the vector store (for dedup)."""
        try:
            from pipelines.common.chroma import CHROMA_COLLECTION_NAME, get_collection
            collection = get_collection(self.chroma_persist_dir, CHROMA_COLLECTION_NAME)
            got = collection.get(include=["metadatas"])
            return {
                m.get("document_id", "")
                for m in (got.get("metadatas") or [])
                if m.get("document_id")
            }
        except Exception as exc:
            logger.debug("Could not load existing doc ids for dedup: %s", exc)
            return set()

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

    def load_raw(self, pmids: list[str] | None = None) -> list[RawDocument]:
        """Load raw PubMed documents.

        Args:
            pmids: If provided (autonomous mode), fetch these PMIDs directly.
                Otherwise read PMIDs from pmids.txt (file mode).
        """
        if pmids is None:
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

            pmid = ab.get("pmid", "")
            url = f"{PUBMED_BASE_URL}{pmid}/" if pmid else None

            # Provenance: the autonomous search query that surfaced this PMID.
            extra = dict(self._discovery_provenance.get(pmid, {}))
            if ab.get("journal"):
                extra.setdefault("journal", ab["journal"])

            docs.append(RawDocument(
                source_type=SOURCE_TYPE,
                source_name=f"PubMed:{pmid}",
                raw_content=normalize_whitespace(content),
                acquired_at=datetime.utcnow(),
                published_at=_parse_pub_date(ab.get("pub_date", "")),
                source_url=url,
                evidence_tier_default=DEFAULT_EVIDENCE_TIER,
                extra_metadata=extra,
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
                        published_at=doc.published_at,
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

    def run_autonomous(self, peptides: list[str]) -> IngestionResult:
        """Discover and ingest PubMed abstracts for a list of peptides.

        Args:
            peptides: Peptide/compound names to search and ingest.

        Returns:
            IngestionResult summarising the run.
        """
        start = time.time()
        pmids = self.discover_pmids(peptides)
        # Dedup against what's already stored.
        existing = self._existing_document_ids()
        if existing:
            from pipelines.common.metadata_enrichment import generate_document_id
            before = len(pmids)
            pmids = [
                p for p in pmids
                if generate_document_id(f"{PUBMED_BASE_URL}{p}/", datetime.utcnow(), f"PubMed:{p}")
                not in existing
            ]
            logger.info("Dedup: %d → %d new PMIDs", before, len(pmids))
        docs = self.load_raw(pmids=pmids)
        result = self.process(docs)
        result.duration_seconds = time.time() - start
        logger.info("%s (autonomous)", result)
        return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(PubMedIngestor().run())


if __name__ == "__main__":
    main()
