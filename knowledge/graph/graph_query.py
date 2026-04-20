"""Knowledge graph query interface.

Loads the NetworkX graph from disk and provides semantic query methods
for peptide relationships, contraindications, and evidence paths.
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_GRAPH_PATH = Path("data/processed/graph.pkl")


class GraphQuery:
    """Provides query methods over the peptide knowledge graph.

    The graph is loaded lazily on first use and cached in memory.
    """

    def __init__(self, graph_path: Path = _DEFAULT_GRAPH_PATH) -> None:
        self.graph_path = Path(graph_path)
        self._graph: Any = None

    def _load_graph(self) -> Any:
        """Load the NetworkX graph from disk (lazy, cached)."""
        if self._graph is not None:
            return self._graph

        if not self.graph_path.exists():
            logger.warning("Graph file not found: %s", self.graph_path)
            return None

        try:
            with open(self.graph_path, "rb") as f:
                self._graph = pickle.load(f)
            logger.info(
                "Graph loaded: %d nodes, %d edges",
                self._graph.number_of_nodes(),
                self._graph.number_of_edges(),
            )
            return self._graph
        except Exception as exc:
            logger.error("Failed to load graph: %s", exc)
            return None

    def find_related_peptides(self, peptide_name: str) -> list[dict]:
        """Find peptides related to the given peptide via any graph edge.

        Args:
            peptide_name: Canonical peptide name to query.

        Returns:
            List of dicts: {peptide, relation, evidence_tier, confidence}.
        """
        graph = self._load_graph()
        if graph is None:
            return []

        results: list[dict] = []
        try:
            if peptide_name not in graph:
                return []

            # Outgoing edges
            for _, neighbor, data in graph.out_edges(peptide_name, data=True):
                results.append({
                    "peptide": neighbor,
                    "relation": data.get("relation"),
                    "direction": "outgoing",
                    "evidence_tier": data.get("evidence_tier", 3),
                    "confidence": data.get("confidence", 0.5),
                })

            # Incoming edges
            for predecessor, _, data in graph.in_edges(peptide_name, data=True):
                results.append({
                    "peptide": predecessor,
                    "relation": data.get("relation"),
                    "direction": "incoming",
                    "evidence_tier": data.get("evidence_tier", 3),
                    "confidence": data.get("confidence", 0.5),
                })

        except Exception as exc:
            logger.error("Graph query error for %s: %s", peptide_name, exc)

        return results

    def find_contraindications(self, peptide_name: str) -> list[dict]:
        """Find contraindications for a given peptide.

        Returns edges where relation = CONTRAINDICATED_WITH.

        Args:
            peptide_name: Canonical peptide name.

        Returns:
            List of contraindication dicts.
        """
        related = self.find_related_peptides(peptide_name)
        return [r for r in related if r.get("relation") == "CONTRAINDICATED_WITH"]

    def find_evidence_path(self, entity1: str, entity2: str) -> list[list[str]]:
        """Find shortest paths between two entities in the graph.

        Args:
            entity1: Source entity name.
            entity2: Target entity name.

        Returns:
            List of paths (each path is a list of entity name strings).
            Empty list if no path exists or graph is unavailable.
        """
        graph = self._load_graph()
        if graph is None:
            return []

        try:
            import networkx as nx  # type: ignore[import]
            paths = list(nx.all_simple_paths(graph, entity1, entity2, cutoff=4))
            return paths[:5]  # Return at most 5 paths
        except Exception as exc:
            logger.debug("No path found between %s and %s: %s", entity1, entity2, exc)
            return []
