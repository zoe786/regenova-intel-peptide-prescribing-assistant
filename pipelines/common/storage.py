"""Storage utilities for the ingestion pipeline.

Provides functions for saving/loading normalized records to the filesystem
and upserting chunk embeddings to the ChromaDB vector store.

TODO: Abstract vector store backend via VectorStoreBackend protocol
to support Weaviate and Pinecone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHROMA_COLLECTION_NAME = "regenova_intel_chunks"
_EMBEDDING_MODEL = "text-embedding-3-small"  # TODO: make configurable


def save_normalized(record: Any, output_dir: Path) -> Path:
    """Save a NormalizedRecord to a JSON file in the output directory.

    Args:
        record: A NormalizedRecord dataclass instance.
        output_dir: Directory to save the JSON file.

    Returns:
        Path to the saved JSON file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{record.chunk_id}.json"
    filepath = output_dir / filename

    data = {
        "chunk_id": record.chunk_id,
        "document_id": record.document_id,
        "source_type": record.source_type,
        "source_name": record.source_name,
        "source_url": record.source_url,
        "acquired_at": record.acquired_at.isoformat() if hasattr(record.acquired_at, "isoformat") else str(record.acquired_at),
        "published_at": record.published_at.isoformat() if record.published_at and hasattr(record.published_at, "isoformat") else record.published_at,
        "evidence_tier_default": record.evidence_tier_default,
        "jurisdiction": record.jurisdiction,
        "content_hash": record.content_hash,
        "content": record.content,
        "chunk_index": record.chunk_index,
        "metadata": {
            "source_type": record.source_type,
            "source_name": record.source_name,
            "source_url": record.source_url,
            "acquired_at": record.acquired_at.isoformat() if hasattr(record.acquired_at, "isoformat") else str(record.acquired_at),
            "published_at": record.published_at.isoformat() if record.published_at and hasattr(record.published_at, "isoformat") else record.published_at,
            "evidence_tier_default": record.evidence_tier_default,
            "jurisdiction": record.jurisdiction,
            "content_hash": record.content_hash,
            "document_id": record.document_id,
            "chunk_id": record.chunk_id,
        },
    }

    filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug("Saved normalized chunk: %s", filepath)
    return filepath


def load_normalized(path: Path) -> dict:
    """Load a normalized record from a JSON file.

    Args:
        path: Path to the JSON file.

    Returns:
        Dictionary of the normalized record data.
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


def list_normalized(output_dir: Path) -> list[Path]:
    """List all normalized chunk JSON files in a directory.

    Args:
        output_dir: Directory containing normalized JSON files.

    Returns:
        Sorted list of Path objects for each JSON file.
    """
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return []
    return sorted(output_dir.glob("*.json"))


def save_to_vector_store(
    chunks: list[Any],
    collection_name: str = CHROMA_COLLECTION_NAME,
    chroma_persist_dir: str = "./data/chroma_db",
) -> int:
    """Embed and upsert normalized chunks to ChromaDB.

    Uses OpenAI text-embedding-3-small for embeddings via ChromaDB's
    built-in embedding function. Falls back to ChromaDB default embeddings
    if OpenAI is unavailable.

    Args:
        chunks: List of NormalizedRecord objects to embed and upsert.
        collection_name: ChromaDB collection name.
        chroma_persist_dir: Path to ChromaDB persistence directory.

    Returns:
        Number of chunks successfully upserted.

    TODO: Abstract to support Weaviate and Pinecone backends.
    """
    if not chunks:
        logger.info("No chunks to upsert to vector store")
        return 0

    try:
        import chromadb  # type: ignore[import]

        client = chromadb.PersistentClient(path=chroma_persist_dir)
        collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict] = []

        for chunk in chunks:
            ids.append(chunk.chunk_id)
            documents.append(chunk.content)
            metadatas.append({
                "source_type": chunk.source_type,
                "source_name": chunk.source_name,
                "source_url": chunk.source_url or "",
                "acquired_at": chunk.acquired_at.isoformat() if hasattr(chunk.acquired_at, "isoformat") else str(chunk.acquired_at),
                "published_at": chunk.published_at.isoformat() if chunk.published_at and hasattr(chunk.published_at, "isoformat") else "",
                "evidence_tier_default": chunk.evidence_tier_default,
                "jurisdiction": chunk.jurisdiction or "",
                "content_hash": chunk.content_hash,
                "document_id": chunk.document_id,
                "chunk_id": chunk.chunk_id,
            })

        # Upsert in batches of 100 to avoid memory issues
        batch_size = 100
        upserted = 0
        for i in range(0, len(ids), batch_size):
            batch_ids = ids[i:i + batch_size]
            batch_docs = documents[i:i + batch_size]
            batch_metas = metadatas[i:i + batch_size]
            collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
            upserted += len(batch_ids)
            logger.info("Upserted batch %d/%d (%d chunks)", i // batch_size + 1, (len(ids) + batch_size - 1) // batch_size, len(batch_ids))

        logger.info("Total upserted: %d chunks to collection '%s'", upserted, collection_name)
        return upserted

    except ImportError:
        logger.error("chromadb not installed — cannot save to vector store")
        return 0
    except Exception as exc:
        logger.error("Failed to save to vector store: %s", exc)
        return 0
