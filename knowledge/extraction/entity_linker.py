"""Entity linker for normalizing peptide and entity names in triples.

Resolves synonym variations to canonical entity names for consistent
knowledge graph construction.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Synonym map: canonical name → list of synonyms (case-insensitive matching)
SYNONYM_MAP: dict[str, list[str]] = {
    "BPC-157": ["bpc157", "body protection compound", "pentadecapeptide bpc-157", "pnc-27"],
    "TB-500": ["tb500", "thymosin beta-4", "thymosin beta4", "thymosin β-4", "tβ4"],
    "GHK-Cu": ["ghkcu", "copper peptide", "glycyl-l-histidyl-l-lysine", "ghk copper"],
    "IGF-1": ["igf1", "insulin-like growth factor 1", "insulin-like growth factor-1", "somatomedin c"],
    "CJC-1295": ["cjc1295", "cjc 1295", "dac:grf"],
    "Ipamorelin": ["ipamorelin acetate"],
    "GHRP-6": ["ghrp6", "growth hormone releasing peptide 6", "ghrp 6"],
    "GHRP-2": ["ghrp2", "growth hormone releasing peptide 2", "ghrp 2", "pralmorelin"],
    "Hexarelin": ["examorelin"],
    "MK-677": ["mk677", "ibutamoren", "ibutamoren mesylate", "mk 677"],
    "Sermorelin": ["sermorelin acetate", "geref"],
    "Tesamorelin": ["tesamorelin acetate", "egrifta"],
    "PT-141": ["pt141", "bremelanotide"],
    "Melanotan-II": ["melanotan 2", "melanotan2", "mt-ii", "mt2"],
    "Selank": ["selanke", "tp-7"],
    "Semax": ["pro-gly-pro"],
    "Epithalon": ["epithalone", "epitalon", "tetrapeptide-2"],
    "LL-37": ["ll37", "cathelicidin", "camp"],
    "SS-31": ["ss31", "d-arg-dmt-lys-phe-nh2", "szeto-schiller peptide"],
    "Kisspeptin": ["kisspeptin-10", "metastin"],
    "Dihexa": ["dihexa peptide", "pnb-0408"],
}

# Reverse lookup: synonym → canonical name (built at import time)
_REVERSE_MAP: dict[str, str] = {}
for canonical, synonyms in SYNONYM_MAP.items():
    _REVERSE_MAP[canonical.lower()] = canonical
    for syn in synonyms:
        _REVERSE_MAP[syn.lower()] = canonical


def normalize_entity(name: str) -> str:
    """Resolve an entity name to its canonical form.

    Args:
        name: Entity name as extracted from text.

    Returns:
        Canonical entity name if a match is found, otherwise the original name
        with consistent capitalization applied.
    """
    if not name:
        return name
    canonical = _REVERSE_MAP.get(name.lower().strip())
    return canonical if canonical else name.strip()


class EntityLinker:
    """Links entity names in triple records to canonical forms."""

    def __init__(
        self,
        triples_dir: Path = Path("data/processed/triples"),
    ) -> None:
        self.triples_dir = Path(triples_dir)

    def link_triples(self, triples: list[dict]) -> list[dict]:
        """Apply entity normalization to subject and object fields of triples.

        Args:
            triples: List of triple dicts with 'subject', 'relation', 'object' fields.

        Returns:
            Updated triple list with normalized entity names.
        """
        linked: list[dict] = []
        for triple in triples:
            updated = dict(triple)
            updated["subject"] = normalize_entity(triple.get("subject", ""))
            updated["object"] = normalize_entity(triple.get("object", ""))
            linked.append(updated)
        return linked

    def run(self) -> int:
        """Run entity linking on all triple files (in-place update)."""
        triple_files = list(self.triples_dir.glob("*_triples.json"))
        logger.info("EntityLinker: processing %d triple files", len(triple_files))

        total = 0
        for triple_file in triple_files:
            try:
                triples = json.loads(triple_file.read_text(encoding="utf-8"))
                linked = self.link_triples(triples)
                triple_file.write_text(json.dumps(linked, indent=2), encoding="utf-8")
                total += len(linked)
            except Exception as exc:
                logger.error("Error linking %s: %s", triple_file, exc)

        logger.info("EntityLinker: linked %d total triples", total)
        return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    total = EntityLinker().run()
    print(f"Linked {total} triples")


if __name__ == "__main__":
    main()
