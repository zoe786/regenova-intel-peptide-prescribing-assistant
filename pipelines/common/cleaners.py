"""Text cleaning functions for the ingestion pipeline.

Provides utilities to clean HTML, normalize whitespace, remove boilerplate
content, and detect document language.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


def clean_html(text: str) -> str:
    """Remove HTML tags and decode HTML entities from text.

    Uses BeautifulSoup if available, falls back to regex-based stripping.

    Args:
        text: Text that may contain HTML markup.

    Returns:
        Plain text with HTML removed.
    """
    if not text:
        return ""

    try:
        from bs4 import BeautifulSoup  # type: ignore[import]
        soup = BeautifulSoup(text, "html.parser")
        # Remove script and style elements
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)
    except ImportError:
        # Fallback: regex-based HTML stripping
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        # Decode common HTML entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&#39;", "'")
        return text


def normalize_whitespace(text: str) -> str:
    """Normalize all whitespace in text to single spaces.

    Converts tabs, multiple spaces, non-breaking spaces, and other
    Unicode whitespace characters to a single ASCII space.

    Args:
        text: Input text with potentially irregular whitespace.

    Returns:
        Text with normalized whitespace, stripped of leading/trailing space.
    """
    if not text:
        return ""

    # Normalize Unicode characters
    text = unicodedata.normalize("NFKC", text)

    # Replace all whitespace variants with single space
    text = re.sub(r"[ \t\u00a0\u200b\ufeff]+", " ", text)

    # Collapse multiple newlines to double newline (paragraph separation)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def remove_boilerplate(text: str) -> str:
    """Remove common boilerplate content from web pages and documents.

    Removes: cookie notices, privacy policy banners, navigation text,
    footer content, and other common boilerplate patterns.

    Args:
        text: Cleaned text that may contain boilerplate.

    Returns:
        Text with boilerplate patterns removed.
    """
    if not text:
        return ""

    boilerplate_patterns = [
        r"(?i)accept all cookies?.*?(?:\n|$)",
        r"(?i)privacy policy.*?(?:\n|$)",
        r"(?i)terms (of service|and conditions).*?(?:\n|$)",
        r"(?i)cookie (policy|notice|settings).*?(?:\n|$)",
        r"(?i)subscribe to our newsletter.*?(?:\n|$)",
        r"(?i)all rights reserved.*?(?:\n|$)",
        r"(?i)copyright \d{4}.*?(?:\n|$)",
        r"(?i)skip to (main )?content.*?(?:\n|$)",
        r"(?i)menu\s*(?:\n|$)",
        r"(?i)^\s*share (this|article|post).*?(?:\n|$)",
    ]

    for pattern in boilerplate_patterns:
        text = re.sub(pattern, "", text, flags=re.MULTILINE)

    return normalize_whitespace(text)


def detect_language(text: str) -> Optional[str]:
    """Detect the primary language of text.

    Returns ISO 639-1 language code (e.g. 'en', 'de') or None if detection fails.

    Args:
        text: Text to detect language for.

    Returns:
        Language code string or None.

    TODO: Integrate langdetect or lingua library for production use.
    """
    if not text or len(text) < 20:
        return None

    # TODO: Implement using langdetect: `from langdetect import detect; return detect(text)`
    # For now, return 'en' as default (all ingested content is assumed English)
    return "en"
