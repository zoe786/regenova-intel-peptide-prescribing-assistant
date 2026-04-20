#!/usr/bin/env bash
# REGENOVA-Intel Bootstrap Script
# Creates virtualenv, installs dependencies, and initialises databases.

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── Prerequisites ─────────────────────────────────────────────────────────────
echo -e "\n${BLUE}╔════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   REGENOVA-Intel Bootstrap             ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════╝${NC}\n"

command -v python3 >/dev/null 2>&1 || error "python3 not found. Install Python 3.11+"

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
REQUIRED_MAJOR=3
REQUIRED_MINOR=11

PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt "$REQUIRED_MAJOR" ] || { [ "$PYTHON_MAJOR" -eq "$REQUIRED_MAJOR" ] && [ "$PYTHON_MINOR" -lt "$REQUIRED_MINOR" ]; }; then
    error "Python $REQUIRED_MAJOR.$REQUIRED_MINOR+ required. Found: $PYTHON_VERSION"
fi
success "Python $PYTHON_VERSION detected"

# ── Virtual Environment ───────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    info "Creating virtual environment (.venv)..."
    python3 -m venv .venv
    success "Virtual environment created"
else
    info "Virtual environment already exists (.venv)"
fi

info "Activating virtual environment..."
# shellcheck source=/dev/null
source .venv/bin/activate || error "Failed to activate virtual environment"
success "Virtual environment activated"

# ── Install Dependencies ──────────────────────────────────────────────────────
info "Installing dependencies from requirements.txt..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
success "Dependencies installed"

# ── Environment File ──────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    info "Creating .env from .env.example..."
    cp .env.example .env
    success ".env created — edit it to set your OPENAI_API_KEY and other secrets"
    warn "⚠️  Don't forget to set OPENAI_API_KEY in .env before running!"
else
    info ".env already exists — skipping"
fi

# ── Data Directories ──────────────────────────────────────────────────────────
info "Creating data directories..."
mkdir -p \
    data/raw/documents \
    data/raw/websites \
    data/raw/skool/courses \
    data/raw/skool/community \
    data/raw/youtube \
    data/raw/forums \
    data/raw/pubmed \
    data/processed/normalized \
    data/processed/claims \
    data/processed/triples \
    data/curated \
    data/exports \
    data/chroma_db
success "Data directories created"

# ── Database Initialisation ───────────────────────────────────────────────────
info "Initialising databases..."
python3 scripts/init_db.py && success "Databases initialised" || warn "Database init skipped (check scripts/init_db.py)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}╔════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Bootstrap Complete! 🎉               ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════╝${NC}\n"
echo "Next steps:"
echo "  1. Edit .env and set OPENAI_API_KEY"
echo "  2. Place source files in data/raw/<type>/"
echo "  3. Run: make ingest-all"
echo "  4. Run: make run-api"
echo "  5. Run: make run-admin"
echo ""
