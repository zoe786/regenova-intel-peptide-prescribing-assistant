"""Smoke test script for REGENOVA-Intel.

Validates core system components without requiring external services.
Prints PASS/FAIL for each check.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add repo root to path
sys.path.insert(0, str(Path(__file__).parents[1]))

PASS = "✓ PASS"
FAIL = "✗ FAIL"
WARN = "⚠ WARN"

results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> bool:
    """Run a check function and record the result."""
    try:
        passed, msg = fn()
        status = PASS if passed else FAIL
        results.append((name, status, msg))
        return passed
    except Exception as exc:
        results.append((name, FAIL, f"Exception: {exc}"))
        return False


# ── Check 1: Normalized data directory ────────────────────────────────────────
def check_normalized_dir():
    normalized = Path("data/processed/normalized")
    if not normalized.exists():
        return False, "data/processed/normalized/ does not exist"
    json_files = list(normalized.glob("*.json"))
    if not json_files:
        return True, f"Directory exists but is empty (run make ingest-all)"
    return True, f"{len(json_files)} chunk files found"


# ── Check 2: ChromaDB accessible ──────────────────────────────────────────────
def check_chromadb():
    try:
        import chromadb  # type: ignore[import]
        client = chromadb.PersistentClient(path="./data/chroma_db")
        collection = client.get_or_create_collection("regenova_intel_chunks")
        count = collection.count()
        return True, f"ChromaDB accessible, collection count={count}"
    except ImportError:
        return False, "chromadb not installed"
    except Exception as exc:
        return False, f"ChromaDB error: {exc}"


# ── Check 3: Sample retrieval ─────────────────────────────────────────────────
def check_retrieval():
    try:
        from apps.api.services.retrieval_service import RetrievalService
        svc = RetrievalService(chroma_persist_dir="./data/chroma_db")
        if not svc.is_ready():
            return True, "ChromaDB not ready — skipping retrieval test (no data)"
        chunks = svc.retrieve("BPC-157 tendon healing", top_k=3)
        if chunks:
            return True, f"Retrieved {len(chunks)} chunks"
        return True, "No chunks returned (collection may be empty)"
    except Exception as exc:
        return False, f"Retrieval error: {exc}"


# ── Check 4: SafetyRuleEngine pregnancy flag ───────────────────────────────────
def check_safety_pregnancy():
    try:
        from apps.api.services.safety_rules import SafetyRuleEngine
        from apps.api.schemas.patient_case import PatientCase

        engine = SafetyRuleEngine()
        patient = PatientCase(
            contraindications=["pregnant"],
            indications=["tendon healing"],
        )
        flags = engine.evaluate(
            query="What is the dosing protocol for BPC-157?",
            patient_case=patient,
            chunks=[],
        )
        critical_flags = [f for f in flags if f.severity == "critical" and f.code == "SR-001"]
        if critical_flags:
            return True, f"SR-001 triggered correctly: {critical_flags[0].message[:60]}..."
        return False, f"SR-001 not triggered. Flags: {[f.code for f in flags]}"
    except Exception as exc:
        return False, f"Safety rule error: {exc}"


# ── Check 5: CitationService ──────────────────────────────────────────────────
def check_citations():
    try:
        from apps.api.services.citation_service import CitationService
        from apps.api.schemas.source import NormalizedChunk, SourceMetadata
        from datetime import datetime

        svc = CitationService()
        meta = SourceMetadata(
            source_type="pubmed",
            source_name="Test Journal",
            acquired_at=datetime.utcnow(),
            evidence_tier_default=1,
            content_hash="abc123",
            document_id="doc_001",
        )
        chunk = NormalizedChunk(
            chunk_id="chunk_001",
            document_id="doc_001",
            content="BPC-157 demonstrated tendon healing in animal models.",
            metadata=meta,
            similarity_score=0.85,
        )
        answer, citations = svc.attach_citations([chunk], "The evidence shows promising results.")
        if not citations:
            return False, "No citations produced"
        if "[1]" not in answer and "Sources" not in answer:
            return False, "No citation markers in answer text"
        return True, f"{len(citations)} citation(s) attached correctly"
    except Exception as exc:
        return False, f"CitationService error: {exc}"


# ── Run all checks ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n🧬 REGENOVA-Intel Smoke Test\n" + "─" * 45)

    check("1. Normalized data directory", check_normalized_dir)
    check("2. ChromaDB accessible", check_chromadb)
    check("3. Sample retrieval", check_retrieval)
    check("4. Safety: pregnancy flag (SR-001)", check_safety_pregnancy)
    check("5. CitationService integrity", check_citations)

    print("\n" + "─" * 45)
    passed_count = sum(1 for _, s, _ in results if s == PASS)
    failed_count = sum(1 for _, s, _ in results if s == FAIL)

    for name, status, msg in results:
        print(f"  {status}  {name}")
        if status == FAIL or (status == WARN):
            print(f"         → {msg}")
        else:
            print(f"         → {msg}")

    print("─" * 45)
    print(f"  Results: {passed_count} passed, {failed_count} failed\n")

    sys.exit(0 if failed_count == 0 else 1)
