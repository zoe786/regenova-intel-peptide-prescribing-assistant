# Data Ingestion Playbook

This playbook provides step-by-step instructions for ingesting all source types into REGENOVA-Intel.

---

## Prerequisites

```bash
# Activate virtualenv and confirm settings
source .venv/bin/activate
cp .env.example .env   # if not already done
# Edit .env: set OPENAI_API_KEY, confirm CHROMA_PERSIST_DIR
python scripts/init_db.py   # initialise ChromaDB collection
```

---

## 1. Source File Placement

### PubMed (Tier 1)
```
data/raw/pubmed/pmids.txt
```
One PubMed ID per line, e.g.:
```
12345678
23456789
```
Requires: `PUBMED_EMAIL` and optionally `PUBMED_API_KEY` in `.env`.

### Clinical Documents (Tier 2)
```
data/raw/documents/
  ├── peptide_protocol_v2.pdf
  ├── bpc157_clinical_overview.txt
  └── tb500_review.md
```
Supported formats: `.pdf`, `.txt`, `.md`

### Websites (Tier 3)
```
data/raw/websites/urls.txt
```
One URL per line:
```
https://www.ncbi.nlm.nih.gov/pmc/articles/PMCXXXXXX/
https://peptidesciences.com/bpc-157/
```

### YouTube Transcripts (Tier 3)
```
data/raw/youtube/video_ids.txt
```
One YouTube video ID per line:
```
dQw4w9WgXcQ
abc123defgh
```

### Skool Courses (Tier 3)
```
data/raw/skool/courses/
  ├── course_peptide_fundamentals.json
  └── module_advanced_protocols.html
```
Export from Skool dashboard. See TODO for API integration.

### Skool Community (Tier 4)
```
data/raw/skool/community/
  └── community_export_2024_01.json
```
Export format: array of `{post_id, author, content, replies: [...], created_at}`.

### Forums (Tier 4)
```
data/raw/forums/
  └── peptide_forum_threads.json
```
Scraped JSON format: array of `{thread_id, title, posts: [{author, content, timestamp}]}`.

---

## 2. Running Individual Ingestors

Each ingestor can be run independently:

```bash
# PubMed
python pipelines/ingest_pubmed.py

# Clinical documents
python pipelines/ingest_documents.py

# Websites
python pipelines/ingest_websites.py

# YouTube
python pipelines/ingest_youtube.py

# Skool courses
python pipelines/ingest_skool_courses.py

# Skool community
python pipelines/ingest_skool_community.py

# Forums
python pipelines/ingest_forums.py
```

Each ingestor outputs normalized JSON to `data/processed/normalized/` and logs progress.

---

## 3. Running Unified Ingestion

To run all ingestors in sequence with a summary report:

```bash
make ingest-all
# or directly:
python pipelines/run_all_ingestion.py
```

Output:
```
╔══════════════════════════════════════════════════════╗
║          REGENOVA-Intel Ingestion Summary            ║
╠══════════════════════════════════════════════════════╣
║  PubMed          │ 45 docs  │  2.3s  │  ✓ OK        ║
║  Documents       │  8 docs  │  1.1s  │  ✓ OK        ║
║  Websites        │ 12 docs  │  8.7s  │  ✓ OK        ║
║  YouTube         │  6 docs  │  4.2s  │  ✓ OK        ║
║  Skool Courses   │  0 docs  │  0.1s  │  ⚠ No files  ║
║  Skool Community │  0 docs  │  0.1s  │  ⚠ No files  ║
║  Forums          │  0 docs  │  0.1s  │  ⚠ No files  ║
╚══════════════════════════════════════════════════════╝
Total: 71 documents | 18.6s
```

---

## 4. Running Knowledge Extraction

After ingestion, extract structured claims and triples:

```bash
# Extract claims from normalized chunks
python knowledge/extraction/claim_extractor.py

# Extract subject-relation-object triples
python knowledge/extraction/triple_extractor.py

# Link entities to canonical names
python knowledge/extraction/entity_linker.py

# Build knowledge graph
python knowledge/graph/graph_builder.py
```

