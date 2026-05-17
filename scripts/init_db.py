"""Database initialisation script.

Initialises ChromaDB collection and creates placeholder graph file.
Run this script once after bootstrap or when resetting the database.
"""

from __future__ import annotations

import logging
import pickle
import sys
from pathlib import Path

def _ensure_repo_root_on_path() -> None:
    repo_root = str(Path(__file__).resolve().parents[1])
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


if __package__ in (None, ""):
    _ensure_repo_root_on_path()

from pipelines.common.chroma import CHROMA_COLLECTION_NAME, get_collection

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

CHROMA_PERSIST_DIR = Path("./data/chroma_db")
GRAPH_PKL_PATH = Path("./data/processed/graph.pkl")


def init_chromadb() -> bool:
    """Initialise ChromaDB collection.

    Returns:
        True if successful, False if ChromaDB unavailable.
    """
    try:
        CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
        collection = get_collection(str(CHROMA_PERSIST_DIR), CHROMA_COLLECTION_NAME)
        logger.info(
            "✓ ChromaDB collection '%s' ready (count=%d)",
            CHROMA_COLLECTION_NAME,
            collection.count(),
        )
        return True
    except ImportError:
        logger.error("chromadb not installed — run: pip install chromadb")
        return False
    except Exception as exc:
        logger.error("ChromaDB init failed: %s", exc)
        return False


def init_graph() -> bool:
    """Create a placeholder empty graph.pkl if it doesn't exist.

    Returns:
        True if successful.
    """
    try:
        import networkx as nx  # type: ignore[import]

        GRAPH_PKL_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not GRAPH_PKL_PATH.exists():
            empty_graph = nx.DiGraph()
            with open(GRAPH_PKL_PATH, "wb") as f:
                pickle.dump(empty_graph, f)
            logger.info("✓ Empty graph placeholder created at %s", GRAPH_PKL_PATH)
        else:
            logger.info("✓ Graph file already exists at %s", GRAPH_PKL_PATH)
        return True
    except ImportError:
        logger.warning("networkx not installed — graph placeholder skipped")
        return True
    except Exception as exc:
        logger.error("Graph init failed: %s", exc)
        return False


def main() -> None:
    """Run all database initialisations."""
    logger.info("REGENOVA-Intel database initialisation starting...")

    chroma_ok = init_chromadb()
    graph_ok = init_graph()

    if chroma_ok and graph_ok:
        logger.info("✓ All initialisations complete")
        print("\n✓ Database initialisation successful!")
        print(f"  ChromaDB: {CHROMA_PERSIST_DIR}")
        print(f"  Graph: {GRAPH_PKL_PATH}\n")
    else:
        print("\n⚠ Some initialisations failed — check logs above\n")


if __name__ == "__main__":
    main()
