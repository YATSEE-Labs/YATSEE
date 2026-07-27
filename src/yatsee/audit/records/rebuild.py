"""
Structural rebuild planning and execution for deterministic records audits.

Rebuild eligibility is intentionally narrower than audit severity. Only
structural artifact loss enters this module's policy; heuristic quality findings
remain review-only.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from typing import Any, Dict, Iterable, List, Sequence

from yatsee.core.errors import ValidationError
from yatsee.core.io import ensure_directory, write_text_atomic
from yatsee.core.paths import sanitize_path_component, serialize_project_path
from yatsee.intel.resolve import resolve_record_paths

from .checks import _non_negative_int
from .policy import (
    REBUILD_ARTIFACT_KIND,
    REBUILD_MANIFEST_NAME,
    REBUILD_MIN_REJECTED_ITEMS,
    REBUILD_MIN_REJECTION_RATE,
    REBUILD_MODE_PLAN,
    REBUILD_SCHEMA_VERSION,
    _REBUILD_DIRECT_FINDING_CODES,
)

def _most_common_text(values: Iterable[Any], default: str) -> str:
    """
    Resolve one stable corpus default from artifact provenance.

    :param values: Candidate text values
    :param default: Fallback when no usable values are present
    :return: Most common non-empty text value
    """
    normalized = [
        str(value).strip()
        for value in values
        if isinstance(value, str) and str(value).strip()
    ]
    if not normalized:
        return default
    counts = Counter(normalized)
    return sorted(counts, key=lambda item: (-counts[item], item))[0]


def _resolve_rebuild_source_files(
    *,
    global_config_path: str,
    entity: str,
    model: str | None,
    meeting_filter: str | None,
) -> tuple[List[str], str | None]:
    """
    Discover normalized transcripts used to verify rebuild eligibility.

    Rebuild planning compares source transcripts with the records directory so a
    missing meeting directory is visible. Ordinary audit mode remains records-only.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional configured records model override
    :param meeting_filter: Optional case-insensitive meeting substring
    :return: Matching transcript paths and an optional discovery error
    """
    try:
        resolved = resolve_record_paths(
            global_config_path,
            entity=entity,
            model=model,
        )
    except ValidationError as exc:
        return [], str(exc)

    normalized_filter = meeting_filter.lower() if meeting_filter else None
    files = []
    for path in resolved["file_list"]:
        base_name = sanitize_path_component(
            os.path.splitext(os.path.basename(path))[0]
        )
        if normalized_filter and normalized_filter not in base_name.lower():
            continue
        files.append(os.path.abspath(path))
    return files, None


def _source_file_map(source_files: Sequence[str]) -> Dict[str, str]:
    """
    Map sanitized records meeting names to normalized transcript paths.

    :param source_files: Normalized transcript paths
    :return: Meeting name to absolute transcript path mapping
    :raises ValidationError: If two sources collapse to the same artifact name
    """
    mapped: Dict[str, str] = {}
    for source_path in source_files:
        meeting = sanitize_path_component(
            os.path.splitext(os.path.basename(source_path))[0]
        )
        existing = mapped.get(meeting)
        if existing and os.path.abspath(existing) != os.path.abspath(source_path):
            raise ValidationError(
                "Multiple normalized transcripts resolve to the same records "
                f"meeting name '{meeting}'"
            )
        mapped[meeting] = os.path.abspath(source_path)
    return mapped


def _rebuild_reasons(meeting: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Select only structural findings for which extraction is a rational remedy.

    Finding severity is intentionally not consulted. Quantity heuristics,
    duplicates, attribution quality, public-comment typing, timestamp alignment,
    and other model-quality findings can never trigger this policy.

    :param meeting: One meeting audit result
    :return: Stable rebuild reason dictionaries
    """
    reasons: List[Dict[str, Any]] = []
    seen_codes: set[str] = set()
    for finding in meeting.get("findings", []):
        code = finding.get("code")
        if code not in _REBUILD_DIRECT_FINDING_CODES or code in seen_codes:
            continue
        seen_codes.add(code)
        reasons.append(
            {
                "code": code,
                "message": str(finding.get("message") or code),
            }
        )

    rejected_count = (
        _non_negative_int(meeting.get("rejected_record_count"))
        + _non_negative_int(meeting.get("rejected_line_count"))
    )
    attempted_count = _non_negative_int(meeting.get("record_count")) + rejected_count
    rejection_rate = rejected_count / attempted_count if attempted_count else 0.0
    threshold = max(
        REBUILD_MIN_REJECTED_ITEMS,
        math.ceil(attempted_count * REBUILD_MIN_REJECTION_RATE),
    )
    if rejected_count >= threshold:
        reasons.append(
            {
                "code": "excessive_validation_rejection",
                "message": (
                    f"Extraction rejected {rejected_count} of {attempted_count} "
                    f"record or JSONL candidates ({rejection_rate:.1%})."
                ),
                "rejected_count": rejected_count,
                "attempted_count": attempted_count,
                "rejection_rate": round(rejection_rate, 6),
            }
        )
    return reasons


