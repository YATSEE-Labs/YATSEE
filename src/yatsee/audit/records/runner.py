"""
Public orchestration entry point for deterministic records corpus auditing.

This module resolves paths, runs meeting checks, writes requested reports, and
optionally coordinates one bounded structural rebuild pass.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict

from yatsee.core.errors import ValidationError
from yatsee.core.io import ensure_directory, write_text_atomic
from yatsee.core.paths import (
    resolve_contained_path,
    sanitize_path_component,
    serialize_project_path,
)
from yatsee.audit.resolve import resolve_records_audit_paths

from .checks import audit_meeting_directory
from .policy import (
    AUDIT_ARTIFACT_KIND,
    AUDIT_PROFILE_BALANCED,
    AUDIT_SCHEMA_VERSION,
    REBUILD_MODE_AUTO,
    REBUILD_MODE_PLAN,
    _resolve_audit_profile,
)
from .rebuild import (
    _execute_rebuild_plan,
    _resolve_rebuild_source_files,
    _write_rebuild_manifest,
    build_rebuild_plan,
)
from .report import (
    _apply_distribution_findings,
    _discover_meeting_directories,
    _report_filenames,
    _report_summary,
    render_audit_csv,
    render_audit_markdown,
)


def _project_root(global_config_path: str) -> str:
    """
    Resolve the project root from the active global configuration path.

    :param global_config_path: Path to yatsee.toml
    :return: Absolute project root directory
    """
    return str(Path(global_config_path).resolve().parent)


def run_records_audit(
    *,
    global_config_path: str,
    entity: str,
    model: str | None = None,
    output_dir: str | None = None,
    meeting_filter: str | None = None,
    output_format: str = "all",
    top: int = 20,
    min_risk: int = 1,
    audit_profile: str = AUDIT_PROFILE_BALANCED,
    rebuild_mode: str | None = None,
    _allow_empty_records: bool = False,
) -> Dict[str, Any]:
    """
    Audit a model-specific entity records corpus and write review reports.

    Normal and strict audit modes remain read-only. Optional rebuild planning
    applies a separate structural policy that ignores heuristic model-quality
    findings. Automatic mode attempts each rebuildable meeting once and then
    reruns the audit to report the resulting state.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional configured records model override
    :param output_dir: Optional audit report directory override
    :param meeting_filter: Optional meeting directory substring
    :param output_format: all, json, csv, or markdown
    :param top: Maximum detailed meetings in Markdown; zero disables details
    :param min_risk: Minimum risk score for Markdown detail
    :param audit_profile: Explicit balanced or strict audit profile
    :param rebuild_mode: None, plan, or auto
    :param _allow_empty_records: Internal support for post-rebuild empty roots
    :return: Audit execution metadata, report paths, and optional rebuild data
    :raises ValidationError: If arguments or records paths are invalid
    """
    profile = _resolve_audit_profile(audit_profile)
    if output_format not in {"all", "json", "csv", "markdown"}:
        raise ValidationError(f"Unsupported audit output format '{output_format}'")
    if top < 0:
        raise ValidationError("Audit --top must be zero or greater")
    if min_risk < 0:
        raise ValidationError("Audit --min-risk must be zero or greater")
    if rebuild_mode not in {None, REBUILD_MODE_PLAN, REBUILD_MODE_AUTO}:
        raise ValidationError(
            "Audit rebuild mode must be 'plan', 'auto', or omitted"
        )

    resolved = resolve_records_audit_paths(
        global_config_path,
        entity=entity,
        model=model,
        output_dir=output_dir,
    )
    if meeting_filter and output_dir is None:
        # A focused audit must not replace the canonical full-corpus report.
        # Store filtered reports beneath a stable, contained meeting namespace.
        report_component = sanitize_path_component(meeting_filter)
        resolved["output_dir"] = resolve_contained_path(
            resolved["output_dir"],
            "meetings",
            report_component,
        )
    project_root = _project_root(global_config_path)
    meeting_dirs = _discover_meeting_directories(
        resolved["records_dir"],
        output_dir=resolved["output_dir"],
        meeting_filter=meeting_filter,
        allow_empty=bool(rebuild_mode) or _allow_empty_records,
    )
    meetings = [
        audit_meeting_directory(
            meeting_dir,
            entity_cfg=resolved["entity_cfg"],
            project_root=project_root,
            profile=profile,
        )
        for meeting_dir in meeting_dirs
    ]
    _apply_distribution_findings(meetings)
    meetings.sort(key=lambda item: (-item["risk_score"], item["meeting"].lower()))

    report = {
        "artifact_kind": AUDIT_ARTIFACT_KIND,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "entity": entity,
        "model": resolved["model"],
        "audit_profile": profile.name,
        "records_dir": serialize_project_path(resolved["records_dir"], project_root),
        "meeting_filter": meeting_filter,
        "summary": _report_summary(meetings),
        "meetings": meetings,
    }

    ensure_directory(resolved["output_dir"])
    report_filenames = _report_filenames(profile)
    paths = {
        report_format: os.path.join(resolved["output_dir"], filename)
        for report_format, filename in report_filenames.items()
    }
    written_paths: Dict[str, str] = {}
    if output_format in {"all", "json"}:
        write_text_atomic(
            paths["json"],
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        )
        written_paths["json"] = paths["json"]
    if output_format in {"all", "csv"}:
        write_text_atomic(paths["csv"], render_audit_csv(report))
        written_paths["csv"] = paths["csv"]
    if output_format in {"all", "markdown"}:
        write_text_atomic(
            paths["markdown"],
            render_audit_markdown(report, top=top, min_risk=min_risk),
        )
        written_paths["markdown"] = paths["markdown"]

    result = {
        "entity": entity,
        "model": resolved["model"],
        "audit_profile": profile.name,
        "records_dir": resolved["records_dir"],
        "output_dir": resolved["output_dir"],
        "meeting_count": report["summary"]["meeting_count"],
        "meetings_with_findings": report["summary"]["meetings_with_findings"],
        "critical_findings": report["summary"]["critical_findings"],
        "high_findings": report["summary"]["high_findings"],
        "medium_findings": report["summary"]["medium_findings"],
        "low_findings": report["summary"]["low_findings"],
        "report_paths": written_paths,
        "report": report,
    }
    if rebuild_mode is None:
        return result

    source_files, source_discovery_error = _resolve_rebuild_source_files(
        global_config_path=global_config_path,
        entity=entity,
        model=resolved["model"],
        meeting_filter=meeting_filter,
    )
    plan, runtime_sources = build_rebuild_plan(
        report,
        source_files=source_files,
        project_root=project_root,
        source_discovery_error=source_discovery_error,
    )
    plan["mode"] = rebuild_mode
    manifest_path = _write_rebuild_manifest(
        plan,
        output_dir=resolved["output_dir"],
    )
    result.update(
        {
            "rebuild_mode": rebuild_mode,
            "rebuild_candidate_count": plan["candidate_count"],
            "rebuildable_count": plan["rebuildable_count"],
            "rebuild_blocked_count": plan["blocked_count"],
            "rebuild_manifest_path": manifest_path,
            "rebuild_plan": plan,
        }
    )
    if rebuild_mode == REBUILD_MODE_PLAN or plan["rebuildable_count"] == 0:
        return result

    executions = _execute_rebuild_plan(
        plan,
        runtime_sources=runtime_sources,
        global_config_path=global_config_path,
        entity=entity,
        model=resolved["model"],
        records_dir=resolved["records_dir"],
    )
    execution_counts = Counter(execution["status"] for execution in executions)
    plan["execution"] = executions
    plan["execution_summary"] = {
        "attempted": sum(
            execution["status"] != "blocked" for execution in executions
        ),
        "complete": execution_counts.get("complete", 0),
        "incomplete": execution_counts.get("incomplete", 0),
        "failed": execution_counts.get("failed", 0),
        "blocked": execution_counts.get("blocked", 0),
    }
    _write_rebuild_manifest(plan, output_dir=resolved["output_dir"])

    # Re-audit exactly once after the batch. This measures whether structural
    # rebuild reasons cleared without creating a retry loop.
    post_result = run_records_audit(
        global_config_path=global_config_path,
        entity=entity,
        model=resolved["model"],
        output_dir=output_dir,
        meeting_filter=meeting_filter,
        output_format=output_format,
        top=top,
        min_risk=min_risk,
        audit_profile=profile.name,
        rebuild_mode=None,
        _allow_empty_records=True,
    )
    post_plan, _ = build_rebuild_plan(
        post_result["report"],
        source_files=source_files,
        project_root=project_root,
        source_discovery_error=source_discovery_error,
    )
    plan["remaining_candidate_count"] = post_plan["candidate_count"]
    plan["remaining_rebuildable_count"] = post_plan["rebuildable_count"]
    plan["remaining_candidates"] = [
        {
            "meeting": candidate["meeting"],
            "rebuildable": candidate["rebuildable"],
            "reasons": candidate["reasons"],
        }
        for candidate in post_plan["candidates"]
    ]
    _write_rebuild_manifest(plan, output_dir=resolved["output_dir"])

    post_result.update(
        {
            "rebuild_mode": rebuild_mode,
            "rebuild_candidate_count": plan["candidate_count"],
            "rebuildable_count": plan["rebuildable_count"],
            "rebuild_blocked_count": plan["blocked_count"],
            "rebuild_manifest_path": manifest_path,
            "rebuild_plan": plan,
            "rebuild_execution": executions,
            "rebuild_execution_summary": plan["execution_summary"],
            "remaining_rebuild_candidate_count": post_plan["candidate_count"],
        }
    )
    return post_result
