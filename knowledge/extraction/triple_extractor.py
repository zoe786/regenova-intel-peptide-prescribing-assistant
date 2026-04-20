"""Subject-relation-object triple extractor using LLM.

Takes claim records and extracts structured triples for knowledge graph construction.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_TRIPLES_PROMPT_PATH = Path(__file__).parents[2] / "prompts" / "extraction" / "triples.txt"
_TRIPLES_OUTPUT_DIR = Path("data/processed/triples")

VALID_RELATIONS = {
    "TREATS", "CONTRAINDICATED_WITH", "INTERACTS_WITH",
    "STUDIED_IN", "DOSAGE_FOR", "MECHANISM_OF", "ASSOCIATED_WITH",
    "UPREGULATES", "DOWNREGULATES", "PROMOTES", "INHIBITS",
}


class TripleExtractor:
    """Extracts subject-relation-object triples from claim records."""

    def __init__(
        self,
        claims_dir: Path = Path("data/processed/claims"),
        output_dir: Path = Path("data/processed/triples"),
        model: str = "gpt-4o",
        openai_api_key: str = "",
    ) -> None:
        self.claims_dir = Path(claims_dir)
        self.output_dir = Path(output_dir)
        self.model = model
        self.openai_api_key = openai_api_key
        self._prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        try:
            return _TRIPLES_PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("Triples prompt not found at %s", _TRIPLES_PROMPT_PATH)
            return ""

    def _extract_triples_from_claims(self, claims: list[dict]) -> list[dict]:
        """Extract triples from a list of claim records."""
        if not claims:
            return []

        claims_text = "\n".join(
            f"- [{c.get('evidence_tier', '?')}] {c.get('claim_text', '')}"
            for c in claims if c.get("claim_text")
        )

        try:
            from langchain_openai import ChatOpenAI  # type: ignore[import]
            from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore[import]

            llm = ChatOpenAI(model=self.model, temperature=0.0, api_key=self.openai_api_key or None)
            system_msg = self._prompt or (
                "Extract subject-relation-object triples as JSON. "
                "Relations must be one of: " + ", ".join(sorted(VALID_RELATIONS))
            )
            response = llm.invoke([
                SystemMessage(content=system_msg),
                HumanMessage(content=f"Extract triples from these claims:\n\n{claims_text}"),
            ])
            raw = str(response.content).strip()
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            triples = json.loads(raw)
            if not isinstance(triples, list):
                triples = [triples]

            # Validate relation vocabulary
            valid_triples = []
            for t in triples:
                if t.get("relation") in VALID_RELATIONS:
                    valid_triples.append(t)
                else:
                    logger.debug("Skipping triple with invalid relation: %s", t.get("relation"))
            return valid_triples

        except Exception as exc:
            logger.warning("Triple extraction failed: %s", exc)
            return []

    def run(self) -> int:
        """Run triple extraction on all claim files."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        claim_files = list(self.claims_dir.glob("*_claims.json"))
        logger.info("TripleExtractor: processing %d claim files", len(claim_files))

        total_triples = 0
        for claim_file in claim_files:
            try:
                claims = json.loads(claim_file.read_text(encoding="utf-8"))
                triples = self._extract_triples_from_claims(claims)
                if triples:
                    out_name = claim_file.stem.replace("_claims", "_triples") + ".json"
                    (self.output_dir / out_name).write_text(
                        json.dumps(triples, indent=2), encoding="utf-8"
                    )
                    total_triples += len(triples)
            except Exception as exc:
                logger.error("Error processing %s: %s", claim_file, exc)

        logger.info("TripleExtractor: extracted %d total triples", total_triples)
        return total_triples


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    total = TripleExtractor().run()
    print(f"Extracted {total} triples")


if __name__ == "__main__":
    main()