Or all at once:
```bash
python -c "
from knowledge.extraction.claim_extractor import ClaimExtractor
from knowledge.extraction.triple_extractor import TripleExtractor
from knowledge.extraction.entity_linker import EntityLinker
from knowledge.graph.graph_builder import GraphBuilder

ClaimExtractor().run()
TripleExtractor().run()
EntityLinker().run()
GraphBuilder().run()
"
```

---

## 5. Reindexing

To re-embed all normalized chunks and refresh the vector store:

```bash
make reindex
# or:
python scripts/reindex.py
```

This:
1. Loads all JSON files from `data/processed/normalized/`
2. Re-generates embeddings (calls OpenAI API)
3. Upserts to ChromaDB (deduplicates by chunk_id)

**Warning:** Full reindex can be slow and costly if many chunks. Partial reindex by source type is planned (TODO).

---

## 6. Validating via API and Admin UI

### API validation

```bash
# Health check
curl http://localhost:8000/health

# Readiness (checks vector store)
curl http://localhost:8000/health/ready

# Sample chat query
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "What is BPC-157?", "role": "clinician"}'
```

### Smoke test

```bash
make smoke-test
# or:
python scripts/smoke_test.py
```

### Admin UI validation

1. Start: `make run-admin`
2. Navigate to `http://localhost:8501`
3. Go to **Source Browser** → confirm chunk count is non-zero
4. Go to **Chat** → test a query
5. Go to **Ingest Status** → confirm last run timestamp

---

## 7. Adding a New Source Type

### Checklist

- [ ] Create `data/raw/<source_type>/` directory and add `.gitkeep`
- [ ] Add to `.gitignore` data exclusion rules
- [ ] Create `pipelines/ingest_<source_type>.py`:
  - [ ] Define `<SourceType>Ingestor` class
  - [ ] Implement `__init__`, `load_raw()`, `process()`, `run()` methods
  - [ ] Set appropriate `evidence_tier_default` (see `docs/SOURCE_TIERING.md`)
  - [ ] Add `main()` function
- [ ] Add to `pipelines/run_all_ingestion.py` ingestor list
- [ ] Update `docs/DATA_INGESTION_PLAYBOOK.md` with placement instructions
- [ ] Add pytest test for new ingestor (at minimum: test it doesn't crash on empty input)
- [ ] Add Makefile target if useful
- [ ] Update `README.md` Data Sources table

### Template

```python
"""Ingestor for <source_type> data."""
import logging
from pathlib import Path
from pipelines.common.models import RawDocument, IngestionResult
from pipelines.common.cleaners import clean_html, normalize_whitespace
from pipelines.common.chunking import chunk_by_tokens
from pipelines.common.metadata_enrichment import enrich_metadata
from pipelines.common.storage import save_normalized, save_to_vector_store

logger = logging.getLogger(__name__)

class NewSourceIngestor:
    """Ingestor for <describe source>."""
    
    EVIDENCE_TIER_DEFAULT = 3  # adjust per SOURCE_TIERING.md
    SOURCE_TYPE = "<source_type>"
    
    def __init__(self, raw_dir: Path, output_dir: Path):
        self.raw_dir = raw_dir
        self.output_dir = output_dir
    
    def load_raw(self) -> list[RawDocument]:
        """Load raw documents from source directory."""
        # TODO: implement
        return []
    
    def process(self, docs: list[RawDocument]) -> IngestionResult:
        """Clean, chunk, enrich, and store documents."""
        # TODO: implement
        return IngestionResult(source_type=self.SOURCE_TYPE, count=0)
    
    def run(self) -> IngestionResult:
        docs = self.load_raw()
        return self.process(docs)

def main():
    result = NewSourceIngestor(
        raw_dir=Path("data/raw/<source_type>"),
        output_dir=Path("data/processed/normalized")
    ).run()
    print(f"Ingested {result.count} documents from {result.source_type}")

if __name__ == "__main__":
    main()
```