def build_rebuild_plan(
    report: Dict[str, Any],
    *,
    source_files: Sequence[str],
    project_root: str,
    source_discovery_error: str | None = None,
) -> tuple[Dict[str, Any], Dict[str, str]]:
    """
    Build a deterministic records rebuild plan from one audit report.

    The plan is a recommendation only. It contains no heuristic content-quality
    findings and records whether each candidate has a normalized source available
    for an automatic attempt.

    :param report: Complete records audit report
    :param source_files: Matching normalized transcript paths
    :param project_root: Project root for portable path serialization
    :param source_discovery_error: Optional transcript discovery failure
    :return: Rebuild plan and runtime-only meeting-to-source mapping
    """
    source_map = _source_file_map(source_files)
    meeting_map = {
        meeting["meeting"]: meeting
        for meeting in report.get("meetings", [])
    }
    default_job_profile = _most_common_text(
        (meeting.get("job_profile") for meeting in meeting_map.values()),
        "civic",
    )
    default_chunk_style = _most_common_text(
        (meeting.get("chunk_style") for meeting in meeting_map.values()),
        "density",
    )
    alignment_values = [
        bool(meeting.get("timestamp_alignment_present"))
        for meeting in meeting_map.values()
        if meeting.get("status") == "complete"
    ]
    default_align_timestamps = (
        sum(alignment_values) > len(alignment_values) / 2
        if alignment_values
        else False
    )

    candidate_names = {
        name
        for name, meeting in meeting_map.items()
        if _rebuild_reasons(meeting)
    }
    candidate_names.update(set(source_map) - set(meeting_map))

    candidates: List[Dict[str, Any]] = []
    runtime_sources: Dict[str, str] = {}
    for meeting_name in sorted(candidate_names, key=str.lower):
        meeting = meeting_map.get(meeting_name)
        if meeting is None:
            reasons = [
                {
                    "code": "missing_meeting_artifact_directory",
                    "message": (
                        "Normalized transcript has no matching records meeting "
                        "artifact directory."
                    ),
                }
            ]
        else:
            reasons = _rebuild_reasons(meeting)
        if not reasons:
            continue

        source_path = source_map.get(meeting_name)
        blockers = [] if source_path else ["normalized_transcript_missing"]
        if source_path:
            runtime_sources[meeting_name] = source_path

        meeting_status = meeting.get("status") if meeting else "missing"
        candidate_align_timestamps = (
            bool(meeting.get("timestamp_alignment_present"))
            if meeting and meeting_status == "complete"
            else default_align_timestamps
        )
        candidate = {
            "meeting": meeting_name,
            "rebuildable": source_path is not None,
            "blockers": blockers,
            "source_transcript": (
                serialize_project_path(source_path, project_root)
                if source_path
                else None
            ),
            "artifact_path": meeting.get("artifact_path") if meeting else None,
            "status": meeting_status,
            "record_count": _non_negative_int(
                meeting.get("record_count") if meeting else 0
            ),
            "reasons": reasons,
            "settings": {
                "job_profile": (
                    meeting.get("job_profile")
                    if meeting and meeting.get("job_profile")
                    else default_job_profile
                ),
                "chunk_style": (
                    meeting.get("chunk_style")
                    if meeting and meeting.get("chunk_style")
                    else default_chunk_style
                ),
                "meeting_type": (
                    meeting.get("meeting_type")
                    if meeting
                    and isinstance(meeting.get("meeting_type"), str)
                    and meeting.get("meeting_type").strip()
                    else None
                ),
                "align_timestamps": candidate_align_timestamps,
            },
        }
        candidates.append(candidate)

    rebuildable_count = sum(candidate["rebuildable"] for candidate in candidates)
    plan = {
        "artifact_kind": REBUILD_ARTIFACT_KIND,
        "schema_version": REBUILD_SCHEMA_VERSION,
        "mode": REBUILD_MODE_PLAN,
        "entity": report["entity"],
        "model": report["model"],
        "audit_profile": report["audit_profile"],
        "records_dir": report["records_dir"],
        "meeting_filter": report.get("meeting_filter"),
        "candidate_count": len(candidates),
        "rebuildable_count": rebuildable_count,
        "blocked_count": len(candidates) - rebuildable_count,
        "source_discovery_error": source_discovery_error,
        "policy": {
            "direct_finding_codes": sorted(_REBUILD_DIRECT_FINDING_CODES),
            "minimum_rejected_items": REBUILD_MIN_REJECTED_ITEMS,
            "minimum_rejection_rate": REBUILD_MIN_REJECTION_RATE,
            "heuristic_findings_trigger_rebuild": False,
            "maximum_attempts_per_invocation": 1,
        },
        "candidates": candidates,
    }
    return plan, runtime_sources


