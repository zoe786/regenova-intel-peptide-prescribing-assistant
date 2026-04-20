"""pytest tests for SafetyRuleEngine.

Tests cover: pregnancy flag, cancer history flag, missing labs flag,
false positive prevention, and polypharmacy flag.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure repo root is in path
sys.path.insert(0, str(Path(__file__).parents[1]))

from apps.api.schemas.patient_case import PatientCase
from apps.api.schemas.source import NormalizedChunk, SourceMetadata
from apps.api.services.safety_rules import SafetyRuleEngine
from datetime import datetime


def _make_chunk(content: str, tier: int = 2) -> NormalizedChunk:
    """Helper: create a NormalizedChunk for test input."""
    meta = SourceMetadata(
        source_type="document",
        source_name="Test Source",
        acquired_at=datetime.utcnow(),
        evidence_tier_default=tier,
        content_hash="test",
        document_id="test_doc",
    )
    return NormalizedChunk(
        chunk_id="test_chunk",
        document_id="test_doc",
        content=content,
        metadata=meta,
        similarity_score=0.8,
    )


def _make_patient(**kwargs) -> PatientCase:
    """Helper: create a PatientCase with given fields."""
    return PatientCase(**kwargs)


@pytest.fixture
def engine() -> SafetyRuleEngine:
    """Return a SafetyRuleEngine instance."""
    return SafetyRuleEngine()


# ── SR-001: Pregnancy / Breastfeeding ─────────────────────────────────────────

class TestPregnancyFlag:
    def test_pregnancy_flag_triggers_via_patient_case(self, engine):
        """SR-001 should trigger when patient case lists 'pregnant'."""
        patient = _make_patient(contraindications=["pregnant"])
        flags = engine.evaluate(
            query="What is the dosing protocol for BPC-157?",
            patient_case=patient,
            chunks=[],
        )
        codes = [f.code for f in flags]
        assert "SR-001" in codes, f"Expected SR-001, got: {codes}"
        sr001 = next(f for f in flags if f.code == "SR-001")
        assert sr001.severity == "critical"

    def test_pregnancy_flag_triggers_via_query(self, engine):
        """SR-001 should trigger when 'pregnant' appears in query."""
        flags = engine.evaluate(
            query="Is BPC-157 safe during pregnancy?",
            patient_case=None,
            chunks=[],
        )
        codes = [f.code for f in flags]
        assert "SR-001" in codes

    def test_breastfeeding_triggers_flag(self, engine):
        """SR-001 should trigger for breastfeeding context."""
        patient = _make_patient(contraindications=["breastfeeding"])
        flags = engine.evaluate(
            query="peptide protocol options",
            patient_case=patient,
            chunks=[],
        )
        codes = [f.code for f in flags]
        assert "SR-001" in codes

    def test_pregnancy_flag_rationale_present(self, engine):
        """SR-001 flag should have a non-empty rationale."""
        patient = _make_patient(contraindications=["pregnant"])
        flags = engine.evaluate(query="BPC-157 protocol", patient_case=patient, chunks=[])
        sr001 = next((f for f in flags if f.code == "SR-001"), None)
        assert sr001 is not None
        assert len(sr001.rationale) > 20


# ── SR-002: Cancer History ─────────────────────────────────────────────────────

class TestCancerHistoryFlag:
    def test_cancer_history_flag_with_igf1_in_chunks(self, engine):
        """SR-002 should trigger when oncology history + IGF-1 in chunks."""
        patient = _make_patient(contraindications=["malignancy history"])
        chunk = _make_chunk("IGF-1 has been shown to promote tissue growth via...")
        flags = engine.evaluate(
            query="growth hormone optimization protocol",
            patient_case=patient,
            chunks=[chunk],
        )
        codes = [f.code for f in flags]
        assert "SR-002" in codes, f"Expected SR-002 in {codes}"
        sr002 = next(f for f in flags if f.code == "SR-002")
        assert sr002.severity == "warning"

    def test_cancer_history_flag_with_bpc157_query(self, engine):
        """SR-002 should trigger with active cancer + BPC-157 in query."""
        patient = _make_patient(contraindications=["cancer"])
        flags = engine.evaluate(
            query="Is BPC-157 appropriate for my patient?",
            patient_case=patient,
            chunks=[],
        )
        codes = [f.code for f in flags]
        assert "SR-002" in codes

    def test_cancer_history_without_growth_peptide_no_flag(self, engine):
        """SR-002 should NOT trigger when no growth-signaling peptide is in scope."""
        patient = _make_patient(contraindications=["malignancy"])
        chunk = _make_chunk("General information about nutrition and wellness.")
        flags = engine.evaluate(
            query="general wellness information",
            patient_case=patient,
            chunks=[chunk],
        )
        codes = [f.code for f in flags]
        assert "SR-002" not in codes, f"SR-002 should not fire without growth peptide: {codes}"


# ── SR-003: Missing Baseline Labs ─────────────────────────────────────────────

class TestMissingLabsFlag:
    def test_missing_labs_flag_empty_dict(self, engine):
        """SR-003 should trigger when patient case has no baseline_labs."""
        patient = _make_patient(indications=["tendon healing"], baseline_labs={})
        flags = engine.evaluate(
            query="peptide options for tendon injury",
            patient_case=patient,
            chunks=[],
        )
        codes = [f.code for f in flags]
        assert "SR-003" in codes
        sr003 = next(f for f in flags if f.code == "SR-003")
        assert sr003.severity == "info"

    def test_no_missing_labs_flag_when_labs_present(self, engine):
        """SR-003 should NOT trigger when baseline_labs is populated."""
        patient = _make_patient(
            indications=["tendon healing"],
            baseline_labs={"igf1": "150 ng/mL", "hba1c": "5.4%"},
        )
        flags = engine.evaluate(query="peptide protocol", patient_case=patient, chunks=[])
        codes = [f.code for f in flags]
        assert "SR-003" not in codes

    def test_no_missing_labs_flag_when_no_patient_case(self, engine):
        """SR-003 should NOT trigger when patient_case is None."""
        flags = engine.evaluate(query="peptide info", patient_case=None, chunks=[])
        codes = [f.code for f in flags]
        assert "SR-003" not in codes


# ── SR-004: Polypharmacy ──────────────────────────────────────────────────────

class TestPolypharmacyFlag:
    def test_polypharmacy_three_medications(self, engine):
        """SR-004 should trigger with 3+ medications."""
        patient = _make_patient(
            current_medications=["metformin", "lisinopril", "atorvastatin"]
        )
        flags = engine.evaluate(query="peptide protocol", patient_case=patient, chunks=[])
        codes = [f.code for f in flags]
        assert "SR-004" in codes
        sr004 = next(f for f in flags if f.code == "SR-004")
        assert sr004.severity == "info"

    def test_no_polypharmacy_flag_with_two_meds(self, engine):
        """SR-004 should NOT trigger with fewer than 3 medications."""
        patient = _make_patient(current_medications=["metformin", "lisinopril"])
        flags = engine.evaluate(query="peptide info", patient_case=patient, chunks=[])
        codes = [f.code for f in flags]
        assert "SR-004" not in codes


# ── No False Positives ─────────────────────────────────────────────────────────

class TestNoFalsePositives:
    def test_no_false_positives_normal_case(self, engine):
        """No flags should trigger for a normal patient case with a non-growth peptide query."""
        patient = _make_patient(
            age_range="35-45",
            sex="male",
            indications=["muscle recovery"],
            contraindications=[],
            current_medications=["vitamin_d"],
            baseline_labs={"igf1": "155 ng/mL"},
        )
        # Selank is NOT in GROWTH_SIGNALING_PEPTIDES and no pregnancy/cancer context
        chunk = _make_chunk("Selank has anxiolytic properties and modulates GABA receptors.")
        flags = engine.evaluate(
            query="What are the anxiolytic effects of Selank?",
            patient_case=patient,
            chunks=[chunk],
        )
        # Should have no critical or warning flags
        high_severity = [f for f in flags if f.severity in ("critical", "warning")]
        assert len(high_severity) == 0, f"Unexpected high-severity flags: {high_severity}"

    def test_empty_query_no_critical_flags(self, engine):
        """An empty patient case should not produce critical flags."""
        flags = engine.evaluate(query="general information", patient_case=None, chunks=[])
        critical = [f for f in flags if f.severity == "critical"]
        assert len(critical) == 0
