"""Knowledge graph builder from extracted triples.

Loads all triple files, builds a NetworkX DiGraph,
and persists to data/processed/graph.pkl and graph_edges.jsonl.
"""

from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path

logger = logging.getLogger(__name__)

_GRAPH_PKL_PATH = Path("data/processed/graph.pkl")
_GRAPH_EDGES_PATH = Path("data/processed/graph_edges.jsonl")


class GraphBuilder:
    """Builds a NetworkX DiGraph from extracted triple records."""

    def __init__(
        self,
        triples_dir: Path = Path("data/processed/triples"),
        graph_path: Path = _GRAPH_PKL_PATH,
        edges_path: Path = _GRAPH_EDGES_PATH,
    ) -> None:
        self.triples_dir = Path(triples_dir)
        self.graph_path = Path(graph_path)
        self.edges_path = Path(edges_path)

    def _load_all_triples(self) -> list[dict]:
        """Load all triple records from the triples directory."""
        triples: list[dict] = []
        for path in self.triples_dir.glob("*_triples.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                triples.extend(data if isinstance(data, list) else [data])
            except Exception as e:
                logger.warning("Failed to load triples from %s: %s", path, e)
        return triples

    def build(self, triples: list[dict]) -> object:
        """Build a NetworkX DiGraph from triple records.

        Args:
            triples: List of {subject, relation, object, ...} dicts.

        Returns:
            NetworkX DiGraph instance.
        """
        import networkx as nx  # type: ignore[import]

        graph = nx.DiGraph()

        for triple in triples:
            subj = triple.get("subject", "").strip()
            rel = triple.get("relation", "").strip()
            obj = triple.get("object", "").strip()

            if not (subj and rel and obj):
                continue

            graph.add_node(subj, entity_type=triple.get("subject_type", "unknown"))
            graph.add_node(obj, entity_type=triple.get("object_type", "unknown"))
            graph.add_edge(
                subj, obj,
                relation=rel,
                evidence_tier=triple.get("evidence_tier", 3),
                source_chunk_id=triple.get("source_chunk_id", ""),
                confidence=triple.get("confidence", 0.5),
            )

        logger.info(
            "Graph built: %d nodes, %d edges",
            graph.number_of_nodes(),
            graph.number_of_edges(),
        )
        return graph

    def save(self, graph: object) -> None:
        """Persist graph to pickle and export edges to JSONL."""
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.graph_path, "wb") as f:
            pickle.dump(graph, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("Graph saved to %s", self.graph_path)

        import networkx as nx  # type: ignore[import]
        with open(self.edges_path, "w", encoding="utf-8") as f:
            for u, v, data in graph.edges(data=True):  # type: ignore[attr-defined]
                edge = {"source": u, "target": v, **data}
                f.write(json.dumps(edge) + "\n")
        logger.info("Edges exported to %s", self.edges_path)

    def run(self) -> int:
        """Load triples, build graph, and save."""
        triples = self._load_all_triples()
        logger.info("GraphBuilder: loaded %d triples", len(triples))
        graph = self.build(triples)
        self.save(graph)
        return len(triples)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    count = GraphBuilder().run()
    print(f"Graph built from {count} triples")


if __name__ == "__main__":
    main()
