"""Tests for the autonomous site crawler.

Network and Playwright are not used; the crawl loop is exercised against an
in-memory fake site by monkeypatching the fetch method and the SSRF guard.
The SSRF guard itself is tested directly against known-bad URLs.
"""

from __future__ import annotations

import pipelines.site_crawler as sc
from pipelines.site_crawler import CrawlConfig, SiteCrawler, _looks_like_attachment, is_safe_url


class TestSSRFGuard:
    def test_rejects_loopback(self) -> None:
        assert is_safe_url("http://127.0.0.1/x") is False
        assert is_safe_url("http://localhost/x") is False

    def test_rejects_private_ranges(self) -> None:
        assert is_safe_url("http://10.0.0.1/") is False
        assert is_safe_url("http://192.168.1.1/") is False

    def test_rejects_cloud_metadata(self) -> None:
        assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False

    def test_rejects_non_http_schemes(self) -> None:
        assert is_safe_url("file:///etc/passwd") is False
        assert is_safe_url("ftp://example.com") is False


class TestLinkExtraction:
    def test_resolves_relative_and_absolute(self) -> None:
        html = '<a href="/a">A</a><a href="https://example.com/b">B</a>'
        links = SiteCrawler.extract_links("https://example.com/", html)
        assert "https://example.com/a" in links
        assert "https://example.com/b" in links

    def test_ignores_anchors_and_images(self) -> None:
        html = '<a href="#frag">x</a><img src="/logo.png">'
        links = SiteCrawler.extract_links("https://example.com/", html)
        # Fragment-only resolves to the page itself; no image links.
        assert all("logo.png" not in link for link in links)


class TestAttachmentDetection:
    def test_documents_detected(self) -> None:
        assert _looks_like_attachment("https://x.com/a/protocol.pdf")
        assert _looks_like_attachment("https://x.com/a/notes.docx")

    def test_pages_not_attachments(self) -> None:
        assert not _looks_like_attachment("https://x.com/about")


class TestCrawlLoop:
    SITE = {
        "https://example.com": '<a href="/a">A</a><a href="/b">B</a><a href="/g.pdf">P</a>',
        "https://example.com/a": '<a href="/c">C</a><a href="/">home</a>',
        "https://example.com/b": '<a href="/a">A</a>',
        "https://example.com/c": '<a href="https://external.com/x">ext</a>',
    }

    def _crawler(self, monkeypatch, **cfg_kwargs) -> SiteCrawler:
        monkeypatch.setattr(sc, "is_safe_url", lambda u: True)

        def fake_fetch(self, url, client):
            key = url if url == "https://example.com" else url.rstrip("/")
            return self.__class__._SITE.get(key)

        SiteCrawler._SITE = self.SITE  # type: ignore[attr-defined]
        monkeypatch.setattr(SiteCrawler, "_fetch_httpx", fake_fetch)
        cfg = CrawlConfig(
            seed_url="https://example.com", request_delay=0,
            respect_robots=False, **cfg_kwargs,
        )
        return SiteCrawler(cfg)

    def test_discovers_all_in_domain_pages(self, monkeypatch) -> None:
        res = self._crawler(monkeypatch).crawl()
        visited = {v.rstrip("/") for v in res.visited}
        assert "https://example.com" in {v if v else "https://example.com" for v in visited} or \
               "https://example.com" in res.visited
        assert "https://example.com/a" in res.visited
        assert "https://example.com/c" in res.visited

    def test_collects_attachments(self, monkeypatch) -> None:
        res = self._crawler(monkeypatch).crawl()
        assert "https://example.com/g.pdf" in res.attachments

    def test_stays_on_domain(self, monkeypatch) -> None:
        res = self._crawler(monkeypatch).crawl()
        assert not any("external.com" in v for v in res.visited)

    def test_respects_max_pages(self, monkeypatch) -> None:
        res = self._crawler(monkeypatch, max_pages=2).crawl()
        assert len(res.visited) <= 2

    def test_unsafe_seed_returns_error(self, monkeypatch) -> None:
        monkeypatch.setattr(sc, "is_safe_url", lambda u: False)
        cfg = CrawlConfig(seed_url="http://10.0.0.1", respect_robots=False)
        res = SiteCrawler(cfg).crawl()
        assert res.errors
        assert not res.pages
