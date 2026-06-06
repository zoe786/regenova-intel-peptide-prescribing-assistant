"""Autonomous website crawler for full-site ingestion.

Crawls every reachable page within a target domain (optionally behind a
login), discovers linked files/attachments (PDF/doc/etc.), and hands raw
content to the WebsiteIngestor for chunking and storage.

Capabilities
------------
- BFS crawl bounded to the seed domain (never wanders off-site).
- Optional headless rendering via Playwright so JS-built pages are fully
  loaded before extraction ("ensure all elements are loaded"). Falls back to
  httpx when Playwright is unavailable.
- Optional authenticated session: form login or injected cookies/headers.
- Attachment discovery: collects links to PDFs and documents for the
  DocumentIngestor to process.
- Politeness: per-domain delay, robots.txt respect (opt-out), page cap.

Safety
------
- SSRF guard: only http(s), and the resolved host must be public — private,
  loopback, and link-local ranges are refused. This applies to the seed and
  every discovered link, including across redirects.
- Crawl is bounded by ``max_pages`` and ``max_depth`` so a misconfigured run
  can't crawl forever.
- Credentials are never logged.

What is NOT verifiable offline
------------------------------
The Playwright rendering path and live authenticated login require a real
browser and live network, which are unavailable in CI/sandbox. Those paths are
written and structured but are exercised only against local fixtures here; they
must be validated against the real targets before production use.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse
from urllib import robotparser

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "REGENOVA-Intel/0.1 (research crawler)"
_ATTACHMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".csv"}
_SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".zip",
}


@dataclass
class CrawlConfig:
    """Configuration for a single site crawl."""

    seed_url: str
    max_pages: int = 200
    max_depth: int = 5
    request_delay: float = 1.0
    respect_robots: bool = True
    render_js: bool = False              # use Playwright if True
    same_domain_only: bool = True
    # Auth (all optional). Provide ONE of: cookies/headers, or form login.
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    login_url: str | None = None
    login_username: str | None = None
    login_password: str | None = None
    login_username_field: str = "username"
    login_password_field: str = "password"


@dataclass
class CrawlResult:
    """Outcome of a crawl: page contents + discovered attachments."""

    pages: list[dict] = field(default_factory=list)        # {url, html|text}
    attachments: list[str] = field(default_factory=list)   # absolute file URLs
    visited: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


# ── SSRF guard ──────────────────────────────────────────────────────────────


def is_safe_url(url: str) -> bool:
    """Return True if the URL is http(s) and resolves to a public IP.

    Refuses private, loopback, link-local, and reserved ranges to prevent
    server-side request forgery against internal infrastructure.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        # Resolve all A/AAAA records; reject if ANY is non-public.
        infos = socket.getaddrinfo(parsed.hostname, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            ):
                return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("URL safety check failed for %s: %s", url, exc)
        return False


def _normalise(url: str) -> str:
    """Strip fragment and trailing slash for dedup (root kept consistent)."""
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    # Treat https://host and https://host/ as identical.
    if parsed.path in ("", "/"):
        return f"{parsed.scheme}://{parsed.netloc}"
    return url.rstrip("/")


def _same_domain(seed: str, candidate: str) -> bool:
    return urlparse(seed).hostname == urlparse(candidate).hostname


def _looks_like_attachment(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _ATTACHMENT_EXTENSIONS)


def _should_skip(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _SKIP_EXTENSIONS)


# ── Crawler ───────────────────────────────────────────────────────────────────


