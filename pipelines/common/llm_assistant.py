"""LLM assistance for the ingestion pipeline.

Provides a single, audited entry point for using the same OpenAI-compatible
LLM that powers the chat API to help with *ingestion-time decisions*:

- building PubMed Entrez search queries from a peptide name,
- triaging whether a discovered source is relevant to a topic,
- light, reversible cleaning of noisy transcript/forum text.

Design rules (enforced by this module's narrow API surface):

1. The LLM may decide *what* to ingest and *how* to search, but it must
   never rewrite, summarise, or paraphrase source content that will be
   stored and later cited. ``clean_text`` only removes filler/boilerplate
   and returns verbatim-preserving output; if the model returns something
   that looks like a rewrite (large length change), the original is kept.
2. Every LLM-assisted decision returns a structured result that callers
   are expected to log to the audit store, so "why was this ingested?"
   is always answerable.
3. The assistant degrades gracefully: if no API key/model is configured
   or the call fails, callers receive a deterministic, conservative
   fallback rather than an exception.

The assistant reuses the chat API's LLM configuration (llm_api_key /
llm_model / llm_base_url / llm_temperature) so there is a single provider
for the whole system.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A cleaning result is only accepted if the cleaned text length stays within
# this fraction of the original. Anything outside the band is treated as a
# rewrite/hallucination and the original is preserved.
_CLEAN_MIN_RATIO = 0.5
_CLEAN_MAX_RATIO = 1.05

_DEFAULT_TIMEOUT = 30


@dataclass
class LLMDecision:
    """Structured, auditable result of an LLM-assisted ingestion decision.

    Attributes:
        kind: Decision type ("pubmed_query", "relevance", "clean").
        used_llm: True if the LLM actually produced the result, False if a
            deterministic fallback was used.
        value: The primary output (query string / bool / cleaned text).
        rationale: Short human-readable reason, for the audit trail.
        raw: Any extra structured data (e.g. parsed JSON) for debugging.
    """

    kind: str
    used_llm: bool
    value: Any
    rationale: str = ""
    raw: dict = field(default_factory=dict)

    def audit_dict(self) -> dict:
        """Return a compact dict suitable for audit logging."""
        value_repr = self.value if isinstance(self.value, (str, bool, int, float)) else str(self.value)
        if isinstance(value_repr, str) and len(value_repr) > 300:
            value_repr = value_repr[:300] + "…"
        return {
            "kind": self.kind,
            "used_llm": self.used_llm,
            "value": value_repr,
            "rationale": self.rationale[:300],
        }


class LLMAssistant:
    """Thin, defensive wrapper over the chat LLM for ingestion-time tasks."""

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str = "",
        base_url: str = "",
        temperature: float = 0.0,
    ) -> None:
        """Initialise with the same config the chat API uses.

        Args:
            model: Model name (provider-specific).
            api_key: LLM provider API key. If empty, the assistant runs in
                fallback-only mode.
            base_url: Optional OpenAI-compatible base URL.
            temperature: Sampling temperature. Defaults to 0.0 because
                ingestion decisions should be deterministic.
        """
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature

    @property
    def available(self) -> bool:
        """True if an API key is configured (LLM calls may be attempted)."""
        return bool(self.api_key)

    # ── Internal LLM call ─────────────────────────────────────────────────

    def _chat_json(self, system: str, user: str) -> Optional[dict]:
        """Call the LLM and parse a JSON object from its reply.

        Returns None on any failure (no key, import error, network error,
        unparseable output) so callers can fall back deterministically.
        """
        if not self.available:
            return None
        try:
            from openai import OpenAI  # type: ignore[import]
        except ImportError:
            logger.warning("openai package not installed — LLM assist disabled")
            return None

        try:
            client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url or None,
                timeout=_DEFAULT_TIMEOUT,
            )
            resp = client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            content = resp.choices[0].message.content or ""
            return self._parse_json(content)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash ingestion
            logger.warning("LLM assist call failed: %s", exc)
            return None

    @staticmethod
    def _parse_json(content: str) -> Optional[dict]:
        """Extract the first JSON object from a model reply."""
        content = content.strip()
        # Strip code fences if the model added them despite json mode.
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
        try:
            data = json.loads(content)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, flags=re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    return None
            return None

    # ── Task 1: PubMed query construction ─────────────────────────────────

    def build_pubmed_query(self, peptide: str) -> LLMDecision:
        """Build an Entrez search query for a peptide.

        The LLM proposes a query using synonyms, common aliases, and
        title/abstract field tags. If the LLM is unavailable or returns
        nothing usable, a conservative deterministic query is returned.

        Args:
            peptide: Peptide name (e.g. "BPC-157").

        Returns:
            LLMDecision whose ``value`` is an Entrez query string.
        """
        fallback = self._fallback_pubmed_query(peptide)

        system = (
            "You are a biomedical search librarian. Given a peptide or compound "
            "name, produce a PubMed Entrez search query that maximises recall of "
            "clinically relevant human and preclinical studies while staying on "
            "topic. Use title/abstract tags [tiab] and OR-group common synonyms "
            "and aliases. Do NOT invent MeSH terms you are unsure exist. "
            'Respond ONLY as JSON: {"query": "<entrez query>", "synonyms": '
            '["..."], "rationale": "<one sentence>"}.'
        )
        user = f"Peptide/compound: {peptide}"
        data = self._chat_json(system, user)

        if not data or not isinstance(data.get("query"), str) or not data["query"].strip():
            return LLMDecision(
                kind="pubmed_query",
                used_llm=False,
                value=fallback,
                rationale="LLM unavailable or invalid output; using deterministic query.",
            )

        query = data["query"].strip()
        return LLMDecision(
            kind="pubmed_query",
            used_llm=True,
            value=query,
            rationale=str(data.get("rationale", ""))[:300],
            raw={"synonyms": data.get("synonyms", [])},
        )

    @staticmethod
    def _fallback_pubmed_query(peptide: str) -> str:
        """Deterministic Entrez query when the LLM is unavailable."""
        safe = peptide.strip().replace('"', "")
        return f'"{safe}"[tiab]'

    # ── Task 2: relevance triage ──────────────────────────────────────────

    def is_relevant(self, topic: str, title: str, snippet: str = "") -> LLMDecision:
        """Decide whether a discovered source is relevant to a topic.

        Used as a cheap filter before spending embedding cost on a
        transcript or abstract. When the LLM is unavailable the decision
        defaults to True (conservative: do not silently drop content) but
        records that no judgement was made.

        Args:
            topic: The topic/peptide the corpus is about.
            title: Title of the discovered source.
            snippet: Optional short snippet/description.

        Returns:
            LLMDecision whose ``value`` is a bool.
        """
        system = (
            "You judge whether a source is relevant to a clinical/scientific "
            "topic. Be inclusive: keep anything plausibly about the topic in a "
            "health, research, or clinical context; reject only clearly "
            'off-topic items. Respond ONLY as JSON: {"relevant": true/false, '
            '"rationale": "<one sentence>"}.'
        )
        user = f"Topic: {topic}\nTitle: {title}\nSnippet: {snippet[:500]}"
        data = self._chat_json(system, user)

        if not data or "relevant" not in data:
            return LLMDecision(
                kind="relevance",
                used_llm=False,
                value=True,
                rationale="LLM unavailable; defaulting to keep (conservative).",
            )

        return LLMDecision(
            kind="relevance",
            used_llm=True,
            value=bool(data["relevant"]),
            rationale=str(data.get("rationale", ""))[:300],
        )

    # ── Task 3: light, reversible cleaning ────────────────────────────────

    def clean_text(self, text: str, source_kind: str = "transcript") -> LLMDecision:
        """Remove filler/boilerplate without altering substantive content.

        This deliberately refuses to summarise or paraphrase. If the model
        returns text whose length is far from the original, the result is
        rejected and the original text is preserved — protecting citation
        integrity against accidental rewrites.

        Args:
            text: Raw source text.
            source_kind: "transcript" or "forum" — tunes the instruction.

        Returns:
            LLMDecision whose ``value`` is the (possibly unchanged) text.
        """
        if not text.strip():
            return LLMDecision(kind="clean", used_llm=False, value=text, rationale="empty")

        system = (
            "You remove non-substantive filler from "
            f"{source_kind} text: greetings, calls to subscribe/like, ad reads, "
            "navigation cruft, and duplicated boilerplate. You MUST preserve all "
            "substantive content verbatim — do not summarise, paraphrase, "
            "reorder, or change any factual statement, dosage, or number. "
            'Respond ONLY as JSON: {"cleaned": "<text>"}.'
        )
        user = text[:12000]  # cap input; cleaning is best-effort
        data = self._chat_json(system, user)

        if not data or not isinstance(data.get("cleaned"), str):
            return LLMDecision(
                kind="clean",
                used_llm=False,
                value=text,
                rationale="LLM unavailable or invalid output; kept original.",
            )

        cleaned = data["cleaned"]
        ratio = len(cleaned) / max(1, len(text))
        if not (_CLEAN_MIN_RATIO <= ratio <= _CLEAN_MAX_RATIO):
            logger.warning(
                "Rejecting LLM clean (length ratio %.2f outside [%.2f, %.2f]) — "
                "kept original to protect content integrity.",
                ratio, _CLEAN_MIN_RATIO, _CLEAN_MAX_RATIO,
            )
            return LLMDecision(
                kind="clean",
                used_llm=True,
                value=text,
                rationale=f"Rejected rewrite (ratio={ratio:.2f}); kept original.",
            )

        return LLMDecision(
            kind="clean",
            used_llm=True,
            value=cleaned,
            rationale="Removed filler; substantive content preserved.",
        )
