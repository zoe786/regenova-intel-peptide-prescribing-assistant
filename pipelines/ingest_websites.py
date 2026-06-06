"""Website ingestor — fetches URLs and extracts text content.

Reads URL list from data/raw/websites/urls.txt, fetches with httpx,
parses with BeautifulSoup, cleans, and chunks (evidence_tier_default=3).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from pipelines.common.chunking import chunk_by_tokens
from pipelines.common.cleaners import (
    clean_html,
    normalize_whitespace,
    remove_boilerplate,
)
from pipelines.common.metadata_enrichment import (
    compute_content_hash,
    generate_document_id,
)
from pipelines.common.models import IngestionResult, NormalizedRecord, RawDocument
from pipelines.common.storage import save_normalized, save_to_vector_store

logger = logging.getLogger(__name__)

DEFAULT_EVIDENCE_TIER = 3
SOURCE_TYPE = "website"
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 1.0  # polite crawl delay in seconds
AUTH_CONFIG_FILENAME = "auth.json"


def _normalise_domain(hostname: str | None) -> str:
    if not hostname:
        return ""
    return hostname.lower().strip().removeprefix("www.")


def _safe_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k.strip()] = v.strip()
    return out


def _load_auth_config(raw_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    """Load optional auth profiles for login-protected sources.

    Supports:
    - data/raw/websites/auth.json
    - WEBSITE_INGEST_AUTH_JSON env var (JSON string)
    """
    config: dict[str, object] = {}
    auth_file = raw_dir / AUTH_CONFIG_FILENAME

    if auth_file.exists():
        try:
            config = json.loads(auth_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Invalid website auth config file %s: %s", auth_file, exc)

    env_json = os.getenv("WEBSITE_INGEST_AUTH_JSON")
    if env_json:
        try:
            config = json.loads(env_json)
        except Exception as exc:
            logger.warning("Invalid WEBSITE_INGEST_AUTH_JSON payload: %s", exc)

    domains_raw = config.get("domains", {}) if isinstance(config, dict) else {}
    parsed_domains: dict[str, dict[str, dict[str, str]]] = {}
    if isinstance(domains_raw, dict):
        for domain, profile in domains_raw.items():
            if not isinstance(domain, str) or not isinstance(profile, dict):
                continue
            parsed_domains[_normalise_domain(domain)] = {
                "headers": _safe_dict(profile.get("headers")),
                "cookies": _safe_dict(profile.get("cookies")),
            }
    return parsed_domains


class WebsiteIngestor:
    """Ingestor for web pages specified in a URL list file."""

    def __init__(
        self,
        raw_dir: Path = Path("data/raw/websites"),
        output_dir: Path = Path("data/processed/normalized"),
        chroma_persist_dir: str = "./data/chroma_db",
        max_tokens_per_chunk: int = 512,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.output_dir = Path(output_dir)
        self.chroma_persist_dir = chroma_persist_dir
        self.max_tokens = max_tokens_per_chunk
        self.urls_file = self.raw_dir / "urls.txt"
        self.auth_profiles = _load_auth_config(self.raw_dir)

    def _fetch_url(self, url: str) -> str | None:
        """Fetch a URL and return the HTML content."""
        try:
            import httpx  # type: ignore[import]
            parsed = urlparse(url)
            host = _normalise_domain(parsed.hostname)
            auth_profile = self.auth_profiles.get(host, {})
            headers = {
                "User-Agent": "REGENOVA-Intel/0.1 (research bot)",
                **auth_profile.get("headers", {}),
            }
            cookies = auth_profile.get("cookies", {})
            with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
                response = client.get(url, headers=headers, cookies=cookies)
                response.raise_for_status()
                return response.text
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", url, exc)
            return None

    def load_raw(self) -> list[RawDocument]:
        """Read URL list and fetch each page."""
        if not self.urls_file.exists():
            logger.warning("URL list file not found: %s", self.urls_file)
            return []

        urls = [
            line.strip() for line in self.urls_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        logger.info("WebsiteIngestor: found %d URLs", len(urls))

        docs: list[RawDocument] = []
        for url in urls:
            html = self._fetch_url(url)
            if not html:
                continue
            text = clean_html(html)
            if not text.strip():
                continue
            docs.append(RawDocument(
                source_type=SOURCE_TYPE,
                source_name=url.split("/")[2],  # domain as name
                raw_content=text,
                acquired_at=datetime.utcnow(),
                source_url=url,
                evidence_tier_default=DEFAULT_EVIDENCE_TIER,
            ))
            time.sleep(REQUEST_DELAY)

        return docs

    def process(self, docs: list[RawDocument]) -> IngestionResult:
        """Clean, chunk, and store website content."""
        result = IngestionResult(source_type=SOURCE_TYPE)
        records: list[NormalizedRecord] = []

        for doc in docs:
            try:
                clean_text = normalize_whitespace(remove_boilerplate(doc.raw_content))
                if not clean_text:
                    result.skipped += 1
                    continue

                chunks = chunk_by_tokens(clean_text, self.max_tokens)
                document_id = generate_document_id(doc.source_url, doc.acquired_at, doc.source_name)

                for idx, chunk_text in enumerate(chunks):
                    record = NormalizedRecord(
                        chunk_id=f"{document_id}_{idx:04d}",
                        document_id=document_id,
                        source_type=SOURCE_TYPE,
                        source_name=doc.source_name,
                        source_url=doc.source_url,
                        acquired_at=doc.acquired_at,
                        evidence_tier_default=doc.evidence_tier_default,
                        content_hash=compute_content_hash(chunk_text),
                        content=chunk_text,
                        chunk_index=idx,
                        extra_metadata=dict(doc.extra_metadata or {}),
                    )
                    save_normalized(record, self.output_dir)
                    records.append(record)
                    result.count += 1
            except Exception as exc:
                logger.error("Error processing %s: %s", doc.source_url, exc)
                result.errors.append(str(exc))

        if records:
            save_to_vector_store(records, chroma_persist_dir=self.chroma_persist_dir)
        return result

    def run(self) -> IngestionResult:
        start = time.time()
        docs = self.load_raw()
        result = self.process(docs)
        result.duration_seconds = time.time() - start
        logger.info("%s", result)
        return result

    def run_autonomous(
        self,
        seed_url: str,
        evidence_tier: int = DEFAULT_EVIDENCE_TIER,
        render_js: bool = False,
        max_pages: int = 200,
        cookies: dict[str, str] | None = None,
        login_url: str | None = None,
        login_username: str | None = None,
        login_password: str | None = None,
        ingest_attachments: bool = True,
    ) -> IngestionResult:
        """Crawl an entire site (optionally behind login) and ingest it.

        Every reachable in-domain page is scraped and ingested. Discovered
        document attachments (PDF/DOC/TXT) are downloaded and handed to the
        DocumentIngestor. JS-rendered sites can be fully loaded via Playwright
        by setting ``render_js=True``.

        Args:
            seed_url: Starting URL; the crawl stays within this domain.
            evidence_tier: Tier for all pages from this site. Commercial /
                community sites should use 4-5; this is the caller's
                responsibility and the value is recorded in provenance.
            render_js: Use a headless browser so dynamic content loads first.
            max_pages: Hard cap on pages crawled.
            cookies / login_*: Optional authentication.
            ingest_attachments: Download + ingest linked documents.

        Returns:
            IngestionResult covering pages (and attachments, if enabled).
        """
        from pipelines.site_crawler import CrawlConfig, SiteCrawler

        start = time.time()
        config = CrawlConfig(
            seed_url=seed_url,
            max_pages=max_pages,
            render_js=render_js,
            cookies=cookies or {},
            login_url=login_url,
            login_username=login_username,
            login_password=login_password,
        )
        crawl = SiteCrawler(config).crawl()

        domain = urlparse(seed_url).hostname or seed_url
        docs: list[RawDocument] = []
        for page in crawl.pages:
            text = clean_html(page["html"])
            if not text.strip():
                continue
            docs.append(RawDocument(
                source_type=SOURCE_TYPE,
                source_name=domain,
                raw_content=text,
                acquired_at=datetime.utcnow(),
                source_url=page["url"],
                evidence_tier_default=evidence_tier,
                extra_metadata={
                    "crawl_seed": seed_url,
                    "site_domain": domain,
                    "page_url": page["url"],
                    "rendered_js": render_js,
                    "authenticated": bool(login_url or cookies),
                },
            ))

        result = self.process(docs)
        for err in crawl.errors:
            result.errors.append(err)

        # Ingest discovered attachments through the DocumentIngestor.
        if ingest_attachments and crawl.attachments:
            attach_result = self._ingest_attachments(crawl.attachments, config)
            result.count += attach_result.count
            result.errors.extend(attach_result.errors)
            result.quarantined_documents.extend(attach_result.quarantined_documents)

        result.duration_seconds = time.time() - start
        logger.info("%s (autonomous site=%s, %d pages, %d attachments)",
                    result, domain, len(crawl.pages), len(crawl.attachments))
        return result

    def _ingest_attachments(self, urls: list[str], config) -> IngestionResult:
        """Download attachment URLs to a temp dir and run DocumentIngestor."""
        import tempfile
        from pipelines.site_crawler import is_safe_url

        result = IngestionResult(source_type="document")
        try:
            import httpx  # type: ignore[import]
        except ImportError:
            result.errors.append("httpx not installed for attachment download")
            return result

        tmpdir = Path(tempfile.mkdtemp(prefix="regenova_attach_"))
        client = httpx.Client(timeout=60, cookies=config.cookies or None,
                              headers={"User-Agent": "REGENOVA-Intel/0.1"})
        downloaded = 0
        try:
            for url in urls:
                if not is_safe_url(url):
                    continue
                try:
                    resp = client.get(url, follow_redirects=True)
                    if not is_safe_url(str(resp.url)):
                        continue
                    resp.raise_for_status()
                    name = Path(urlparse(url).path).name or f"file_{downloaded}"
                    # Only keep the document types DocumentIngestor supports.
                    if Path(name).suffix.lower() not in {".pdf", ".txt", ".md"}:
                        continue
                    (tmpdir / name).write_bytes(resp.content)
                    downloaded += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"Attachment download failed: {type(exc).__name__}")
            if downloaded:
                from pipelines.ingest_documents import DocumentIngestor
                doc_result = DocumentIngestor(
                    raw_dir=tmpdir,
                    output_dir=self.output_dir,
                    chroma_persist_dir=self.chroma_persist_dir,
                ).run()
                result.count = doc_result.count
                result.errors.extend(doc_result.errors)
                result.quarantined_documents.extend(doc_result.quarantined_documents)
            logger.info("Ingested %d/%d attachments", downloaded, len(urls))
            return result
        finally:
            client.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(WebsiteIngestor().run())


if __name__ == "__main__":
    main()
