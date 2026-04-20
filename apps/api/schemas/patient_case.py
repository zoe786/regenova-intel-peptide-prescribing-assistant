"""Pydantic schemas for patient case context.

Patient cases are optional clinical context provided by clinicians
to enable personalised safety rule evaluation.

IMPORTANT: Do NOT submit real patient PII. Use de-identified summaries only.
"""

from __future__ import annotations

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PatientCase(BaseModel):
    """De-identified patient case for safety rule evaluation.

    All fields are optional to support partial context.
    Do NOT populate with real patient identifiers.
    """

    case_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Ephemeral case identifier (not persisted)",
    )
    age_range: Optional[str] = Field(
        default=None,
        description="Age range (e.g. '30-40', '50+') — not exact DOB",
    )
    sex: Optional[Literal["male", "female", "other", "not_specified"]] = Field(
        default=None,
        description="Biological sex for safety rule evaluation",
    )
    indications: list[str] = Field(
        default_factory=list,
        description="Clinical indications / treatment goals (e.g. 'tendon healing', 'GH optimisation')",
    )
    contraindications: list[str] = Field(
        default_factory=list,
        description="Known contraindications / relevant history (e.g. 'pregnant', 'malignancy history')",
    )
    current_medications: list[str] = Field(
        default_factory=list,
        description="Current medications (generic names preferred)",
    )
    baseline_labs: dict[str, str] = Field(
        default_factory=dict,
        description="Relevant baseline lab values (e.g. {'igf1': '150 ng/mL', 'hba1c': '5.4%'})",
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Additional de-identified clinical notes",
    )


class CaseContext(BaseModel):
    """Full context for a clinical query, combining patient case and query."""

    patient_case: Optional[PatientCase] = Field(
        default=None,
        description="Optional de-identified patient case for safety evaluation",
    )
    clinician_query: str = Field(
        ..., description="The clinical query submitted by the clinician"
    )
    session_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Session identifier for tracking multi-turn conversations",
    )