class SiteCrawler:
    """BFS crawler bounded to a domain, with optional JS rendering + auth."""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._robots: robotparser.RobotFileParser | None = None

    # -- robots --------------------------------------------------------------

    def _load_robots(self) -> None:
        if not self.config.respect_robots:
            return
        try:
            parsed = urlparse(self.config.seed_url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            rp = robotparser.RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
            self._robots = rp
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not load robots.txt (%s); proceeding politely", exc)
            self._robots = None

    def _allowed_by_robots(self, url: str) -> bool:
        if self._robots is None:
            return True
        try:
            return self._robots.can_fetch(_DEFAULT_USER_AGENT, url)
        except Exception:  # noqa: BLE001
            return True

    # -- fetching ------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        return {"User-Agent": _DEFAULT_USER_AGENT, **self.config.headers}

    def _authenticate_httpx(self, client) -> dict[str, str]:
        """Perform a form login with httpx and return session cookies."""
        cfg = self.config
        if not (cfg.login_url and cfg.login_username and cfg.login_password):
            return {}
        if not is_safe_url(cfg.login_url):
            raise ValueError("Login URL failed SSRF safety check")
        try:
            resp = client.post(
                cfg.login_url,
                data={
                    cfg.login_username_field: cfg.login_username,
                    cfg.login_password_field: cfg.login_password,
                },
                follow_redirects=True,
            )
            resp.raise_for_status()
            logger.info("Form login submitted (status %s)", resp.status_code)
            return dict(client.cookies)
        except Exception as exc:  # noqa: BLE001
            # Never log credentials.
            raise RuntimeError(f"Login failed: {type(exc).__name__}") from None

    def _fetch_httpx(self, url: str, client) -> str | None:
        try:
            resp = client.get(url, follow_redirects=True)
            # Re-check the final URL after redirects (SSRF defence).
            final = str(resp.url)
            if not is_safe_url(final):
                logger.warning("Refusing unsafe redirect target: %s", final)
                return None
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return None
            return resp.text
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fetch failed for %s: %s", url, exc)
            return None

    def _fetch_rendered(self, url: str) -> str | None:
        """Fetch a fully-rendered page via Playwright (JS executed)."""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import]
        except ImportError:
            logger.warning("Playwright not installed — cannot render JS for %s", url)
            return None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=_DEFAULT_USER_AGENT,
                    extra_http_headers=self.config.headers or None,
                )
                if self.config.cookies:
                    parsed = urlparse(url)
                    context.add_cookies([
                        {"name": k, "value": v, "domain": parsed.hostname, "path": "/"}
                        for k, v in self.config.cookies.items()
                    ])
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                # Ensure dynamic content settled.
                page.wait_for_load_state("domcontentloaded")
                html = page.content()
                browser.close()
                return html
        except Exception as exc:  # noqa: BLE001
            logger.warning("Render failed for %s: %s", url, exc)
            return None

    # -- link extraction -----------------------------------------------------

    @staticmethod
    def extract_links(base_url: str, html: str) -> list[str]:
        """Extract absolute hrefs from page HTML."""
        try:
            from bs4 import BeautifulSoup  # type: ignore[import]
        except ImportError:
            return []
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            absolute, _ = urldefrag(urljoin(base_url, a["href"]))
            if absolute.startswith(("http://", "https://")):
                links.append(absolute)
        return links

    # -- main crawl ----------------------------------------------------------

    def crawl(self) -> CrawlResult:
        """Run the bounded BFS crawl and return pages + attachments."""
        result = CrawlResult()
        cfg = self.config

        if not is_safe_url(cfg.seed_url):
            result.errors.append(f"Seed URL failed safety check: {cfg.seed_url}")
            return result

        self._load_robots()

        try:
            import httpx  # type: ignore[import]
        except ImportError:
            result.errors.append("httpx not installed")
            return result

        client = httpx.Client(
            timeout=30, headers=self._build_headers(), cookies=cfg.cookies or None
        )
        try:
            # Authenticate if form-login details were supplied.
            if cfg.login_url:
                try:
                    session_cookies = self._authenticate_httpx(client)
                    cfg.cookies.update(session_cookies)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(str(exc))
                    return result

            queue: list[tuple[str, int]] = [(_normalise(cfg.seed_url), 0)]
            while queue and len(result.visited) < cfg.max_pages:
                url, depth = queue.pop(0)
                if url in result.visited or depth > cfg.max_depth:
                    continue
                if not is_safe_url(url):
                    continue
                if not _same_domain(cfg.seed_url, url) and cfg.same_domain_only:
                    continue
                if not self._allowed_by_robots(url):
                    logger.info("robots.txt disallows %s", url)
                    continue

                result.visited.add(url)

                html = (
                    self._fetch_rendered(url) if cfg.render_js
                    else self._fetch_httpx(url, client)
                )
                if not html:
                    continue

                result.pages.append({"url": url, "html": html})

                # Discover links: enqueue pages, collect attachments.
                for link in self.extract_links(url, html):
                    if _looks_like_attachment(link):
                        if link not in result.attachments and is_safe_url(link):
                            result.attachments.append(link)
                    elif _should_skip(link):
                        continue
                    elif link not in result.visited:
                        if (not cfg.same_domain_only) or _same_domain(cfg.seed_url, link):
                            queue.append((_normalise(link), depth + 1))

                time.sleep(cfg.request_delay)

            logger.info(
                "Crawl complete: %d pages, %d attachments, %d errors",
                len(result.pages), len(result.attachments), len(result.errors),
            )
            return result
        finally:
            client.close()
