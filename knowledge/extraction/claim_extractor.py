"""Claim extraction from normalized chunks using LLM.

Extracts structured clinical claims from ingested document chunks
and saves them to data/processed/claims/.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CLAIMS_PROMPT_PATH = Path(__file__).parents[2] / "prompts" / "extraction" / "claims.txt"
_CLAIMS_OUTPUT_DIR = Path("data/processed/claims")


class ClaimExtractor:
    """Extracts structured clinical claims from NormalizedChunk content.

    Uses an LLM (OpenAI via LangChain) with the claims.txt prompt to
    identify and structure clinical claims, including:
    - claim_text: The specific clinical assertion
    - source_chunk_id: Reference to the source chunk
    - evidence_tier: Inherited from source metadata
    - confidence: Model confidence in the extraction
    - peptide_mentioned: Peptide(s) mentioned in the claim
    - entities: Other named entities (conditions, drugs, mechanisms)
    """

    def __init__(
        self,
        normalized_dir: Path = Path("data/processed/normalized"),
        output_dir: Path = Path("data/processed/claims"),
        model: str = "gpt-4o",
        openai_api_key: str = "",
    ) -> None:
        self.normalized_dir = Path(normalized_dir)
        self.output_dir = Path(output_dir)
        self.model = model
        self.openai_api_key = openai_api_key
        self._prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        try:
            return _CLAIMS_PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("Claims prompt not found at %s", _CLAIMS_PROMPT_PATH)
            return ""

    def _extract_claims_from_chunk(self, chunk_id: str, content: str, tier: int) -> list[dict]:
        """Call LLM to extract claims from a single chunk.

        Args:
            chunk_id: Source chunk identifier.
            content: Chunk text content.
            tier: Evidence tier of the source.

        Returns:
            List of claim record dicts.
        """
        try:
            from langchain_openai import ChatOpenAI  # type: ignore[import]
            from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore[import]

            llm = ChatOpenAI(model=self.model, temperature=0.0, api_key=self.openai_api_key or None)
            system_msg = self._prompt or (
                "Extract clinical claims as JSON array. Each claim: "
                "{claim_text, confidence, peptide_mentioned, entities}"
            )
            human_msg = f"Extract clinical claims from:\n\n{content}"
            response = llm.invoke([SystemMessage(content=system_msg), HumanMessage(content=human_msg)])
            raw = str(response.content).strip()

            # Parse JSON array from response
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            claims_data = json.loads(raw)
            if not isinstance(claims_data, list):
                claims_data = [claims_data]

            for claim in claims_data:
                claim["source_chunk_id"] = chunk_id
                claim["evidence_tier"] = tier

            return claims_data

        except Exception as exc:
            logger.warning("Claim extraction failed for chunk %s: %s", chunk_id, exc)
            return []

    def run(self) -> int:
        """Run claim extraction on all normalized chunks.

        Returns:
            Total number of claims extracted.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        chunk_files = list(self.normalized_dir.glob("*.json"))
        logger.info("ClaimExtractor: processing %d chunks", len(chunk_files))

        total_claims = 0
        for chunk_file in chunk_files:
            try:
                chunk_data = json.loads(chunk_file.read_text(encoding="utf-8"))
                chunk_id = chunk_data.get("chunk_id", chunk_file.stem)
                content = chunk_data.get("content", "")
                tier = chunk_data.get("evidence_tier_default", 3)

                if not content.strip():
                    continue

                claims = self._extract_claims_from_chunk(chunk_id, content, tier)
                if claims:
                    out_path = self.output_dir / f"{chunk_id}_claims.json"
                    out_path.write_text(json.dumps(claims, indent=2), encoding="utf-8")
                    total_claims += len(claims)
                    logger.debug("Extracted %d claims from %s", len(claims), chunk_id)

            except Exception as exc:
                logger.error("Error processing %s: %s", chunk_file, exc)

        logger.info("ClaimExtractor: extracted %d total claims", total_claims)
        return total_claims


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    total = ClaimExtractor().run()
    print(f"Extracted {total} claims")


if __name__ == "__main__":
    main()
