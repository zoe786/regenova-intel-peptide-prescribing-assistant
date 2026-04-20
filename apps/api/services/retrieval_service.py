"""Vector retrieval service for REGENOVA-Intel.

Connects to ChromaDB (default) and retrieves relevant chunks
for a given query using cosine similarity search.

TODO: Support Weaviate and Pinecone backends via VectorStoreBackend protocol.
TODO: Add graph-hybrid retrieval mode (ENABLE_GRAPH_RETRIEVAL flag).
"""

from __future__ import annotations

import logging
from typing import Any

from apps.api.schemas.source import NormalizedChunk, SourceMetadata

logger = logging.getLogger(__name__)

CHROMA_COLLECTION_NAME = "regenova_intel_chunks"


class RetrievalService:
    """Retrieves relevant knowledge chunks from the vector store.

    On initialisation, connects to the configured ChromaDB instance.
    Subsequent calls to retrieve() perform approximate nearest-neighbour
    search and return ranked NormalizedChunk objects.
    """

    def __init__(self, chroma_persist_dir: str = "./data/chroma_db") -> None:
        """Initialise retrieval service and connect to ChromaDB.

        Args:
            chroma_persist_dir: Path to ChromaDB persistence directory.
        """
        self.chroma_persist_dir = chroma_persist_dir
        self._client: Any = None
        self._collection: Any = None
        self._connect()

    def _connect(self) -> None:
        """Establish connection to ChromaDB.

        Raises:
            RuntimeError: If ChromaDB cannot be initialised.
        """
        try:
            import chromadb  # type: ignore[import]

            self._client = chromadb.PersistentClient(path=self.chroma_persist_dir)
            self._collection = self._client.get_or_create_collection(
                name=CHROMA_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "Connected to ChromaDB at %s, collection=%s, count=%d",
                self.chroma_persist_dir,
                CHROMA_COLLECTION_NAME,
                self._collection.count(),
            )
        except Exception as exc:
            logger.error("Failed to connect to ChromaDB: %s", exc)
            # Allow degraded mode — retrieve() will return empty list
            self._client = None
            self._collection = None

    def is_ready(self) -> bool:
        """Return True if the vector store connection is healthy."""
        if self._collection is None:
            return False
        try:
            self._collection.count()
            return True
        except Exception:
            return False

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[NormalizedChunk]:
        """Retrieve top-K relevant chunks for a query.

        Performs cosine similarity search against the vector store.
        Returns chunks with similarity_score populated.

        Args:
            query: The query text to search for.
            top_k: Number of chunks to return.
            filters: Optional ChromaDB where-clause filters (e.g. {"evidence_tier": {"$lte": 3}}).

        Returns:
            List of NormalizedChunk objects sorted by relevance (highest first).
        """
        if self._collection is None:
            logger.warning("Vector store not available — returning empty results")
            return []

        try:
            query_params: dict[str, Any] = {
                "query_texts": [query],
                "n_results": min(top_k, max(1, self._collection.count())),
                "include": ["documents", "metadatas", "distances"],
            }
            if filters:
                query_params["where"] = filters

            results = self._collection.query(**query_params)

            chunks: list[NormalizedChunk] = []
            documents = results.get("documents", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            for doc, meta, dist in zip(documents, metadatas, distances):
                similarity = 1.0 - float(dist)  # convert cosine distance → similarity
                try:
                    source_meta = SourceMetadata(
                        source_type=meta.get("source_type", "unknown"),
                        source_name=meta.get("source_name", "Unknown Source"),
                        source_url=meta.get("source_url"),
                        acquired_at=meta.get("acquired_at", "2024-01-01T00:00:00"),
                        published_at=meta.get("published_at"),
                        evidence_tier_default=int(meta.get("evidence_tier_default", 3)),
                        jurisdiction=meta.get("jurisdiction"),
                        content_hash=meta.get("content_hash", ""),
                        document_id=meta.get("document_id", ""),
                    )
                    chunk = NormalizedChunk(
                        chunk_id=meta.get("chunk_id", ""),
                        document_id=meta.get("document_id", ""),
                        content=doc,
                        metadata=source_meta,
                        similarity_score=similarity,
                    )
                    chunks.append(chunk)
                except Exception as parse_err:
                    logger.warning("Failed to parse chunk metadata: %s", parse_err)

            logger.info(
                "Retrieved %d chunks for query (top_k=%d)", len(chunks), top_k
            )
            return chunks

        except Exception as exc:
            logger.error("Retrieval failed: %s", exc)
            return []

    # TODO: Add graph-hybrid retrieval combining vector + graph traversal
    # def retrieve_with_graph(self, query: str, top_k: int = 5) -> list[NormalizedChunk]:
    #     vector_chunks = self.retrieve(query, top_k)
    #     from knowledge.graph.graph_query import GraphQuery
    #     graph_chunks = GraphQuery().expand_from_chunks(vector_chunks)
    #     return merge_and_rerank(vector_chunks, graph_chunks)
