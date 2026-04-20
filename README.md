# REGENOVA-Intel: Peptide Prescribing Assistant

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111+-green.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Status](https://img.shields.io/badge/status-scaffold-orange.svg)

> **⚠️ CLINICAL DISCLAIMER:** REGENOVA-Intel is a **clinical decision support tool only**. It does not provide medical advice, diagnosis, or treatment. All outputs must be reviewed by a qualified healthcare professional before clinical application. Use of this system does not create a clinician-patient relationship.

---

## Overview

REGENOVA-Intel is an evidence-tiered AI assistant for peptide prescribing decisions. It ingests multi-source knowledge (peer-reviewed literature, clinical documents, practitioner courses, community forums) and serves a retrieval-augmented generation (RAG) API with safety rules, evidence tiering, and citation integrity.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER                             │
│          Streamlit Admin UI  │  External API Consumers          │
└──────────────────┬──────────────────────┬───────────────────────┘
                   │                      │
┌──────────────────▼──────────────────────▼───────────────────────┐
│                     FastAPI API LAYER (apps/api)                │
│  /health  │  /chat  │  /ingest  │  Auth Middleware │  CORS      │
└──────┬────────────┬─────────────────────────────────────────────┘
       │            │
┌──────▼────┐  ┌────▼──────────────────────────────────────────┐
│ Safety    │  │              SERVICE LAYER                    │
│ Rules     │  │  RetrievalService → RankingService →          │
│ Engine    │  │  CitationService → AnswerComposer             │
└──────┬────┘  └────┬──────────────────────────────────────────┘
       │            │
┌──────▼────────────▼───────────────────────────────────────────┐
│                    KNOWLEDGE LAYER                             │
│  ChromaDB (vectors)  │  NetworkX Graph  │  Neo4j (optional)   │
└──────────────────────┬────────────────────────────────────────┘
                       │
┌──────────────────────▼────────────────────────────────────────┐
│                   PIPELINE LAYER (pipelines/)                  │
│  Documents │ Websites │ PubMed │ YouTube │ Skool │ Forums      │
└──────────────────────────────────────────────────────────────-─┘
```

---

## Quick Start

### 1. Bootstrap

```bash
git clone https://github.com/zoe786/regenova-intel-peptide-prescribing-assistant.git
cd regenova-intel-peptide-prescribing-assistant
bash scripts/bootstrap.sh
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your OpenAI API key and other settings
```

### 3. Run API

```bash
make run-api
# API available at http://localhost:8000
# Docs at http://localhost:8000/docs
```

### 4. Run Admin UI

```bash
make run-admin
# UI available at http://localhost:8501
```

### 5. Ingest Data

Place source files in `data/raw/` subdirectories, then:

```bash
make ingest-all
```

---

## Data Sources

| Source Type | Directory | Evidence Tier | Notes |
|-------------|-----------|---------------|-------|
| PubMed abstracts | `data/raw/pubmed/pmids.txt` | Tier 1 | High-quality peer-reviewed |
| Clinical documents | `data/raw/documents/` | Tier 2 | PDF/TXT/MD files |
| Websites | `data/raw/websites/urls.txt` | Tier 3 | URL list, fetched at ingest |
| YouTube transcripts | `data/raw/youtube/video_ids.txt` | Tier 3 | Video ID list |
| Skool courses | `data/raw/skool/courses/` | Tier 3 | Exported JSON/HTML |
| Skool community | `data/raw/skool/community/` | Tier 4 | Exported JSON |
| Forums | `data/raw/forums/` | Tier 4 | Scraped JSON |

---

## Evidence Tiers

| Tier | Label | Weight | Examples |
|------|-------|--------|---------|
| 1 | Peer-Reviewed | 1.00 | PubMed abstracts, RCTs |
| 2 | Clinical Document | 0.85 | Protocol PDFs, clinical guidelines |
| 3 | Educational Content | 0.65 | Courses, reputable websites, educational videos |
| 4 | Community/Forum | 0.40 | Practitioner forums, Skool community |
| 5 | Anecdotal | 0.15 | Unverified user reports |

---

## API Usage

### Chat endpoint

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is the evidence for BPC-157 in tendon healing?",
    "role": "clinician",
    "context_window_size": 5,
    "include_reconstitution": false
  }'
```

**Response:**
```json
{
  "answer": "Based on available evidence...",
  "recommendations": ["Consult specialist", "Review contraindications"],
  "citations": [
    {
      "source_id": "pmid_12345",
      "source_name": "PubMed",
      "evidence_tier": 1,
      "excerpt": "BPC-157 demonstrated..."
    }
  ],
  "safety_flags": [],
  "confidence": 0.72,
  "evidence_summary": "3 tier-1 sources, 2 tier-3 sources",
  "disclaimer": "This is clinical decision support only...",
  "request_id": "uuid",
  "latency_ms": 1250
}
```

---

## Admin UI

The Streamlit admin dashboard at `http://localhost:8501` provides:

- **Chat interface** — Test queries against the RAG pipeline
- **Ingest Status** — Monitor pipeline runs
- **Source Browser** — Inspect normalized chunks
- **Config Viewer** — View non-sensitive settings

Requires admin API key (set `ADMIN_API_KEY` in `.env`).

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Write tests for new functionality
4. Ensure `make test` and `make lint` pass
5. Submit a pull request with a clear description

See `docs/ARCHITECTURE.md` for system design details.

---

## Disclaimer

REGENOVA-Intel is provided for **research and clinical decision support purposes only**. It is not a substitute for professional medical advice, diagnosis, or treatment. The system's outputs are intended to assist qualified healthcare professionals and should never be used as the sole basis for clinical decisions. Always consult current clinical guidelines, regulatory guidance, and specialist expertise before prescribing peptides or related compounds.

The authors and contributors make no warranties regarding the accuracy, completeness, or fitness for purpose of any information generated by this system.
