"""Text chunking strategies for the ingestion pipeline.

Provides multiple chunking methods with configurable size and overlap.
Token counting uses tiktoken when available, falls back to word count.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_DEFAULT_ENCODING = "cl100k_base"  # Used by GPT-4 / text-embedding-3 models
_tokenizer = None


def _get_tokenizer():
    """Return a cached tiktoken tokenizer, or None if not available."""
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    try:
        import tiktoken  # type: ignore[import]
        _tokenizer = tiktoken.get_encoding(_DEFAULT_ENCODING)
        return _tokenizer
    except (ImportError, Exception) as e:
        logger.debug("tiktoken not available (%s), using word count fallback", e)
        return None


def _count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken or word-count fallback."""
    enc = _get_tokenizer()
    if enc is not None:
        return len(enc.encode(text))
    return len(text.split())


def chunk_by_tokens(
    text: str,
    max_tokens: int = 512,
    overlap: int = 50,
) -> list[str]:
    """Split text into chunks of at most max_tokens tokens with overlap.

    Uses tiktoken for accurate token counting when available,
    falls back to approximate word-based splitting.

    Args:
        text: The full text to chunk.
        max_tokens: Maximum tokens per chunk (default: 512).
        overlap: Number of tokens to overlap between adjacent chunks (default: 50).

    Returns:
        List of text chunks, each within the token limit.
    """
    if not text.strip():
        return []

    enc = _get_tokenizer()

    if enc is not None:
        # Tiktoken-based splitting
        tokens = enc.encode(text)
        chunks: list[str] = []
        start = 0
        while start < len(tokens):
            end = min(start + max_tokens, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text = enc.decode(chunk_tokens)
            if chunk_text.strip():
                chunks.append(chunk_text.strip())
            start = end - overlap if end < len(tokens) else len(tokens)
        return chunks
    else:
        # Word-based fallback
        words = text.split()
        chunks = []
        start = 0
        while start < len(words):
            end = min(start + max_tokens, len(words))
            chunk_text = " ".join(words[start:end])
            if chunk_text.strip():
                chunks.append(chunk_text.strip())
            start = end - overlap if end < len(words) else len(words)
        return chunks


def chunk_by_paragraph(
    text: str,
    max_paragraphs: int = 3,
) -> list[str]:
    """Split text into chunks grouping up to max_paragraphs paragraphs.

    Paragraph boundaries are detected by double-newlines or blank lines.
    Consecutive short paragraphs are merged into single chunks.

    Args:
        text: The full text to chunk.
        max_paragraphs: Maximum number of paragraphs per chunk (default: 3).

    Returns:
        List of text chunks, each containing 1–max_paragraphs paragraphs.
    """
    if not text.strip():
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    if not paragraphs:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    current: list[str] = []

    for para in paragraphs:
        current.append(para)
        if len(current) >= max_paragraphs:
            chunks.append("\n\n".join(current))
            current = []

    if current:
        chunks.append("\n\n".join(current))

    return [c for c in chunks if c.strip()]


def chunk_by_sentence(
    text: str,
    max_sentences: int = 8,
) -> list[str]:
    """Split text into chunks of at most max_sentences sentences.

    Uses regex-based sentence splitting (handles common abbreviations).

    Args:
        text: The full text to chunk.
        max_sentences: Maximum sentences per chunk (default: 8).

    Returns:
        List of text chunks, each containing 1–max_sentences sentences.
    """
    if not text.strip():
        return []

    # Sentence boundary detection: handles Dr., Mr., vs., etc.
    sentence_boundary = re.compile(
        r"(?<!\b(?:Dr|Mr|Mrs|Ms|Prof|St|vs|etc|Fig|al))"
        r"(?<![A-Z][a-z])"
        r"[.!?]+\s+"
    )

    sentences = sentence_boundary.split(text)
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    current: list[str] = []

    for sentence in sentences:
        current.append(sentence)
        if len(current) >= max_sentences:
            chunks.append(" ".join(current))
            current = []

    if current:
        chunks.append(" ".join(current))

    return [c for c in chunks if c.strip()]
