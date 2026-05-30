from __future__ import annotations

from pathlib import Path

from pipelines.ingest_pubmed import PubMedIngestor
from pipelines.ingest_websites import WebsiteIngestor
from pipelines.ingest_youtube import YouTubeIngestor


def test_website_ingestor_reports_invalid_urls(tmp_path: Path, monkeypatch) -> None:
    raw_dir = tmp_path / "websites"
    raw_dir.mkdir(parents=True)
    (raw_dir / "urls.txt").write_text(
        "not-a-url\nhttps://example.com/page\nhttps://example.com/page\n",
        encoding="utf-8",
    )

    ingestor = WebsiteIngestor(raw_dir=raw_dir)
    monkeypatch.setattr(ingestor, "_fetch_url", lambda _url: "<html><body>hello</body></html>")
    docs = ingestor.load_raw()

    assert len(docs) == 1
    assert any("Invalid website URL" in msg for msg in ingestor.load_errors)


def test_youtube_ingestor_reports_invalid_ids(tmp_path: Path) -> None:
    raw_dir = tmp_path / "youtube"
    raw_dir.mkdir(parents=True)
    (raw_dir / "video_ids.txt").write_text("bad id\nhttps://youtu.be/dQw4w9WgXcQ\n", encoding="utf-8")

    ingestor = YouTubeIngestor(raw_dir=raw_dir)
    video_docs = ingestor.load_raw()

    assert video_docs == []
    assert any("Invalid YouTube ID/URL" in msg for msg in ingestor.load_errors)


def test_pubmed_ingestor_reports_invalid_pmids(tmp_path: Path) -> None:
    raw_dir = tmp_path / "pubmed"
    raw_dir.mkdir(parents=True)
    (raw_dir / "pmids.txt").write_text("abc\nhttps://pubmed.ncbi.nlm.nih.gov/12345678/\n", encoding="utf-8")

    ingestor = PubMedIngestor(raw_dir=raw_dir)
    ingestor._fetch_abstracts = lambda pmids: []  # type: ignore[method-assign]
    docs = ingestor.load_raw()

    assert docs == []
    assert any("Invalid PMID" in msg for msg in ingestor.load_errors)
