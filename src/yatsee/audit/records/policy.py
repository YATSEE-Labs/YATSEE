"""
Shared policy and sensitivity profiles for deterministic records auditing.

This module contains only stable audit configuration. Detector implementation,
report rendering, rebuild planning, and orchestration remain in separate modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from yatsee.core.errors import ValidationError

AUDIT_ARTIFACT_KIND = "records_audit"
AUDIT_SCHEMA_VERSION = "records_audit_v2"
AUDIT_PROFILE_BALANCED = "balanced"
AUDIT_PROFILE_STRICT = "strict"
AUDIT_JSON_NAME = "report.json"
AUDIT_CSV_NAME = "report.csv"
AUDIT_MARKDOWN_NAME = "report.md"
REBUILD_ARTIFACT_KIND = "records_rebuild_plan"
REBUILD_SCHEMA_VERSION = "records_rebuild_v1"
REBUILD_MANIFEST_NAME = "rebuild.json"
REBUILD_MODE_PLAN = "plan"
REBUILD_MODE_AUTO = "auto"
REBUILD_MIN_REJECTED_ITEMS = 3
REBUILD_MIN_REJECTION_RATE = 0.05

# Rebuild eligibility is intentionally narrower than finding severity. These
# codes describe structural artifact loss that another extraction attempt can
# plausibly repair. Heuristic quality findings never enter this policy.
_REBUILD_DIRECT_FINDING_CODES = {
    "failed_chunks",
    "incomplete_records_status",
    "invalid_required_field",
    "invalid_source_chunks",
    "missing_records_artifact",
    "missing_records_list",
    "non_object_record",
    "partial_chunks",
    "partial_records_artifact",
    "record_count_mismatch",
    "unreadable_records_artifact",
    "unsupported_record_type",
    "zero_records_from_nonempty_meeting",
}

SEVERITY_WEIGHTS = {
    "critical": 25,
    "high": 10,
    "medium": 4,
    "low": 1,
}


@dataclass(frozen=True)
class AuditProfile:
    """
    Define deterministic audit sensitivity without duplicating detector logic.

    Balanced mode is calibrated for useful best-effort records from small local
    models. Strict mode raises semantic expectations for higher-tier extraction
    runs while preserving the same structural hard-failure checks.

    :param name: Stable report and CLI profile name
    :param quantity_mode: material or expanded summary quantity checking
    :param duplicate_mode: conservative or expanded duplicate thresholds
    :param unknown_value_severity: Severity for explicit unknown optional values
    :param short_evidence_severity: Baseline severity for unusually short evidence
    :param filename_mismatch_severity: Severity for explicit filename routing mismatch
    :param weak_alignment_threshold: Rate below which alignment is weak
    :param low_alignment_threshold: Rate below which alignment is high risk
    :param context_identifier_severity: Severity for missing subject identifiers
    :param enable_detail_gap_check: Whether to flag detailed summaries with weak evidence
    """

    name: str
    quantity_mode: str
    duplicate_mode: str
    unknown_value_severity: str
    short_evidence_severity: str
    filename_mismatch_severity: str
    weak_alignment_threshold: float
    low_alignment_threshold: float
    context_identifier_severity: str
    enable_detail_gap_check: bool


AUDIT_PROFILES = {
    AUDIT_PROFILE_BALANCED: AuditProfile(
        name=AUDIT_PROFILE_BALANCED,
        quantity_mode="material",
        duplicate_mode="conservative",
        unknown_value_severity="low",
        short_evidence_severity="low",
        filename_mismatch_severity="medium",
        weak_alignment_threshold=0.95,
        low_alignment_threshold=0.90,
        context_identifier_severity="medium",
        enable_detail_gap_check=False,
    ),
    AUDIT_PROFILE_STRICT: AuditProfile(
        name=AUDIT_PROFILE_STRICT,
        quantity_mode="expanded",
        duplicate_mode="expanded",
        unknown_value_severity="medium",
        short_evidence_severity="low",
        filename_mismatch_severity="high",
        weak_alignment_threshold=0.98,
        low_alignment_threshold=0.95,
        context_identifier_severity="high",
        enable_detail_gap_check=True,
    ),
}


def _resolve_audit_profile(value: str) -> AuditProfile:
    """
    Resolve one explicit audit sensitivity profile.

    Profile selection is never inferred from model names because parameter
    counts, quantization, providers, and fine-tunes are not reliably encoded in
    a model tag.

    :param value: Requested profile name
    :return: Immutable audit profile configuration
    :raises ValidationError: If the profile is unsupported
    """
    normalized = str(value or AUDIT_PROFILE_BALANCED).strip().lower()
    profile = AUDIT_PROFILES.get(normalized)
    if profile is None:
        supported = ", ".join(sorted(AUDIT_PROFILES))
        raise ValidationError(
            f"Unsupported audit profile '{value}'. Supported profiles: {supported}"
        )
    return profile
