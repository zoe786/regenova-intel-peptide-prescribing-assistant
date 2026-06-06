"""Citation service for attaching numbered citations to generated answers.

Injects [1], [2] style inline markers into answer text and produces
a deduplicated Citation list for the ChatResponse.
"""

from __future__ import annotations

import logging
import re

from apps.api.schemas.chat import Citation
from apps.api.schemas.source import NormalizedChunk

logger = logging.getLogger(__name__)

# Maximum excerpt length for citations
_EXCERPT_MAX_LEN = 300


class CitationService:
    """Attaches numbered citations to answer text.

    Takes retrieved chunks and answer text, assigns citation numbers,
    deduplicates by source_id, and returns the annotated answer + citation list.
    """

    def attach_citations(
        self,
        chunks: list[NormalizedChunk],
        answer_text: str,
    ) -> tuple[str, list[Citation]]:
        """Inject citation markers into answer text and build citation list.

        The method works as follows:
        1. Deduplicate chunks by source_id (keep first occurrence per source)
        2. Append [N] markers to answer text referencing each source
        3. Validate citation integrity (every [N] has a matching Citation)

        Args:
            chunks: Ranked chunks used to generate the answer.
            answer_text: The LLM-generated answer text.

        Returns:
            Tuple of (annotated_answer, citations_list).
        """
        if not chunks:
            logger.info("No chunks provided — no citations attached")
            return answer_text, []

        # Deduplicate by source_id, preserving order
        seen_source_ids: set[str] = set()
        unique_chunks: list[NormalizedChunk] = []
        for chunk in chunks:
            sid = chunk.metadata.document_id or chunk.chunk_id
            if sid not in seen_source_ids:
                seen_source_ids.add(sid)
                unique_chunks.append(chunk)

        # Build Citation objects
        citations: list[Citation] = []
        for idx, chunk in enumerate(unique_chunks, start=1):
            excerpt = chunk.content[:_EXCERPT_MAX_LEN].replace("\n", " ").strip()
            if len(chunk.content) > _EXCERPT_MAX_LEN:
                excerpt += "…"

            provenance = dict(getattr(chunk.metadata, "extra_metadata", {}) or {})
            citation = Citation(
                source_id=chunk.metadata.document_id or chunk.chunk_id,
                source_name=self._display_name(chunk),
                url=chunk.metadata.source_url,
                chunk_id=chunk.chunk_id,
                evidence_tier=chunk.metadata.evidence_tier_default,
                excerpt=excerpt,
                provenance=provenance,
            )
            citations.append(citation)
            logger.debug("Citation [%d] → %s (tier %d)", idx, citation.source_name, citation.evidence_tier)

        # Append citation markers to answer text
        if citations:
            marker_block = "\n\n**Sources:**\n" + "\n".join(
                f"[{i+1}] {c.source_name}" + (f" — {c.url}" if c.url else "")
                for i, c in enumerate(citations)
            )
            annotated_answer = answer_text + marker_block
        else:
            annotated_answer = answer_text

        # Validate integrity: every [N] in the final text has a Citation
        self._validate_citation_integrity(annotated_answer, citations)

        logger.info("Attached %d citations to answer", len(citations))
        return annotated_answer, citations

    @staticmethod
    def _display_name(chunk: NormalizedChunk) -> str:
        """Build a human-readable attribution string from chunk provenance.

        Falls back to the raw source_name when no richer provenance exists.
        - YouTube: "<channel> — <video title>"
        - PubMed:  "<source_name> (via search: <query>)"
        """
        meta = chunk.metadata
        extra = getattr(meta, "extra_metadata", {}) or {}
        source_type = (meta.source_type or "").lower()

        if source_type == "youtube":
            channel = extra.get("channel_name")
            title = extra.get("video_title")
            if channel and title:
                return f"{channel} — {title}"
            if title:
                return str(title)
            if channel:
                return str(channel)

        if source_type == "pubmed":
            query = extra.get("pubmed_query")
            if query:
                return f"{meta.source_name} (via search: {query})"

        if source_type == "skool_community":
            community = extra.get("community_name")
            author = extra.get("post_author")
            if community and author:
                return f"{meta.source_name} — {community} (post by {author})"

        return meta.source_name

    def _validate_citation_integrity(
        self,
        answer_text: str,
        citations: list[Citation],
    ) -> None:
        """Check that every [N] marker in the answer has a corresponding Citation.

        Logs a warning for any orphaned markers or missing citations.

        Args:
            answer_text: The annotated answer text.
            citations: The list of Citation objects.
        """
        markers = set(int(m) for m in re.findall(r"\[(\d+)\]", answer_text))
        citation_numbers = set(range(1, len(citations) + 1))

        orphaned = markers - citation_numbers
        if orphaned:
            logger.warning(
                "Citation integrity issue: markers %s have no corresponding Citation",
                sorted(orphaned),
            )

        unused = citation_numbers - markers
        if unused:
            # This is expected when citation block is appended — not a true integrity issue
            logger.debug(
                "Citations %s are listed but not inline-referenced in answer text",
                sorted(unused),
            )
