"""Safety Rule Engine for REGENOVA-Intel.

Evaluates clinical queries and patient context against a catalogue of
safety rules, returning SafetyFlag objects for any triggered rules.

All rules are conservative by design — false positives are preferable
to missed safety concerns in a clinical decision support context.
"""

from __future__ import annotations

import logging
from typing import Optional

from apps.api.schemas.chat import SafetyFlag
from apps.api.schemas.patient_case import PatientCase
from apps.api.schemas.source import NormalizedChunk

logger = logging.getLogger(__name__)

# ── Growth-Signaling Peptide Registry ─────────────────────────────────────────
# These peptides activate growth pathways and carry special oncology/
# pregnancy caution flags.

GROWTH_SIGNALING_PEPTIDES: frozenset[str] = frozenset({
    "bpc-157", "bpc157", "body protection compound",
    "tb-500", "tb500", "thymosin beta-4", "thymosin beta 4",
    "ghk-cu", "ghkcu", "copper peptide",
    "igf-1", "igf1", "insulin-like growth factor",
    "cjc-1295", "cjc1295",
    "ipamorelin",
    "ghrp-6", "ghrp6",
    "ghrp-2", "ghrp2",
    "hexarelin",
    "mk-677", "mk677", "ibutamoren",
    "sermorelin",
    "tesamorelin",
})

ALL_PEPTIDES: frozenset[str] = GROWTH_SIGNALING_PEPTIDES | frozenset({
    "pt-141", "pt141", "bremelanotide",
    "melanotan", "melanotan-2",
    "epithalon",
    "ss-31", "ss31",
    "kisspeptin",
    "selank",
    "semax",
    "dihexa",
    "ll-37",
    "pentadecapeptide",
})

# Pregnancy / breastfeeding indicator terms
PREGNANCY_TERMS: frozenset[str] = frozenset({
    "pregnant", "pregnancy", "breastfeeding", "breast feeding",
    "nursing", "lactating", "lactation", "gestating", "gestation",
    "trimester", "prenatal", "postnatal", "postpartum",
})

# Oncology indicator terms
ONCOLOGY_TERMS: frozenset[str] = frozenset({
    "cancer", "malignancy", "malignant", "tumour", "tumor",
    "carcinoma", "sarcoma", "lymphoma", "leukaemia", "leukemia",
    "melanoma", "oncology", "oncologist", "metastasis", "metastatic",
    "neoplasm",
})


def _text_contains_any(text: str, terms: frozenset[str]) -> bool:
    """Return True if the lowercased text contains any of the given terms."""
    lower = text.lower()
    return any(term in lower for term in terms)


def _case_contains_any(patient_case: PatientCase | None, terms: frozenset[str]) -> bool:
    """Check patient case fields (contraindications, indications, notes) for terms."""
    if patient_case is None:
        return False
    fields = (
        patient_case.contraindications
        + patient_case.indications
        + ([patient_case.notes] if patient_case.notes else [])
    )
    return any(_text_contains_any(field, terms) for field in fields)


def _chunks_mention_growth_peptide(chunks: list[NormalizedChunk]) -> bool:
    """Return True if any retrieved chunk content mentions a growth-signaling peptide."""
    return any(
        _text_contains_any(chunk.content, GROWTH_SIGNALING_PEPTIDES) for chunk in chunks
    )


