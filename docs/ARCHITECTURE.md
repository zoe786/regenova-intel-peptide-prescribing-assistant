# REGENOVA-Intel: Architecture Documentation

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          CLIENT LAYER                                   │
│                                                                         │
│   ┌──────────────────┐          ┌─────────────────────────────────┐    │
│   │  Streamlit       │          │  External REST Clients          │    │
│   │  Admin UI        │          │  (curl, SDK, web apps)          │    │
│   │  :8501           │          │                                 │    │
│   └────────┬─────────┘          └──────────────┬──────────────────┘    │
└────────────┼──────────────────────────────────-┼────────────────────── ┘
             │ HTTP                               │ HTTP
┌────────────▼────────────────────────────────────▼────────────────────── ┐
│                         API LAYER  (apps/api/)                          │
│                                                                         │
│  ┌──────────┐  ┌───────────┐  ┌────────────┐  ┌────────────────────┐  │
│  │ /health  │  │   /chat   │  │  /ingest   │  │  Auth Middleware   │  │
│  │  router  │  │  router   │  │  router    │  │  CORS Middleware   │  │
│  └──────────┘  └─────┬─────┘  └─────┬──────┘  └────────────────────┘  │
│                      │              │                                   │
│  ┌───────────────────▼──────────────▼──────────────────────────────┐   │
│  │                     SERVICE LAYER                               │   │
│  │                                                                  │   │
│  │  RetrievalService → RankingService → CitationService →          │   │
│  │  SafetyRuleEngine  →  AnswerComposer                            │   │
│  └───────────────────┬──────────────────────────────────────────── ┘   │
└──────────────────────┼─────────────────────────────────────────────────┘
                       │
┌──────────────────────▼─────────────────────────────────────────────────┐
│                       KNOWLEDGE LAYER                                  │
│                                                                         │
│  ┌─────────────────┐  ┌───────────────────┐  ┌──────────────────────┐ │
│  │  ChromaDB        │  │  NetworkX Graph   │  │  Neo4j (optional)   │ │
│  │  Vector Store    │  │  (in-process)     │  │  bolt://7687        │ │
│  │  :8001           │  │  graph.pkl        │  │                     │ │
│  └─────────────────┘  └───────────────────┘  └──────────────────────┘ │
└──────────────────────────────┬──────────────────────────────────────── ┘
                               │
┌──────────────────────────────▼──────────────────────────────────────── ┐
│                      PIPELINE LAYER  (pipelines/)                      │
│                                                                         │
│  ingest_documents  │  ingest_pubmed  │  ingest_websites               │
│  ingest_youtube    │  ingest_forums  │  ingest_skool_*                 │
│                                                                         │
│  → common/cleaners → common/chunking → common/metadata_enrichment     │
│  → common/storage (ChromaDB + filesystem)                              │
└──────────────────────────────────────────────────────────────────────── ┘
```

---

## 2. Component Breakdown

### 2.1 API Layer (`apps/api/`)

**Responsibility:** Expose HTTP endpoints, enforce authentication, route requests to services, handle errors uniformly.

| Component | Path | Responsibility |
|-----------|------|----------------|
| `main.py` | `apps/api/main.py` | FastAPI app factory, lifespan, middleware |
| `config.py` | `apps/api/config.py` | Pydantic Settings, env-var binding |
| `routers/health.py` | | Liveness/readiness probes |
| `routers/chat.py` | | Chat query endpoint, orchestrates services |
| `routers/ingest.py` | | Admin-only pipeline trigger endpoints |

**Key middleware:**
- `X-Decision-Support-Only: true` header on all responses
- CORS with configurable origins
- Global RFC 7807 problem-detail error handler

### 2.2 Service Layer (`apps/api/services/`)

**Responsibility:** Implement the RAG query pipeline as composable services.

```
Query
  └─> RetrievalService.retrieve(query, top_k)
        └─> vector similarity search (ChromaDB)
        └─> [optional] graph traversal (NetworkX/Neo4j)
  └─> RankingService.rank(chunks, query)
        └─> evidence_tier_weight × relevance_score × recency_boost
  └─> SafetyRuleEngine.evaluate(query, patient_case, chunks)
        └─> pregnancy_breastfeeding_caution()
        └─> cancer_history_caution()
        └─> missing_baseline_labs_warning()
        └─> polypharmacy_interaction_caution()
  └─> CitationService.attach_citations(chunks, answer)
        └─> [1], [2] marker injection
        └─> deduplication by source_id
  └─> AnswerComposer.compose(...)
        └─> LLM call (OpenAI/langchain)
        └─> safety guardrail injection if critical flags
