"""
Corpus aggregation and report rendering for deterministic records audits.

Rendering is kept separate from detector logic so report formats can evolve
without changing the findings produced for each meeting.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Sequence

from yatsee.core.errors import ValidationError

from .checks import _add_finding, _finalize_meeting_result
from .policy import (
    AUDIT_CSV_NAME,
    AUDIT_JSON_NAME,
    AUDIT_MARKDOWN_NAME,
    AUDIT_PROFILE_BALANCED,
    AuditProfile,
)

def _quartiles(values: Sequence[float]) -> tuple[float, float]:
    """
    Calculate lower and upper quartiles with a median-of-halves method.

    :param values: Numeric values
    :return: First and third quartiles
    :raises ValidationError: If fewer than four values are supplied
    """
    ordered = sorted(values)
    if len(ordered) < 4:
        raise ValidationError("At least four values are required for quartiles")
    midpoint = len(ordered) // 2
    lower = ordered[:midpoint]
    upper = ordered[midpoint:] if len(ordered) % 2 == 0 else ordered[midpoint + 1 :]
    return float(median(lower)), float(median(upper))


def _apply_distribution_findings(meetings: Sequence[Dict[str, Any]]) -> None:
    """
    Add low-severity corpus distribution outliers to meeting results.

    :param meetings: Mutable meeting audit result dictionaries
    :return: None
    """
    eligible = [
        meeting
        for meeting in meetings
        if meeting["status"] == "complete" and meeting["chunk_count"] > 0
    ]
    if len(eligible) < 8:
        return

    ratios = [meeting["record_count"] / meeting["chunk_count"] for meeting in eligible]
    q1, q3 = _quartiles(ratios)
    iqr = q3 - q1
    lower_bound = max(0.0, q1 - 1.5 * iqr)
    upper_bound = q3 + 1.5 * iqr

    for meeting in eligible:
        ratio = meeting["record_count"] / meeting["chunk_count"]
        if ratio < lower_bound or ratio > upper_bound:
            _add_finding(
                meeting,
                code="records_per_chunk_outlier",
                severity="low",
                message=f"Records-per-chunk ratio {ratio:.2f} is a corpus outlier.",
                details={
                    "ratio": round(ratio, 4),
                    "lower_bound": round(lower_bound, 4),
                    "upper_bound": round(upper_bound, 4),
                },
            )
            _finalize_meeting_result(meeting)


def _discover_meeting_directories(
    records_dir: str,
    *,
    output_dir: str,
    meeting_filter: str | None,
    allow_empty: bool = False,
) -> List[str]:
    """
    Discover immediate meeting artifact directories beneath a records root.

    :param records_dir: Model-specific records root
    :param output_dir: Audit output directory to exclude from discovery
    :param meeting_filter: Optional case-insensitive directory-name substring
    :param allow_empty: Whether an empty records root is valid for rebuild planning
    :return: Sorted meeting directory paths
    :raises ValidationError: If no matching meeting directories exist
    """
    records_path = Path(records_dir).resolve()
    output_path = Path(output_dir).resolve()
    if not records_path.is_dir():
        if allow_empty:
            return []
        raise ValidationError(f"Records directory not found: {records_path}")

    normalized_filter = meeting_filter.lower() if meeting_filter else None
    directories = []
    for path in sorted(records_path.iterdir(), key=lambda item: item.name.lower()):
        if (
            not path.is_dir()
            or path.resolve() == output_path
            or path.name == "audit"
            or path.name.startswith(".")
        ):
            continue
        if normalized_filter and normalized_filter not in path.name.lower():
            continue
        directories.append(str(path.resolve()))

    if not directories and not allow_empty:
        if meeting_filter:
            raise ValidationError(
                f"No meeting directories matching '{meeting_filter}' found in: {records_path}"
            )
        raise ValidationError(f"No meeting artifact directories found in: {records_path}")
    return directories


def _finding_code_counts(meeting: Dict[str, Any]) -> str:
    """
    Render compact finding-code counts for CSV output.

    :param meeting: Meeting audit result
    :return: Semicolon-delimited code counts
    """
    counts = Counter(finding["code"] for finding in meeting["findings"])
    return ";".join(f"{code}:{counts[code]}" for code in sorted(counts))


def render_audit_csv(report: Dict[str, Any]) -> str:
    """
    Render a flat CSV inventory from a records audit report.

    :param report: Complete records audit report
    :return: CSV text
    """
    fieldnames = [
        "audit_profile",
        "meeting",
        "status",
        "meeting_type",
        "chunk_count",
        "successful_chunk_count",
        "partial_chunk_count",
        "failed_chunk_count",
        "record_count",
        "validation_warning_count",
        "normalized_record_count",
        "rejected_record_count",
        "rejected_line_count",
        "alignment_rate",
        "alignment_matched",
        "alignment_ambiguous",
        "alignment_unmatched",
        "duplicate_candidates",
        "summary_evidence_number_mismatches",
        "official_public_comment_candidates",
        "unknown_attribute_values",
        "risk_score",
        "critical_findings",
        "high_findings",
        "medium_findings",
        "low_findings",
        "finding_codes",
        "artifact_path",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for meeting in report["meetings"]:
        writer.writerow(
            {
                **{field: meeting.get(field) for field in fieldnames},
                "audit_profile": report["audit_profile"],
                "critical_findings": meeting["severity_counts"]["critical"],
                "high_findings": meeting["severity_counts"]["high"],
                "medium_findings": meeting["severity_counts"]["medium"],
                "low_findings": meeting["severity_counts"]["low"],
                "finding_codes": _finding_code_counts(meeting),
            }
        )
    return output.getvalue()


def _markdown_escape(value: Any) -> str:
    """
    Escape table-sensitive characters for Markdown rendering.

    :param value: Value to render
    :return: Markdown-safe text
    """
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _format_alignment_rate(value: Any) -> str:
    """
    Format an optional alignment rate for human review.

    :param value: Optional numeric alignment rate
    :return: Percentage or dash
    """
    if isinstance(value, (int, float)):
        return f"{float(value):.1%}"
    return "—"


def _append_record_preview(
    lines: List[str],
    label: str,
    preview: Dict[str, Any],
) -> None:
    """
    Append one bounded record preview to Markdown report lines.

    :param lines: Mutable Markdown lines
    :param label: Human-readable preview label
    :param preview: Bounded record preview
    :return: None
    """
    lines.append(
        f"  - {label}: `{_markdown_escape(preview.get('record_type') or 'unknown')}` "
        f"— **{_markdown_escape(preview.get('subject') or 'Untitled record')}**"
    )
    if preview.get("attribution"):
        lines.append(f"    - Attribution: {_markdown_escape(preview['attribution'])}")
    if preview.get("attributes"):
        lines.append(
            "    - Attributes: `"
            + json.dumps(preview["attributes"], ensure_ascii=False, sort_keys=True)
            .replace("`", "\\`")
            + "`"
        )
    if preview.get("summary"):
        lines.append(f"    - Summary: {_markdown_escape(preview['summary'])}")
    if preview.get("evidence"):
        lines.append(f"    - Evidence: “{_markdown_escape(preview['evidence'])}”")
    if preview.get("source_chunks"):
        chunks = ", ".join(str(chunk) for chunk in preview["source_chunks"])
        lines.append(f"    - Source chunks: {chunks}")


def render_audit_markdown(
    report: Dict[str, Any],
    *,
    top: int,
    min_risk: int,
) -> str:
    """
    Render a ranked human-review report from corpus audit data.

    :param report: Complete records audit report
    :param top: Maximum meetings to render with detailed findings
    :param min_risk: Minimum risk score for detailed rendering
    :return: Markdown report text
    """
    summary = report["summary"]
    lines = [
        "# Records Audit",
        "",
        f"Entity: `{report['entity']}`",
        "",
        f"Model: `{report['model']}`",
        "",
        f"Audit profile: `{report['audit_profile']}`",
        "",
        f"Records directory: `{report['records_dir']}`",
        "",
        "This report is deterministic and read-only. Findings are review candidates, "
        "not automatic corrections.",
        "",
        "## Corpus Summary",
        "",
        f"- Meetings audited: {summary['meeting_count']}",
        f"- Meetings with findings: {summary['meetings_with_findings']}",
        f"- Complete artifacts: {summary['complete_artifacts']}",
        f"- Partial or missing artifacts: {summary['partial_or_missing_artifacts']}",
        f"- Total records: {summary['record_count']}",
        f"- Duplicate candidates: {summary['duplicate_candidates']}",
        f"- Quantity mismatch candidates: {summary['summary_evidence_number_mismatches']}",
        f"- Official/public-comment candidates: {summary['official_public_comment_candidates']}",
        "",
        "## Meeting Inventory",
        "",
        "| Risk | Meeting | Status | Type | Chunks | Records | Alignment | Findings |",
        "|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for meeting in report["meetings"]:
        lines.append(
            "| "
            f"{meeting['risk_score']} | "
            f"{_markdown_escape(meeting['meeting'])} | "
            f"{_markdown_escape(meeting['status'])} | "
            f"{_markdown_escape(meeting['meeting_type'] or '—')} | "
            f"{meeting['successful_chunk_count']}/{meeting['chunk_count']} | "
            f"{meeting['record_count']} | "
            f"{_format_alignment_rate(meeting['alignment_rate'])} | "
            f"{len(meeting['findings'])} |"
        )

    lines.extend(["", "## Highest-Risk Meetings", ""])
    detailed = [meeting for meeting in report["meetings"] if meeting["risk_score"] >= min_risk]
    if top >= 0:
        detailed = detailed[:top]
    if not detailed:
        lines.extend(["No meetings met the detailed-review threshold.", ""])
        return "\n".join(lines).strip() + "\n"

    for rank, meeting in enumerate(detailed, start=1):
        lines.extend(
            [
                f"### {rank}. {meeting['meeting']}",
                "",
                f"Risk score: **{meeting['risk_score']}**",
                "",
                f"Artifact: `{meeting['artifact_path'] or 'missing'}`",
                "",
            ]
        )
        for finding in meeting["findings"]:
            record_text = ""
            if finding.get("record_indices"):
                record_text = " Records: " + ", ".join(
                    str(index) for index in finding["record_indices"]
                ) + "."
            lines.append(
                f"- **{finding['severity'].upper()} — {finding['code']}**: "
                f"{finding['message']}{record_text}"
            )
            details = dict(finding.get("details") or {})
            record_preview = details.pop("record", None)
            left_preview = details.pop("left_record", None)
            right_preview = details.pop("right_record", None)
            if isinstance(record_preview, dict):
                _append_record_preview(lines, "Record", record_preview)
            if isinstance(left_preview, dict):
                _append_record_preview(lines, "Left record", left_preview)
            if isinstance(right_preview, dict):
                _append_record_preview(lines, "Right record", right_preview)
            if details:
                lines.append(
                    "  - Diagnostic: `"
                    + json.dumps(details, ensure_ascii=False, sort_keys=True)
                    .replace("`", "\\`")
                    + "`"
                )
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _report_summary(meetings: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """
    Aggregate corpus-level counters from meeting audit results.

    :param meetings: Ranked meeting audit results
    :return: Corpus summary counters
    """
    return {
        "meeting_count": len(meetings),
        "meetings_with_findings": sum(bool(meeting["findings"]) for meeting in meetings),
        "complete_artifacts": sum(meeting["status"] == "complete" for meeting in meetings),
        "partial_or_missing_artifacts": sum(meeting["status"] != "complete" for meeting in meetings),
        "record_count": sum(meeting["record_count"] for meeting in meetings),
        "duplicate_candidates": sum(meeting["duplicate_candidates"] for meeting in meetings),
        "summary_evidence_number_mismatches": sum(
            meeting["summary_evidence_number_mismatches"] for meeting in meetings
        ),
        "official_public_comment_candidates": sum(
            meeting["official_public_comment_candidates"] for meeting in meetings
        ),
        "unknown_attribute_values": sum(meeting["unknown_attribute_values"] for meeting in meetings),
        "critical_findings": sum(meeting["severity_counts"]["critical"] for meeting in meetings),
        "high_findings": sum(meeting["severity_counts"]["high"] for meeting in meetings),
        "medium_findings": sum(meeting["severity_counts"]["medium"] for meeting in meetings),
        "low_findings": sum(meeting["severity_counts"]["low"] for meeting in meetings),
    }

def _report_filenames(profile: AuditProfile) -> Dict[str, str]:
    """
    Resolve report filenames without overwriting balanced results in strict mode.

    Balanced mode preserves the established ``report.*`` filenames. Strict mode
    appends its profile name so both reports can coexist in one audit directory.

    :param profile: Active audit profile
    :return: Mapping of report format to filename
    """
    if profile.name == AUDIT_PROFILE_BALANCED:
        return {
            "json": AUDIT_JSON_NAME,
            "csv": AUDIT_CSV_NAME,
            "markdown": AUDIT_MARKDOWN_NAME,
        }
    return {
        "json": f"report.{profile.name}.json",
        "csv": f"report.{profile.name}.csv",
        "markdown": f"report.{profile.name}.md",
    }
