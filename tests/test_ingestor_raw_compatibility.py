from __future__ import annotations

import json
from pathlib import Path

from pipelines.ingest_forums import ForumIngestor
from pipelines.ingest_pubmed import PubMedIngestor
from pipelines.ingest_skool_community import SkoolCommunityIngestor
from pipelines.ingest_websites import WebsiteIngestor
from pipelines.ingest_youtube import YouTubeIngestor


def test_pubmed_ingestor_reads_multiple_pmid_files(tmp_path: Path, monkeypatch) -> None:
    raw_dir = tmp_path / "pubmed"
    raw_dir.mkdir(parents=True)
    (raw_dir / "pmids.txt").write_text("111\n", encoding="utf-8")
    (raw_dir / "pmids_extra.txt").write_text("222\n", encoding="utf-8")

    ingestor = PubMedIngestor(raw_dir=raw_dir)
    monkeypatch.setattr(ingestor, "_setup_entrez", lambda: None)

    captured = {}

    def fake_fetch(pmids):
        captured["pmids"] = pmids
        return []

    monkeypatch.setattr(ingestor, "_fetch_abstracts", fake_fetch)
    ingestor.load_raw()

    assert captured["pmids"] == ["111", "222"]


def test_website_ingestor_reads_multiple_url_files(tmp_path: Path, monkeypatch) -> None:
    raw_dir = tmp_path / "websites"
    raw_dir.mkdir(parents=True)
    (raw_dir / "urls.txt").write_text("https://example.com/a\n", encoding="utf-8")
    (raw_dir / "urls_partner.txt").write_text("https://example.com/b\n", encoding="utf-8")

    ingestor = WebsiteIngestor(raw_dir=raw_dir)
    monkeypatch.setattr(ingestor, "_fetch_url", lambda url: f"<html><body>{url}</body></html>")

    docs = ingestor.load_raw()

    assert len(docs) == 2


def test_youtube_ingestor_reads_multiple_id_files(tmp_path: Path, monkeypatch) -> None:
    raw_dir = tmp_path / "youtube"
    raw_dir.mkdir(parents=True)
    (raw_dir / "video_ids.txt").write_text("abc123\n", encoding="utf-8")
    (raw_dir / "video_ids_extra.txt").write_text("def456\n", encoding="utf-8")

    ingestor = YouTubeIngestor(raw_dir=raw_dir)
    monkeypatch.setattr(ingestor, "_fetch_transcript", lambda _: "transcript")

    docs = ingestor.load_raw()

    assert len(docs) == 2


def test_forum_ingestor_supports_canonical_threads_object(tmp_path: Path) -> None:
    raw_dir = tmp_path / "forums"
    raw_dir.mkdir(parents=True)
    (raw_dir / "canon.json").write_text(
        json.dumps(
            {
                "source_id": "forum-1",
                "threads": [
                    {
                        "thread_id": "t1",
                        "title": "Topic",
                        "posts": [{"author": "alice", "content": "hello"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    docs = ForumIngestor(raw_dir=raw_dir).load_raw()

    assert len(docs) == 1
    assert "Thread: Topic" in docs[0].raw_content


def test_skool_ingestor_supports_canonical_posts_object(tmp_path: Path) -> None:
    raw_dir = tmp_path / "skool" / "community"
    raw_dir.mkdir(parents=True)
    (raw_dir / "canon.json").write_text(
        json.dumps(
            {
                "source_id": "skool-1",
                "posts": [
                    {
                        "post_id": "p1",
                        "author": "mod",
                        "content": "main post",
                        "replies": [{"author": "member", "content": "reply"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    docs = SkoolCommunityIngestor(raw_dir=raw_dir).load_raw()

    assert len(docs) == 1
    assert "main post" in docs[0].raw_content
