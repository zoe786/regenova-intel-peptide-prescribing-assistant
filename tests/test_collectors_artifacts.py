from __future__ import annotations

import json
from pathlib import Path

from collectors.factory import ForumCollector, PubMedCollector, WebsiteCollector, YouTubeCollector
from collectors.models import SourceDefinition


def test_website_collector_writes_source_specific_artifact(tmp_path: Path) -> None:
    collector = WebsiteCollector(raw_root=tmp_path / "raw")
    source = SourceDefinition(
        id="trusted-web",
        type="website",
        config={"urls": ["https://example.com/a", "https://example.com/b"]},
    )

    result = collector.collect(source)

    output_file = tmp_path / "raw" / "websites" / "urls_trusted-web.txt"
    assert result.success
    assert output_file.exists()
    assert "https://example.com/a" in output_file.read_text(encoding="utf-8")


def test_pubmed_collector_writes_pmids_without_network(tmp_path: Path, monkeypatch) -> None:
    collector = PubMedCollector(raw_root=tmp_path / "raw")

    monkeypatch.setattr(collector, "_search_pmids", lambda *args, **kwargs: ["111", "222"])
    source = SourceDefinition(
        id="pubmed-core",
        type="pubmed",
        config={"queries": ["bpc-157"], "pmids": ["333"]},
    )

    result = collector.collect(source)

    output_file = tmp_path / "raw" / "pubmed" / "pmids_pubmed-core.txt"
    assert result.success
    assert result.records_collected == 3
    assert output_file.exists()


def test_youtube_collector_writes_video_ids_file(tmp_path: Path) -> None:
    collector = YouTubeCollector(raw_root=tmp_path / "raw")
    source = SourceDefinition(
        id="yt-approved",
        type="youtube",
        config={"video_ids": ["abc123", "def456"]},
    )

    result = collector.collect(source)

    output_file = tmp_path / "raw" / "youtube" / "video_ids_yt-approved.txt"
    assert result.success
    assert output_file.exists()
    assert "abc123" in output_file.read_text(encoding="utf-8")


def test_forum_collector_writes_canonical_json(tmp_path: Path) -> None:
    collector = ForumCollector(raw_root=tmp_path / "raw")
    source = SourceDefinition(
        id="forum-approved",
        type="forum",
        config={
            "threads": [
                {
                    "thread_id": "t1",
                    "title": "Thread 1",
                    "posts": [{"author": "alice", "content": "hello"}],
                }
            ]
        },
    )

    result = collector.collect(source)

    output_file = tmp_path / "raw" / "forums" / "forum-approved.json"
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    assert result.success
    assert payload["source_id"] == "forum-approved"
    assert payload["threads"][0]["thread_id"] == "t1"
