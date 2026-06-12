"""Shared ChromaDB setup helpers used across init, ingestion, and retrieval."""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any

CHROMA_COLLECTION_NAME = "regenova_intel_chunks"
CHROMA_COLLECTION_METADATA = {"hnsw:space": "cosine"}
CHROMA_EMBEDDING_MODEL = "text-embedding-3-small"


class LocalDeterministicEmbeddingFunction:
    """Lightweight lexical embedding fallback for offline/local development."""

    def __init__(self, dimensions: int = 128) -> None:
        self.dimensions = dimensions

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            tokens = re.findall(r"[a-z0-9-]+", text.lower())
            for token in tokens:
                idx = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16) % self.dimensions
                vector[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vector))
            embeddings.append([v / norm for v in vector] if norm else vector)
        return embeddings

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self._embed_texts(input)

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        return self._embed_texts(input)

    def embed_query(self, input: str | list[str]) -> list[list[float]]:
        text = input[0] if isinstance(input, list) else input
        return self._embed_texts([text])

    def name(self) -> str:
        return "regenova-local-deterministic"


def _get_openai_api_key() -> str:
    """Return a *dedicated* OpenAI embeddings key, if one is explicitly set.

    Deliberately does NOT fall back to LLM_API_KEY. The LLM key (e.g. a
    DeepSeek key used for chat/OCR) lives in a different provider and a
    different concern; treating its mere presence as "use OpenAI embeddings"
    routes DeepSeek credentials at api.openai.com and 401s. OpenAI embeddings
    are opt-in via an explicit OPENAI_API_KEY only.
    """
    return os.getenv("OPENAI_API_KEY") or ""


def get_embedding_function() -> Any:
    """Return the Chroma embedding function for this repo.

    Defaults to the offline LocalDeterministicEmbeddingFunction so that
    ingestion shares an embedding space with the grounding service (which
    also uses it). OpenAI embeddings are used only when an explicit,
    dedicated OPENAI_API_KEY is set — and note that switching backends
    changes vector dimensionality (local=128, text-embedding-3-small=1536),
    which is incompatible with an existing collection built on the other.
    """
    api_key = _get_openai_api_key()
    if api_key:
        from chromadb.utils import embedding_functions  # type: ignore[import]

        return embedding_functions.OpenAIEmbeddingFunction(
            api_key=api_key,
            model_name=CHROMA_EMBEDDING_MODEL,
        )
    return LocalDeterministicEmbeddingFunction()


def get_collection(chroma_persist_dir: str, collection_name: str = CHROMA_COLLECTION_NAME) -> Any:
    """Create a Chroma client and return the configured collection."""
    import chromadb  # type: ignore[import]

    client = chromadb.PersistentClient(path=chroma_persist_dir)
    try:
        return client.get_or_create_collection(
            name=collection_name,
            metadata=CHROMA_COLLECTION_METADATA,
            embedding_function=get_embedding_function(),
        )
    except Exception as exc:
        if "embedding function already exists" not in str(exc):
            raise
        return client.get_collection(name=collection_name)
