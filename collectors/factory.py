"""Collector implementations and factory."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from collectors.base import BaseCollector
from collectors.models import CollectionResult, SourceDefinition, SourceType

logger = logging.getLogger(__name__)


class PubMedCollector(BaseCollector):
    """Collect PubMed IDs from approved queries and write raw PMID files."""

    def _search_pmids(self, query: str, max_results: int, email: str, api_key: str) -> list[str]:
        try:
            from Bio import Entrez  # type: ignore[import]

            Entrez.email = email
            if api_key:
                Entrez.api_key = api_key
            handle = Entrez.esearch(db="pubmed", term=query, retmax=max_results, sort="relevance")
            record = Entrez.read(handle)
            handle.close()
            ids = record.get("IdList", [])
            return [str(item).strip() for item in ids if str(item).strip()]
        except Exception as exc:
            logger.warning("PubMed query failed for '%s': %s", query, exc)
            return []

    def collect(self, source: SourceDefinition) -> CollectionResult:
        result = CollectionResult(source_id=source.id, source_type=source.type)
        config = source.config
        max_results = int(config.get("max_results_per_query", 25))
        email_env = str(config.get("email_env", "PUBMED_EMAIL"))
        api_key_env = str(config.get("api_key_env", "PUBMED_API_KEY"))
        email = os.getenv(email_env, "research@regenova-intel.example.com")
        api_key = os.getenv(api_key_env, "")

        pmids = [str(v) for v in config.get("pmids", []) if str(v).strip()]
        for query in config.get("queries", []):
            if not str(query).strip():
                continue
            pmids.extend(self._search_pmids(str(query), max_results, email, api_key))

        unique_pmids = self.dedupe_lines(pmids)
        if not unique_pmids:
            result.success = False
            result.errors.append("No PMIDs collected from configured queries/pmids")
            return result

        artifact = self.write_text_artifact(
            Path("pubmed") / f"pmids_{source.id}.txt",
            "\n".join(unique_pmids) + "\n",
            record_count=len(unique_pmids),
        )
        result.artifacts.append(artifact)
        result.records_collected = len(unique_pmids)
        return result


class WebsiteCollector(BaseCollector):
    """Collect approved website URLs into raw URL list artifacts."""

    def collect(self, source: SourceDefinition) -> CollectionResult:
        result = CollectionResult(source_id=source.id, source_type=source.type)
        urls = [str(v) for v in source.config.get("urls", []) if str(v).strip()]
        unique_urls = self.dedupe_lines(urls)
        if not unique_urls:
            result.success = False
            result.errors.append("No URLs configured for website source")
            return result

        artifact = self.write_text_artifact(
            Path("websites") / f"urls_{source.id}.txt",
            "\n".join(unique_urls) + "\n",
            record_count=len(unique_urls),
        )
        result.artifacts.append(artifact)
        result.records_collected = len(unique_urls)
        return result


class YouTubeCollector(BaseCollector):
    """Collect approved YouTube video IDs into raw ID list artifacts."""

    def collect(self, source: SourceDefinition) -> CollectionResult:
        result = CollectionResult(source_id=source.id, source_type=source.type)
        ids = [str(v) for v in source.config.get("video_ids", []) if str(v).strip()]
        unique_ids = self.dedupe_lines(ids)
        if not unique_ids:
            result.success = False
            result.errors.append("No video IDs configured for youtube source")
            return result

        artifact = self.write_text_artifact(
            Path("youtube") / f"video_ids_{source.id}.txt",
            "\n".join(unique_ids) + "\n",
            record_count=len(unique_ids),
        )
        result.artifacts.append(artifact)
        result.records_collected = len(unique_ids)
        return result


class SkoolCommunityCollector(BaseCollector):
    """Normalize approved Skool community exports into canonical JSON artifacts."""

    def _load_posts(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        export_file = config.get("export_file")
        if export_file:
            path = Path(str(export_file))
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                posts = payload.get("posts", [payload]) if isinstance(payload, dict) else payload
                if isinstance(posts, list):
                    return [item for item in posts if isinstance(item, dict)]
        posts = config.get("posts", [])
        return [item for item in posts if isinstance(item, dict)]

    def collect(self, source: SourceDefinition) -> CollectionResult:
        result = CollectionResult(source_id=source.id, source_type=source.type)
        posts = self._load_posts(source.config)
        if not posts:
            result.success = False
            result.errors.append("No Skool community posts available from approved export/config")
            return result

        canonical_posts: list[dict[str, Any]] = []
        for post in posts:
            replies = post.get("replies", [])
            normalized_replies = [r for r in replies if isinstance(r, dict)] if isinstance(replies, list) else []
            canonical_posts.append(
                {
                    "post_id": str(post.get("post_id") or post.get("id") or ""),
                    "author": str(post.get("author") or "member"),
                    "content": str(post.get("content") or ""),
                    "created_at": post.get("created_at"),
                    "url": post.get("url"),
                    "replies": normalized_replies,
                }
            )

        payload = {"source_id": source.id, "posts": canonical_posts}
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        artifact = self.write_text_artifact(
            Path("skool") / "community" / f"{source.id}.json",
            content,
            record_count=len(canonical_posts),
        )
        result.artifacts.append(artifact)
        result.records_collected = len(canonical_posts)
        return result


class ForumCollector(BaseCollector):
    """Normalize approved forum exports into canonical JSON artifacts."""

    def _load_threads(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        export_file = config.get("export_file")
        if export_file:
            path = Path(str(export_file))
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                threads = payload.get("threads", [payload]) if isinstance(payload, dict) else payload
                if isinstance(threads, list):
                    return [item for item in threads if isinstance(item, dict)]
        threads = config.get("threads", [])
        return [item for item in threads if isinstance(item, dict)]

    def collect(self, source: SourceDefinition) -> CollectionResult:
        result = CollectionResult(source_id=source.id, source_type=source.type)
        threads = self._load_threads(source.config)
        if not threads:
            result.success = False
            result.errors.append("No forum threads available from approved export/config")
            return result

        canonical_threads: list[dict[str, Any]] = []
        for thread in threads:
            posts = thread.get("posts", [])
            canonical_threads.append(
                {
                    "thread_id": str(thread.get("thread_id") or thread.get("id") or ""),
                    "title": str(thread.get("title") or "Forum Thread"),
                    "url": thread.get("url"),
                    "posts": [p for p in posts if isinstance(p, dict)] if isinstance(posts, list) else [],
                }
            )

        payload = {"source_id": source.id, "threads": canonical_threads}
        content = json.dumps(payload, indent=2, ensure_ascii=False)
        artifact = self.write_text_artifact(
            Path("forums") / f"{source.id}.json",
            content,
            record_count=len(canonical_threads),
        )
        result.artifacts.append(artifact)
        result.records_collected = len(canonical_threads)
        return result


COLLECTOR_MAP: dict[SourceType, type[BaseCollector]] = {
    "pubmed": PubMedCollector,
    "website": WebsiteCollector,
    "youtube": YouTubeCollector,
    "skool_community": SkoolCommunityCollector,
    "forum": ForumCollector,
}


def create_collector(source_type: SourceType, raw_root: Path = Path("data/raw")) -> BaseCollector:
    """Factory that returns collector implementation for a source type."""
    collector_cls = COLLECTOR_MAP[source_type]
    return collector_cls(raw_root=raw_root)