```

### 2.3 Pipeline Layer (`pipelines/`)

**Responsibility:** Transform raw source materials into normalized, chunked, embedded documents.

Each ingestor follows the same interface:
```
RawSource → clean → chunk → enrich_metadata → save_normalized → embed → upsert_vector_store
```

### 2.4 Knowledge Layer (`knowledge/`)

**Responsibility:** Extract structured knowledge from chunks and build a queryable graph.

```
NormalizedChunks
  └─> ClaimExtractor (LLM) → claim records (JSON)
  └─> TripleExtractor (LLM) → subject-relation-object triples (JSON)
  └─> EntityLinker → canonical entity resolution
  └─> GraphBuilder → networkx DiGraph → graph.pkl + graph_edges.jsonl
  └─> GraphQuery → find_related_peptides(), find_contraindications(), ...
```

### 2.5 Vector Store

**Primary:** ChromaDB (local persistent, Docker-optional)
**Collection:** `regenova_intel_chunks`
**Embedding model:** OpenAI `text-embedding-3-small` (TODO: configurable)

**TODO:** Abstract via `VectorStoreBackend` protocol supporting Weaviate and Pinecone.

### 2.6 Graph Store

**Primary:** NetworkX DiGraph (in-process, loaded from `data/processed/graph.pkl`)
**Optional production:** Neo4j 5 via bolt protocol

Node types: `Peptide`, `Condition`, `Drug`, `Mechanism`, `Study`
Edge types: `TREATS`, `CONTRAINDICATED_WITH`, `INTERACTS_WITH`, `STUDIED_IN`, `DOSAGE_FOR`

### 2.7 Prompt Layer (`prompts/`)

- `extraction/claims.txt` — structured claim extraction
- `extraction/triples.txt` — KG triple extraction
- `generation/clinician_answer.txt` — RAG answer generation
- `generation/safety_guardrails.txt` — appended when critical flags present

### 2.8 Safety Engine

Rules fire against: query text, patient case (if provided), and retrieved chunk content.
All fired rules produce `SafetyFlag` objects with `severity: info | warning | critical`.
Critical flags trigger safety_guardrails prompt injection and escalate disclaimer language.

### 2.9 Admin UI (`apps/admin/`)

Streamlit single-page app with navigation sidebar. Pages:
- **Chat**: Direct RAG query interface
- **Ingest Status**: Pipeline run monitoring
- **Source Browser**: Inspect normalized data
- **Config**: Non-sensitive settings display

---

## 3. Data Flow Narrative

### Ingestion Flow

1. Operator places source files in `data/raw/<type>/`
2. `make ingest-all` triggers `pipelines/run_all_ingestion.py`
3. Each ingestor reads, cleans, chunks, and enriches metadata
4. Normalized JSON saved to `data/processed/normalized/`
5. `common/storage.save_to_vector_store()` embeds chunks and upserts to ChromaDB
6. Optionally: `knowledge/extraction/` pipeline extracts claims and triples for graph

### Query Flow

1. Client sends `POST /chat` with `ChatRequest`
2. Auth middleware validates JWT/API key
3. `RetrievalService.retrieve()` performs vector similarity search
4. `RankingService.rank()` applies evidence-tier weighting
5. `SafetyRuleEngine.evaluate()` fires rules against query + patient context
6. `CitationService.attach_citations()` injects [N] markers
7. `AnswerComposer.compose()` calls LLM with assembled context
8. `ChatResponse` returned with answer, citations, safety_flags, confidence

---

## 4. Technology Choices

| Technology | Choice | Rationale |
|-----------|--------|-----------|
| API Framework | FastAPI | Async-native, Pydantic integration, auto-docs |
| LLM | OpenAI GPT-4o | Best-in-class reasoning, function calling |
| LLM Orchestration | LangChain | Prompt templating, chain composition |
| Vector Store | ChromaDB | Zero-config local dev, Docker for prod |
| Graph | NetworkX + Neo4j | Fast local traversal, scale to Neo4j |
| Settings | pydantic-settings | Type-safe env-var binding |
| Logging | loguru | Structured, contextual, readable |
| Testing | pytest + pytest-asyncio | Standard, async-compatible |
| Admin UI | Streamlit | Rapid prototyping, Python-native |

---

## 5. Extensibility Notes

- **New LLM provider**: Swap `langchain-openai` for `langchain-anthropic` etc., update `AnswerComposer`
- **New vector store**: Implement `VectorStoreBackend` protocol, update `VECTOR_DB_BACKEND` env var
- **New ingestor**: Create `pipelines/ingest_<source>.py` following the existing pattern
- **New safety rule**: Add method to `SafetyRuleEngine`, register in `evaluate()`
- **New evidence tier**: Update `TIER_WEIGHTS` in `knowledge/scoring/evidence_tiering.py`

---

## 6. Deployment Topology

```
Internet
   │
   ▼
[Reverse Proxy: nginx/Caddy]  ← TLS termination
   │
   ├─> :8000  [FastAPI API]
   │            │
   │            ├─> :8001 [ChromaDB]
   │            └─> :7687 [Neo4j bolt]
   │
   └─> :8501  [Streamlit Admin] ← IP-restricted
```

See `docs/DEPLOYMENT.md` for full deployment guide.
