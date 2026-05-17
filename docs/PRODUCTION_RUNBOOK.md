# REGENOVA-Intel — Production Runbook

**Fastest path to going live online.**

Estimated time from a fresh Ubuntu server to a working HTTPS deployment: **30–60 minutes**.

---

## Prerequisites

| Item | Notes |
|------|-------|
| Linux server | Ubuntu 22.04 LTS recommended. 2 vCPU / 4 GB RAM minimum. |
| Domain name | DNS A-record pointing to the server IP |
| OpenAI API key | Or compatible LLM provider |
| Firewall ports | 22 (SSH), 80 (HTTP redirect), 443 (HTTPS) open |

> **⚠ Medical/compliance note:** This system processes clinical decision-support queries. Before opening it to real clinicians or patient data, complete a legal/compliance review for your jurisdiction.

---

## Step 1 — Provision the server

```bash
# On your server (Ubuntu 22.04)
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl

# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker          # or log out and back in
docker --version       # verify

# Install Docker Compose v2 (included with Docker Desktop; verify on server)
docker compose version # should print v2.x
```

---

## Step 2 — Clone the repository

```bash
git clone https://github.com/zoe786/regenova-intel-peptide-prescribing-assistant.git
cd regenova-intel-peptide-prescribing-assistant
```

---

## Step 3 — Create and populate `.env`

```bash
cp .env.example .env
nano .env   # or your preferred editor
```

**Minimum required values to change:**

```bash
# 1. Your LLM API key
LLM_API_KEY=sk-...

# 2. Must be "production" to enable the startup safety guard
ENVIRONMENT=production

# 3. Generate strong secrets (run these commands, paste the output):
#    python3 -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET=<paste-generated-value>
ADMIN_API_KEY=<paste-generated-value>
AUDIT_IP_SALT=<paste-generated-value>

# 4. Disable public API docs (recommended for production)
ENABLE_DOCS=false

# 5. Set your real production CORS origin
# CORS_ORIGINS=["https://app.yourdomain.com"]
```

> **Security rule:** The API will **refuse to start** with `ENVIRONMENT=production` if `JWT_SECRET`, `ADMIN_API_KEY`, or `AUDIT_IP_SALT` are still at their shipped defaults. This is intentional.

---

## Step 4 — Start the stack

```bash
# Start API, ChromaDB, and admin (Neo4j is off by default)
docker compose up -d

# Check that all containers are healthy
docker compose ps

# Stream logs to confirm clean startup
docker compose logs -f api
# Look for: "🧬 REGENOVA-Intel API starting"
# If you see "STARTUP BLOCKED", fix .env secrets and retry.
```

If Neo4j is needed:
```bash
docker compose --profile neo4j up -d
```

---

## Step 5 — Verify the stack locally

```bash
# Liveness
curl http://localhost:8000/health
# Expected: {"status":"ok","version":"0.1.0",...}

# Readiness (checks ChromaDB)
curl http://localhost:8000/health/ready
# Expected: {"status":"ready","checks":{"vector_store":"ok"},...}

# Prescriber SPA
curl -I http://localhost:8000/app
# Expected: HTTP 200

# ChromaDB heartbeat (internal port)
curl http://localhost:8001/api/v1/heartbeat
```

---

## Step 6 — Install Caddy (HTTPS reverse proxy)

Caddy is the fastest route to automatic, auto-renewing HTTPS.

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

Copy and edit the example Caddyfile:

```bash
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
# Replace <YOUR_DOMAIN> with your actual domain (e.g. app.yourdomain.com)

sudo systemctl reload caddy
sudo systemctl enable caddy
```

Caddy automatically provisions a Let's Encrypt TLS certificate on the first request.

**Verify HTTPS:**
```bash
curl https://app.yourdomain.com/health
```

---

## Step 7 — Run data ingestion

Place raw source files in `data/raw/<type>/` (see `docs/DATA_INGESTION_PLAYBOOK.md`), then:

```bash
# Inside the API container
docker compose exec api python pipelines/run_all_ingestion.py

# or via Make on the host (if Python env is set up)
make ingest-all
```

Verify data was ingested:
```bash
curl http://localhost:8000/health/ready
# "vector_store": "ok" and ideally a non-zero collection count
```

---

## Step 8 — Run the smoke test

```bash
docker compose exec api python scripts/smoke_test.py
# All checks should show ✓ PASS
```

---

## Step 9 — Access the admin dashboard

The admin UI is **bound to loopback only** and must be accessed via SSH tunnel:

```bash
# From your local machine:
ssh -L 8501:localhost:8501 user@app.yourdomain.com

# Then open in your browser:
http://localhost:8501
```

> Never expose port 8501 directly on the public internet. The Streamlit admin has no additional auth beyond the `ADMIN_API_KEY` it uses to call the API.

---

## Post-launch checklist

- [ ] All five smoke-test checks pass
- [ ] HTTPS cert is valid (`curl -v https://app.yourdomain.com/health`)
- [ ] `/docs` returns 404 (if `ENABLE_DOCS=false`)
- [ ] `ENVIRONMENT=production` is set in `.env`
- [ ] `JWT_SECRET`, `ADMIN_API_KEY`, `AUDIT_IP_SALT` are strong random values
- [ ] `NEO4J_PASSWORD` is not `changeme` (if Neo4j is enabled)
- [ ] Admin UI accessible only via SSH tunnel (not on `0.0.0.0:8501`)
- [ ] `docker compose ps` shows all services as `healthy`
- [ ] Daily backup cron is set (see Backup section below)
- [ ] Legal/compliance review completed before real patient data

---

## Backup

```bash
# Backup ChromaDB and audit database (run daily via cron)
sudo crontab -e
# Add:
# 0 2 * * * tar czf /backups/regenova-$(date +\%Y\%m\%d).tar.gz \
#   /path/to/repo/data/chroma_db /path/to/repo/data/audit.db
```

---

## Rollback

```bash
# Roll back to a previous git tag
git fetch --tags
git checkout v<previous-tag>
docker compose build api admin
docker compose up -d

# Or roll forward by reverting
git revert HEAD
docker compose build api admin
docker compose up -d
```

---

## Common problems

| Symptom | Cause | Fix |
|---------|-------|-----|
| API container exits immediately | Insecure defaults in production | Check logs: `docker compose logs api` — look for "STARTUP BLOCKED" |
| `health/ready` shows `vector_store: degraded` | No ingestion run yet | Run `make ingest-all` |
| 502 Bad Gateway from Caddy | API container not healthy | `docker compose ps` — wait for healthy, or check logs |
| Admin UI shows "Connection refused" | API not healthy yet | Wait for `api` to be `healthy`, then restart `admin` |
| ChromaDB permission error | Volume ownership mismatch | `docker compose down -v && docker compose up -d` (⚠ clears data) |

---

## Environment variable quick reference

| Variable | Required | Production value |
|----------|----------|-----------------|
| `LLM_API_KEY` | ✅ | Your real API key |
| `ENVIRONMENT` | ✅ | `production` |
| `JWT_SECRET` | ✅ | Strong random hex (32+ bytes) |
| `ADMIN_API_KEY` | ✅ | Strong random hex (32+ bytes) |
| `AUDIT_IP_SALT` | ✅ | Strong random hex (32+ bytes) |
| `ENABLE_DOCS` | Recommended | `false` |
| `CORS_ORIGINS` | Recommended | `["https://app.yourdomain.com"]` |
| `NEO4J_PASSWORD` | If Neo4j | Strong password (not `changeme`) |

Generate secrets quickly:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```