def _write_rebuild_manifest(
    plan: Dict[str, Any],
    *,
    output_dir: str,
) -> str:
    """
    Atomically write one rebuild plan or execution manifest.

    :param plan: Rebuild plan dictionary
    :param output_dir: Audit output directory
    :return: Absolute manifest path
    """
    ensure_directory(output_dir)
    path = os.path.join(output_dir, REBUILD_MANIFEST_NAME)
    write_text_atomic(path, json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    return path


def _execute_rebuild_plan(
    plan: Dict[str, Any],
    *,
    runtime_sources: Dict[str, str],
    global_config_path: str,
    entity: str,
    model: str,
    records_dir: str,
) -> List[Dict[str, Any]]:
    """
    Attempt each rebuildable candidate exactly once.

    The records stage keeps an existing complete artifact until a replacement is
    complete and atomically writable. Failures are isolated per meeting so one
    provider or model error does not consume repeated attempts or stop the batch.

    :param plan: Rebuild plan dictionary
    :param runtime_sources: Absolute source paths keyed by meeting name
    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Resolved records model
    :param records_dir: Model-specific records output directory
    :return: Per-meeting execution results
    """
    from yatsee.intel.record import run_records_stage

    executions: List[Dict[str, Any]] = []
    for candidate in plan.get("candidates", []):
        meeting = candidate["meeting"]
        if not candidate.get("rebuildable"):
            executions.append(
                {
                    "meeting": meeting,
                    "status": "blocked",
                    "message": "Normalized transcript is unavailable.",
                }
            )
            continue

        settings = candidate.get("settings", {})
        try:
            stage_result = run_records_stage(
                global_config_path=global_config_path,
                entity=entity,
                input_path=runtime_sources[meeting],
                output_dir=records_dir,
                model=model,
                job_profile=str(settings.get("job_profile") or "civic"),
                chunk_style=str(settings.get("chunk_style") or "density"),
                meeting_type=settings.get("meeting_type"),
                force=True,
                align_timestamps=bool(settings.get("align_timestamps")),
            )
            item = stage_result["results"][0]
            completed = (
                item.get("status") == "complete"
                and item.get("failed_chunk_count", 0) == 0
                and item.get("partial_chunk_count", 0) == 0
            )
            executions.append(
                {
                    "meeting": meeting,
                    "status": "complete" if completed else "incomplete",
                    "artifact_path": item.get("output_path"),
                    "record_count": item.get("record_count", 0),
                    "failed_chunk_count": item.get("failed_chunk_count", 0),
                    "partial_chunk_count": item.get("partial_chunk_count", 0),
                    "validation_warning_count": item.get(
                        "validation_warning_count",
                        0,
                    ),
                }
            )
        except Exception as exc:
            # Automatic mode is an operator convenience boundary. Isolate model,
            # provider, and filesystem failures per meeting and never retry here.
            executions.append(
                {
                    "meeting": meeting,
                    "status": "failed",
                    "message": str(exc),
                }
            )
    return executions
