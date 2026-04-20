# Deployment Guide

## Prerequisites

- Python 3.11+
- Docker & Docker Compose (for containerised deployment)
- OpenAI API key
- Git

---

## 1. Local Development Setup

```bash
# Clone repository
git clone https://github.com/zoe786/regenova-intel-peptide-prescribing-assistant.git
cd regenova-intel-peptide-prescribing-assistant

# Run bootstrap script (creates venv, installs deps, creates .env)
bash scripts/bootstrap.sh

# Edit environment variables
nano .env   # set OPENAI_API_KEY at minimum

# Initialise databases
python scripts/init_db.py

# Start API
make run-api       # http://localhost:8000
make run-admin     # http://localhost:8501 (separate terminal)
```

---

## 2. Docker Compose Setup

```bash
# Copy and configure environment
cp .env.example .env
nano .env

# Start all services
make docker-up
# or:
docker compose up -d

# Check service health
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8001/api/v1/heartbeat  # ChromaDB

# View logs
docker compose logs -f api
docker compose logs -f chromadb
```

Services started:
| Service | Port | URL |
|---------|------|-----|
| API | 8000 | http://localhost:8000 |
| ChromaDB | 8001 | http://localhost:8001/api/v1 |
| Neo4j Browser | 7474 | http://localhost:7474 |
| Neo4j Bolt | 7687 | bolt://localhost:7687 |

---

## 3. Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | Yes | — | OpenAI API key |
| `LLM_MODEL` | No | `gpt-4o` | LLM model name |
| `LLM_TEMPERATURE` | No | `0.1` | LLM temperature |
| `VECTOR_DB_BACKEND` | No | `chroma` | Vector store backend |
| `CHROMA_PERSIST_DIR` | No | `./data/chroma_db` | ChromaDB data directory |
| `NEO4J_URI` | No | — | Neo4j bolt URI |
| `NEO4J_USER` | No | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | No | — | Neo4j password |
| `API_HOST` | No | `0.0.0.0` | API bind host |
| `API_PORT` | No | `8000` | API bind port |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `ENVIRONMENT` | No | `development` | Environment name |
| `JWT_SECRET` | Yes (prod) | — | JWT signing secret |
| `ADMIN_API_KEY` | Yes (prod) | — | Admin API key |
| `PUBMED_EMAIL` | For PubMed | — | NCBI email for Entrez |
| `PUBMED_API_KEY` | No | — | NCBI API key (higher rate limits) |
| `ENABLE_GRAPH_RETRIEVAL` | No | `false` | Enable graph-hybrid retrieval |
| `ENABLE_RECONSTITUTION_GUIDANCE` | No | `false` | Enable reconstitution guidance |

---

## 4. Database Initialisation

### ChromaDB
```bash
python scripts/init_db.py
```
Creates collection `regenova_intel_chunks` in ChromaDB.

### Neo4j (if enabled)
```bash
# Connect to Neo4j browser at http://localhost:7474
# Default credentials: neo4j / changeme (set in .env)
# No schema initialisation needed — graph is schema-optional
```

---

## 5. First-Run Ingestion

```bash
# Place source files in data/raw/<type>/
# See docs/DATA_INGESTION_PLAYBOOK.md for file format details

# Run full ingestion
make ingest-all

# Validate
make smoke-test
```

---

## 6. Health Check Endpoints

```bash
# Liveness
GET http://localhost:8000/health
# Response: {"status": "ok", "version": "0.1.0", "timestamp": "...", "environment": "development"}

# Readiness (checks vector store)
GET http://localhost:8000/health/ready
# Response: {"status": "ready"} or {"status": "degraded", "detail": "..."}
```

---

## 7. Production Considerations

### Reverse Proxy (nginx)

```nginx
upstream regenova_api {
    server 127.0.0.1:8000;
}

server {
    listen 443 ssl;
    server_name api.regenova-intel.example.com;
    
    ssl_certificate     /etc/ssl/certs/regenova.crt;
    ssl_certificate_key /etc/ssl/private/regenova.key;
    
    location / {
        proxy_pass http://regenova_api;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### TLS
- Use Let's Encrypt with Certbot or Caddy for automatic TLS
- Never serve the API over plain HTTP in production

### Secrets Management
- Use AWS Secrets Manager, HashiCorp Vault, or similar
- Never commit `.env` to version control
- Rotate `JWT_SECRET` and `ADMIN_API_KEY` regularly

### Scaling
- API: Gunicorn + Uvicorn workers (`uvicorn apps.api.main:app --workers 4`)
- ChromaDB: Move to managed Weaviate or Pinecone for horizontal scaling
- Graph: Move to Neo4j Aura or self-hosted Neo4j cluster

### Backup
```bash
# Backup ChromaDB data
cp -r data/chroma_db data/chroma_db.bak.$(date +%Y%m%d)

# Backup Neo4j
docker exec regenova_neo4j neo4j-admin database dump neo4j --to-path=/backups/
```

---

## 8. Monitoring

- **Liveness probe:** `GET /health` → 200 OK
- **Readiness probe:** `GET /health/ready` → 200 OK
- **Logs:** Structured JSON logs via loguru, pipe to ELK/CloudWatch
- **Metrics:** TODO — Add Prometheus `/metrics` endpoint
- **Alerting:** Set alerts on: 5xx error rate > 1%, latency p99 > 5s, vector store unreachable

---

## Rollback

```bash
# Git rollback
git checkout <previous-tag>
pip install -r requirements.txt
make docker-up
```
