"""Tests for the ingestion LLMAssistant.

These tests exercise the deterministic fallback paths and the
content-integrity guard without requiring any network/API access.
"""

from __future__ import annotations

from pipelines.common.llm_assistant import LLMAssistant, LLMDecision


class TestFallbackMode:
    """Behaviour when no API key is configured (available == False)."""

    def setup_method(self) -> None:
        self.assistant = LLMAssistant(api_key="")  # fallback-only

    def test_not_available_without_key(self) -> None:
        assert self.assistant.available is False

    def test_pubmed_query_fallback_is_deterministic(self) -> None:
        d = self.assistant.build_pubmed_query("BPC-157")
        assert isinstance(d, LLMDecision)
        assert d.used_llm is False
        assert "BPC-157" in d.value
        assert "[tiab]" in d.value

    def test_pubmed_query_strips_quotes(self) -> None:
        d = self.assistant.build_pubmed_query('TB-500"; DROP')
        assert '"' not in d.value.replace('"TB-500', "").replace('"[tiab]', "X")
        # The injected quote should not survive into the middle of the term.
        assert "DROP" in d.value  # term kept, but quote-neutralised

    def test_relevance_defaults_to_keep(self) -> None:
        d = self.assistant.is_relevant("BPC-157", "Some video title")
        assert d.used_llm is False
        assert d.value is True  # conservative: never silently drop

    def test_clean_returns_original(self) -> None:
        text = "Hello and welcome. BPC-157 dose was 250 mcg twice daily."
        d = self.assistant.clean_text(text)
        assert d.used_llm is False
        assert d.value == text  # unchanged in fallback

    def test_clean_empty_text(self) -> None:
        d = self.assistant.clean_text("   ")
        assert d.value.strip() == ""


class TestAuditDict:
    """The audit_dict() helper used for the provenance trail."""

    def test_audit_dict_truncates_long_values(self) -> None:
        d = LLMDecision(kind="clean", used_llm=True, value="x" * 1000, rationale="r" * 1000)
        a = d.audit_dict()
        assert len(a["value"]) <= 301
        assert len(a["rationale"]) <= 300
        assert a["kind"] == "clean"
        assert a["used_llm"] is True

    def test_audit_dict_handles_bool(self) -> None:
        d = LLMDecision(kind="relevance", used_llm=True, value=True)
        a = d.audit_dict()
        assert a["value"] is True


class TestJsonParsing:
    """The internal JSON extraction handles fenced / noisy model output."""

    def test_parse_plain_json(self) -> None:
        assert LLMAssistant._parse_json('{"query": "x"}') == {"query": "x"}

    def test_parse_fenced_json(self) -> None:
        assert LLMAssistant._parse_json('```json\n{"query": "x"}\n```') == {"query": "x"}

    def test_parse_embedded_json(self) -> None:
        assert LLMAssistant._parse_json('Here you go: {"a": 1} done') == {"a": 1}

    def test_parse_garbage_returns_none(self) -> None:
        assert LLMAssistant._parse_json("not json at all") is None
