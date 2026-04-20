"""Orchestrator for running all ingestion pipelines in sequence.

Runs all ingestors, captures results, and prints a summary table.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class RunAllIngestion:
    """Runs all configured ingestors sequentially with error capture."""

    def run(self) -> dict:
        """Execute all ingestion pipelines and return a summary dict.

        Returns:
            Summary dictionary with per-source results and totals.
        """
        from pipelines.ingest_documents import DocumentIngestor
        from pipelines.ingest_websites import WebsiteIngestor
        from pipelines.ingest_youtube import YouTubeIngestor
        from pipelines.ingest_skool_courses import SkoolCourseIngestor
        from pipelines.ingest_skool_community import SkoolCommunityIngestor
        from pipelines.ingest_forums import ForumIngestor
        from pipelines.ingest_pubmed import PubMedIngestor

        ingestors = [
            ("pubmed", PubMedIngestor()),
            ("documents", DocumentIngestor()),
            ("websites", WebsiteIngestor()),
            ("youtube", YouTubeIngestor()),
            ("skool_courses", SkoolCourseIngestor()),
            ("skool_community", SkoolCommunityIngestor()),
            ("forums", ForumIngestor()),
        ]

        results: dict = {}
        total_count = 0
        total_start = time.time()

        for name, ingestor in ingestors:
            logger.info("Running ingestor: %s", name)
            try:
                result = ingestor.run()
                results[name] = {
                    "count": result.count,
                    "errors": result.errors,
                    "duration_seconds": round(result.duration_seconds, 2),
                    "status": "ok" if result.success else "error",
                }
                total_count += result.count
            except Exception as exc:
                logger.error("Ingestor %s failed: %s", name, exc)
                results[name] = {"count": 0, "errors": [str(exc)], "status": "failed"}

        total_duration = round(time.time() - total_start, 2)
        summary = {
            "total_chunks": total_count,
            "total_duration_seconds": total_duration,
            "ingestors": results,
        }

        self._print_summary(summary)
        return summary

    @staticmethod
    def _print_summary(summary: dict) -> None:
        """Print a formatted summary table to stdout."""
        print("\n" + "═" * 60)
        print("  REGENOVA-Intel Ingestion Summary")
        print("═" * 60)
        for name, result in summary["ingestors"].items():
            status = "✓" if result["status"] == "ok" else "✗"
            print(f"  {status} {name:<20} {result['count']:>5} chunks  {result['duration_seconds']:>5.1f}s")
        print("─" * 60)
        print(f"  Total: {summary['total_chunks']} chunks | {summary['total_duration_seconds']:.1f}s")
        print("═" * 60 + "\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    RunAllIngestion().run()


if __name__ == "__main__":
    main()
