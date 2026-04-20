"""Reindex script — re-embeds and re-upserts all normalized chunks to the vector store.

Loads all JSON files from data/processed/normalized/ and upserts them
to ChromaDB. Deduplication is handled by ChromaDB (upsert by chunk_id).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

NORMALIZED_DIR = Path("./data/processed/normalized")
CHROMA_PERSIST_DIR = Path("./data/chroma_db")


@dataclass
class MockRecord:
    """Lightweight record for reindexing — mirrors NormalizedRecord interface."""
    chunk_id: str
    document_id: str
    source_type: str
    source_name: str
    source_url: str | None
    acquired_at: datetime
    published_at: datetime | None
    evidence_tier_default: int
    jurisdiction: str | None
    content_hash: str
    content: str
    chunk_index: int


def load_all_records(normalized_dir: Path) -> list[MockRecord]:
    """Load all normalized chunk JSON files."""
    records: list[MockRecord] = []
    json_files = list(normalized_dir.glob("*.json"))
    logger.info("Found %d normalized chunk files", len(json_files))

    for path in json_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            acquired = data.get("acquired_at", "2024-01-01T00:00:00")
            published = data.get("published_at")

            record = MockRecord(
                chunk_id=data["chunk_id"],
                document_id=data["document_id"],
                source_type=data.get("source_type", "unknown"),
                source_name=data.get("source_name", "unknown"),
                source_url=data.get("source_url"),
                acquired_at=datetime.fromisoformat(acquired) if isinstance(acquired, str) else acquired,
                published_at=datetime.fromisoformat(published) if isinstance(published, str) else None,
                evidence_tier_default=int(data.get("evidence_tier_default", 3)),
                jurisdiction=data.get("jurisdiction"),
                content_hash=data.get("content_hash", ""),
                content=data["content"],
                chunk_index=int(data.get("chunk_index", 0)),
            )
            records.append(record)
        except Exception as exc:
            logger.warning("Failed to load %s: %s", path.name, exc)

    return records


def main() -> None:
    """Run reindexing of all normalized chunks."""
    if not NORMALIZED_DIR.exists():
        logger.error("Normalized directory not found: %s", NORMALIZED_DIR)
        sys.exit(1)

    start = time.time()
    records = load_all_records(NORMALIZED_DIR)

    if not records:
        logger.warning("No records found to reindex")
        print("⚠ No records found in data/processed/normalized/")
        return

    logger.info("Reindexing %d chunks to ChromaDB...", len(records))

    from pipelines.common.storage import save_to_vector_store  # type: ignore[import]
    upserted = save_to_vector_store(records, chroma_persist_dir=str(CHROMA_PERSIST_DIR))

    elapsed = time.time() - start
    print(f"\n✓ Reindex complete: {upserted} chunks upserted in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
