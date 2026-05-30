"""Collector runner and orchestration CLI."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from collectors.audit import CollectorAuditLogger
from collectors.factory import create_collector
from collectors.models import RunSummary, SourceDefinition, SourceType
from collectors.registry import load_source_registry
from collectors.state import CollectionStateStore
from collectors.utils import now_utc_iso

logger = logging.getLogger(__name__)


class CollectionRunner:
    """Run enabled collectors and optionally trigger ingestion pipelines."""

    def __init__(
        self,
        registry_path: Path = Path("config/source_registry.yaml"),
        raw_root: Path = Path("data/raw"),
        state_path: Path = Path("data/state/collectors_state.json"),
        audit_root: Path = Path("data/audit/collectors"),
    ) -> None:
        self.registry_path = Path(registry_path)
        self.raw_root = Path(raw_root)
        self.state_store = CollectionStateStore(state_path)
        self.audit_root = Path(audit_root)

    def _trigger_ingestion(self, source_type: SourceType) -> None:
        if source_type == "pubmed":
            from pipelines.ingest_pubmed import PubMedIngestor

            PubMedIngestor().run()
        elif source_type == "website":
            from pipelines.ingest_websites import WebsiteIngestor

            WebsiteIngestor().run()
        elif source_type == "youtube":
            from pipelines.ingest_youtube import YouTubeIngestor

            YouTubeIngestor().run()
        elif source_type == "skool_community":
            from pipelines.ingest_skool_community import SkoolCommunityIngestor

            SkoolCommunityIngestor().run()
        elif source_type == "forum":
            from pipelines.ingest_forums import ForumIngestor

            ForumIngestor().run()

    def _filter_sources(
        self,
        sources: list[SourceDefinition],
        source_type: SourceType | None,
        source_id: str | None,
    ) -> list[SourceDefinition]:
        selected = [source for source in sources if source.enabled]
        if source_type is not None:
            selected = [source for source in selected if source.type == source_type]
        if source_id is not None:
            selected = [source for source in selected if source.id == source_id]
        return selected

    def run(
        self,
        source_type: SourceType | None = None,
        source_id: str | None = None,
        trigger_ingestion: bool = True,
    ) -> RunSummary:
        run_id = now_utc_iso().replace(":", "-")
        audit = CollectorAuditLogger(run_id=run_id, root_dir=self.audit_root)

        registry = load_source_registry(self.registry_path)
        selected_sources = self._filter_sources(registry.sources, source_type, source_id)
        summary = RunSummary(total_sources=len(selected_sources))

        audit.log(
            "run_started",
            {
                "total_sources": len(selected_sources),
                "source_type_filter": source_type,
                "source_id_filter": source_id,
            },
        )

        for source in selected_sources:
            collector = create_collector(source.type, raw_root=self.raw_root)
            result = collector.collect(source)

            changed_artifacts = [
                artifact
                for artifact in result.artifacts
                if self.state_store.has_artifact_changed(source.id, artifact)
            ]

            if changed_artifacts:
                summary.changed_sources += 1
                self.state_store.update_source(source.id, result.artifacts)
                if trigger_ingestion and result.success:
                    try:
                        self._trigger_ingestion(source.type)
                        result.triggered_ingestion = True
                    except Exception as exc:
                        result.success = False
                        result.errors.append(f"Ingestion trigger failed: {exc}")

            if result.success:
                summary.successful_sources += 1
            else:
                summary.failed_sources += 1

            summary.results.append(result)
            audit.log(
                "source_collected",
                {
                    "source_id": source.id,
                    "source_type": source.type,
                    "success": result.success,
                    "records_collected": result.records_collected,
                    "changed_artifacts": [artifact.path for artifact in changed_artifacts],
                    "errors": result.errors,
                },
            )

        self.state_store.save()
        audit.log("run_finished", summary.model_dump())
        return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run approved-source collectors")
    parser.add_argument("--registry", default="config/source_registry.yaml", help="Path to source registry YAML")
    parser.add_argument("--source-type", choices=["pubmed", "website", "youtube", "skool_community", "forum"])
    parser.add_argument("--source-id", help="Run a specific source by id")
    parser.add_argument("--no-trigger-ingestion", action="store_true", help="Do not run ingestion pipelines")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()

    runner = CollectionRunner(registry_path=Path(args.registry))
    summary = runner.run(
        source_type=args.source_type,
        source_id=args.source_id,
        trigger_ingestion=not args.no_trigger_ingestion,
    )
    print(json.dumps(summary.model_dump(), indent=2))


if __name__ == "__main__":
    main()