class SafetyRuleEngine:
    """Evaluates clinical safety rules and returns triggered SafetyFlags.

    Usage::

        engine = SafetyRuleEngine()
        flags = engine.evaluate(query, patient_case, chunks)

    Each rule method returns Optional[SafetyFlag]: a flag if triggered,
    None otherwise.
    """

    def evaluate(
        self,
        query: str,
        patient_case: PatientCase | None,
        chunks: list[NormalizedChunk],
    ) -> list[SafetyFlag]:
        """Run all safety rules and return a list of triggered flags.

        Args:
            query: The clinical query string.
            patient_case: Optional de-identified patient context.
            chunks: Retrieved chunks (used to detect peptide context).

        Returns:
            List of SafetyFlag objects for any triggered rules.
            Empty list if no rules fire.
        """
        flags: list[SafetyFlag] = []

        rules = [
            self.pregnancy_breastfeeding_caution(query, patient_case, chunks),
            self.cancer_history_caution(query, patient_case, chunks),
            self.missing_baseline_labs_warning(query, patient_case),
            self.polypharmacy_interaction_caution(patient_case),
        ]

        for flag in rules:
            if flag is not None:
                logger.warning(
                    "Safety rule triggered: code=%s severity=%s",
                    flag.code,
                    flag.severity,
                )
                flags.append(flag)

        logger.info("Safety evaluation complete: %d flags raised", len(flags))
        return flags

    def pregnancy_breastfeeding_caution(
        self,
        query: str,
        patient_case: PatientCase | None,
        chunks: list[NormalizedChunk],
    ) -> Optional[SafetyFlag]:
        """SR-001: Raise critical flag if pregnancy/breastfeeding context detected.

        Triggers if:
        - Patient case mentions pregnancy/breastfeeding, OR
        - Query text mentions pregnancy/breastfeeding
        AND any peptide is in scope (query or chunks).

        Returns:
            SafetyFlag(severity='critical') or None.
        """
        pregnancy_in_case = _case_contains_any(patient_case, PREGNANCY_TERMS)
        pregnancy_in_query = _text_contains_any(query, PREGNANCY_TERMS)

        if not (pregnancy_in_case or pregnancy_in_query):
            return None

        return SafetyFlag(
            severity="critical",
            code="SR-001",
            message=(
                "Pregnancy or breastfeeding context detected. "
                "Peptide use is contraindicated until safety in pregnancy is established."
            ),
            rationale=(
                "Safety profiles for most research peptides in pregnancy and lactation "
                "are unknown. No adequate well-controlled studies exist. The theoretical "
                "risk to the fetus/neonate cannot be excluded."
            ),
        )

    def cancer_history_caution(
        self,
        query: str,
        patient_case: PatientCase | None,
        chunks: list[NormalizedChunk],
    ) -> Optional[SafetyFlag]:
        """SR-002: Raise warning if oncology history + growth-signaling peptide context.

        Triggers if:
        - Patient case mentions cancer/malignancy, OR query mentions oncology
        AND a growth-signaling peptide is mentioned in query or chunks.

        Returns:
            SafetyFlag(severity='warning') or None.
        """
        oncology_detected = _case_contains_any(
            patient_case, ONCOLOGY_TERMS
        ) or _text_contains_any(query, ONCOLOGY_TERMS)

        if not oncology_detected:
            return None

        growth_peptide_detected = (
            _text_contains_any(query, GROWTH_SIGNALING_PEPTIDES)
            or _chunks_mention_growth_peptide(chunks)
        )

        if not growth_peptide_detected:
            return None

        return SafetyFlag(
            severity="warning",
            code="SR-002",
            message=(
                "Oncology history detected with growth-signaling peptide context. "
                "Oncologist consultation required before initiating any growth-signaling peptide."
            ),
            rationale=(
                "Growth-signaling peptides (including IGF-1 pathway agonists, GHRPs, "
                "and tissue-repair peptides) carry a theoretical risk of promoting "
                "cancer cell proliferation. Evidence is limited but the risk cannot "
                "be excluded in patients with active or historical malignancy."
            ),
        )

    def missing_baseline_labs_warning(
        self,
        query: str,  # noqa: ARG002
        patient_case: PatientCase | None,
    ) -> Optional[SafetyFlag]:
        """SR-003: Warn if patient case provided but baseline labs are absent.

        Returns:
            SafetyFlag(severity='info') or None.
        """
        if patient_case is None:
            return None

        if not patient_case.baseline_labs:
            return SafetyFlag(
                severity="info",
                code="SR-003",
                message=(
                    "No baseline laboratory values provided. "
                    "Consider obtaining baseline labs before initiating a peptide protocol."
                ),
                rationale=(
                    "Recommended baseline labs for peptide prescribing include: "
                    "IGF-1, IGFBP-3, fasting insulin, HbA1c, LFTs, full blood count, "
                    "and relevant hormone panels. Baseline values enable safe monitoring."
                ),
            )

        return None

    def polypharmacy_interaction_caution(
        self,
        patient_case: PatientCase | None,
    ) -> Optional[SafetyFlag]:
        """SR-004: Flag polypharmacy when patient is on 3+ medications.

        Returns:
            SafetyFlag(severity='info') or None.
        """
        if patient_case is None:
            return None

        med_count = len(patient_case.current_medications)
        if med_count >= 3:
            return SafetyFlag(
                severity="info",
                code="SR-004",
                message=(
                    f"Patient is on {med_count} medications. "
                    "Peptide-drug interaction data is limited — pharmacist review recommended."
                ),
                rationale=(
                    "Published peptide-drug interaction data is sparse. Polypharmacy "
                    "increases the risk of unknown interactions, particularly with "
                    "insulin-sensitising agents, GH-axis medications, and anticoagulants."
                ),
            )

        return None
