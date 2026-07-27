"""
Deterministic QA reporting for YATSEE VTT transcript artifacts.

The audit detects repeated ASR loops, implausibly dense cues, low-information
long cues, and timestamp ordering problems. It never modifies transcript files.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Sequence

from yatsee.audit.resolve import resolve_transcript_audit_paths
from yatsee.core.errors import ValidationError
from yatsee.core.io import ensure_directory, write_text_atomic
from yatsee.core.paths import serialize_project_path

TIME_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
SKIP_DIR_MARKERS = {"old", "bak", "backup", "backups"}
STATUS_RANK = {"block": 4, "rerun": 3, "review": 2, "pass": 0}
AUDIT_ARTIFACT_KIND = "transcript_audit"
AUDIT_SCHEMA_VERSION = "transcript_audit_v1"


@dataclass(frozen=True)
class TranscriptAuditSettings:
    """
    Thresholds used by deterministic transcript checks.

    :param prefix_words: Leading words used for repeated cue matching
    :param max_words_per_second: Maximum plausible cue speech rate
    :param chunk_duration: Expected transcription chunk duration in seconds
    :param overlap_seconds: Expected transcription chunk overlap in seconds
    :param seam_threshold: Distance from an estimated seam considered adjacent
    """

    prefix_words: int = 16
    max_words_per_second: float = 4.0
    chunk_duration: float = 600.0
    overlap_seconds: float = 2.0
    seam_threshold: float = 3.0


def should_skip_path(path: Path) -> bool:
    """
    Return whether a path belongs to an old or backup artifact tree.

    :param path: Candidate path
    :return: True when the path should be skipped
    """
    for part in path.parts:
        lowered = part.casefold()
        if lowered in SKIP_DIR_MARKERS:
            return True
        if lowered.endswith("_old") or lowered.endswith("_bak"):
            return True
    return False


def timestamp_to_seconds(timestamp: str) -> float:
    """
    Convert a VTT timestamp to seconds.

    :param timestamp: VTT timestamp
    :return: Timestamp in seconds
    """
    hours, minutes, rest = timestamp.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def word_count(text: str) -> int:
    """
    Count word-like tokens in cue text.

    :param text: Cue text
    :return: Word count
    """
    return len(re.findall(r"\b[\w']+\b", text))


def normalize_key(text: str, prefix_words: int) -> str:
    """
    Normalize cue text into a repeated-prefix comparison key.

    :param text: Cue text
    :param prefix_words: Number of leading words to retain
    :return: Normalized key or an empty string when too short
    """
    normalized = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    words = normalized.split()
    if len(words) < 8:
        return ""
    key = " ".join(words[:prefix_words])
    return key if len(key) >= 50 else ""


def parse_vtt(path: Path) -> list[dict[str, Any]]:
    """
    Parse VTT cues without requiring an optional VTT dependency.

    :param path: VTT file path
    :return: Parsed cue dictionaries
    """
    cues: list[dict[str, Any]] = []
    current: tuple[str, str] | None = None
    text_parts: list[str] = []

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        match = TIME_RE.search(line)
        if match:
            if current and text_parts:
                cues.append(
                    {
                        "index": len(cues),
                        "start": current[0],
                        "end": current[1],
                        "start_seconds": timestamp_to_seconds(current[0]),
                        "end_seconds": timestamp_to_seconds(current[1]),
                        "text": " ".join(text_parts).strip(),
                    }
                )
            current = (match.group("start"), match.group("end"))
            text_parts = []
            continue

        if not line or line == "WEBVTT" or line.isdigit():
            continue
        if current:
            text_parts.append(line)

    if current and text_parts:
        cues.append(
            {
                "index": len(cues),
                "start": current[0],
                "end": current[1],
                "start_seconds": timestamp_to_seconds(current[0]),
                "end_seconds": timestamp_to_seconds(current[1]),
                "text": " ".join(text_parts).strip(),
            }
        )
    return cues


def classify_loop(count: int, span_seconds: float) -> str:
    """
    Classify one adjacent repeated cue run.

    :param count: Repeated cue count
    :param span_seconds: Duration covered by the run
    :return: Severity label
    """
    if count >= 8 or span_seconds >= 240:
        return "critical"
    if count >= 4 or span_seconds >= 90:
        return "high"
    if count >= 3 or span_seconds >= 45:
        return "medium"
    return "low"


def find_asr_loops(
    cues: list[dict[str, Any]],
    prefix_words: int,
) -> list[dict[str, Any]]:
    """
    Find adjacent repeated cue-prefix runs.

    Ceremonial and procedural language is not exempt because long repeated runs
    of that content can still represent ASR corruption.

    :param cues: Parsed VTT cues
    :param prefix_words: Prefix word count used for matching
    :return: Medium, high, and critical ASR loop findings
    """
    loops: list[dict[str, Any]] = []
    run_key = ""
    run_start = ""
    run_end = ""
    run_start_seconds = 0.0
    run_end_seconds = 0.0
    run_text = ""
    run_count = 0

    def flush_run() -> None:
        """
        Emit the current repeated-prefix run when actionable.

        :return: None
        """
        nonlocal run_key, run_start, run_end, run_text, run_count
        nonlocal run_start_seconds, run_end_seconds

        if not run_key or run_count < 2:
            return
        span_seconds = run_end_seconds - run_start_seconds
        severity = classify_loop(run_count, span_seconds)
        if severity == "low":
            return
        loops.append(
            {
                "type": "asr_loop",
                "severity": severity,
                "action": "rerun",
                "fixable": False,
                "count": run_count,
                "span_seconds": round(span_seconds, 3),
                "start": run_start,
                "end": run_end,
                "start_seconds": round(run_start_seconds, 3),
                "end_seconds": round(run_end_seconds, 3),
                "sample": run_text[:160],
            }
        )

    for cue in cues:
        key = normalize_key(cue["text"], prefix_words)
        if key and key == run_key:
            run_end = cue["end"]
            run_end_seconds = cue["end_seconds"]
            run_count += 1
            continue

        flush_run()
        if key:
            run_key = key
            run_start = cue["start"]
            run_end = cue["end"]
            run_start_seconds = cue["start_seconds"]
            run_end_seconds = cue["end_seconds"]
            run_text = cue["text"]
            run_count = 1
        else:
            run_key = ""
            run_start = ""
            run_end = ""
            run_start_seconds = 0.0
            run_end_seconds = 0.0
            run_text = ""
            run_count = 0

    flush_run()
    return loops


def nearest_chunk_seam_distance(
    start_seconds: float,
    chunk_duration: float,
    overlap_seconds: float,
) -> float:
    """
    Estimate distance from a cue start to the nearest chunk seam.

    :param start_seconds: Cue start time in seconds
    :param chunk_duration: Expected chunk duration
    :param overlap_seconds: Expected chunk overlap
    :return: Absolute distance to the nearest estimated seam
    """
    stride = chunk_duration - overlap_seconds
    if stride <= 0:
        return 0.0
    return abs(start_seconds - round(start_seconds / stride) * stride)


def find_impossible_cues(
    cues: list[dict[str, Any]],
    settings: TranscriptAuditSettings,
) -> list[dict[str, Any]]:
    """
    Find cue timing-density issues that may be repairable.

    :param cues: Parsed VTT cues
    :param settings: Audit thresholds
    :return: Implausible cue findings
    """
    issues: list[dict[str, Any]] = []
    for cue in cues:
        duration = cue["end_seconds"] - cue["start_seconds"]
        words = word_count(cue["text"])
        if duration <= 0:
            continue
        words_per_second = words / duration
        minimum_duration = words / settings.max_words_per_second
        legacy_trigger = (duration < 1.0 and words > 8) or (
            duration < 2.0 and words > 20
        )
        speech_rate_trigger = words >= 6 and duration < minimum_duration
        if not legacy_trigger and not speech_rate_trigger:
            continue

        seam_distance = nearest_chunk_seam_distance(
            cue["start_seconds"],
            settings.chunk_duration,
            settings.overlap_seconds,
        )
        seam_adjacent = seam_distance <= settings.seam_threshold
        issues.append(
            {
                "type": "impossible_cue",
                "severity": "warning" if seam_adjacent else "medium",
                "action": "fix",
                "fixable": True,
                "cue_index": cue["index"],
                "start": cue["start"],
                "end": cue["end"],
                "start_seconds": round(cue["start_seconds"], 3),
                "end_seconds": round(cue["end_seconds"], 3),
                "duration_seconds": round(duration, 3),
                "minimum_duration_seconds": round(minimum_duration, 3),
                "words": words,
                "words_per_second": round(words_per_second, 2),
                "seam_distance_seconds": round(seam_distance, 3),
                "seam_adjacent": seam_adjacent,
                "sample": cue["text"][:160],
            }
        )
    return issues


def find_long_low_info(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Find long cues containing very little text.

    :param cues: Parsed VTT cues
    :return: Report-only low-information findings
    """
    issues: list[dict[str, Any]] = []
    for cue in cues:
        duration = cue["end_seconds"] - cue["start_seconds"]
        words = word_count(cue["text"])
        if duration < 20 or words > 3:
            continue
        issues.append(
            {
                "type": "long_low_info_cue",
                "severity": "warning",
                "action": "report",
                "fixable": False,
                "cue_index": cue["index"],
                "start": cue["start"],
                "end": cue["end"],
                "start_seconds": round(cue["start_seconds"], 3),
                "end_seconds": round(cue["end_seconds"], 3),
                "duration_seconds": round(duration, 3),
                "words": words,
                "sample": cue["text"][:160],
            }
        )
    return issues


def find_timestamp_order_issues(
    cues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Find non-positive durations and overlapping cue timestamps.

    :param cues: Parsed VTT cues
    :return: Timestamp findings
    """
    issues: list[dict[str, Any]] = []
    previous_end = 0.0
    for cue in cues:
        start = cue["start_seconds"]
        end = cue["end_seconds"]
        if end <= start:
            issues.append(
                {
                    "type": "non_positive_duration",
                    "severity": "medium",
                    "action": "fix",
                    "fixable": True,
                    "cue_index": cue["index"],
                    "start": cue["start"],
                    "end": cue["end"],
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                    "sample": cue["text"][:160],
                }
            )
        if start < previous_end:
            issues.append(
                {
                    "type": "timestamp_overlap",
                    "severity": "warning",
                    "action": "fix",
                    "fixable": True,
                    "cue_index": cue["index"],
                    "start": cue["start"],
                    "end": cue["end"],
                    "start_seconds": round(start, 3),
                    "end_seconds": round(end, 3),
                    "previous_end_seconds": round(previous_end, 3),
                    "overlap_seconds": round(previous_end - start, 3),
                    "sample": cue["text"][:160],
                }
            )
        previous_end = max(previous_end, end)
    return issues


def classify_file(loops: list[dict[str, Any]]) -> str:
    """
    Classify a VTT from ASR loop severity.

    :param loops: ASR loop findings
    :return: pass, review, rerun, or block
    """
    if not loops:
        return "pass"
    max_span = max(loop["span_seconds"] for loop in loops)
    max_count = max(loop["count"] for loop in loops)
    severities = {loop["severity"] for loop in loops}
    if "critical" in severities or max_span >= 240 or max_count >= 8:
        return "block"
    if "high" in severities or max_span >= 90 or max_count >= 4 or len(loops) >= 3:
        return "rerun"
    return "review"


def scan_file(
    path: Path,
    settings: TranscriptAuditSettings,
    *,
    project_root: Path,
) -> dict[str, Any]:
    """
    Audit one VTT file.

    :param path: VTT file path
    :param settings: Audit thresholds
    :param project_root: Project root for portable path serialization
    :return: File audit result
    """
    cues = parse_vtt(path)
    loops = find_asr_loops(cues, settings.prefix_words)
    impossible_cues = find_impossible_cues(cues, settings)
    low_info = find_long_low_info(cues)
    timestamp_issues = find_timestamp_order_issues(cues)
    issues = loops + impossible_cues + low_info + timestamp_issues
    duration_seconds = cues[-1]["end_seconds"] if cues else 0.0

    status = classify_file(loops)
    return {
        "path": serialize_project_path(path, project_root),
        "filename": path.name,
        "status": status,
        "action": (
            "rerun"
            if status in {"block", "rerun"}
            else "review"
            if status == "review"
            else "pass"
        ),
        "duration_seconds": round(duration_seconds, 3),
        "duration_hours": round(duration_seconds / 3600, 3),
        "cue_count": len(cues),
        "loop_count": len(loops),
        "max_loop_span": round(
            max((loop["span_seconds"] for loop in loops), default=0.0),
            3,
        ),
        "total_loop_span": round(
            sum(loop["span_seconds"] for loop in loops),
            3,
        ),
        "max_loop_count": max((loop["count"] for loop in loops), default=0),
        "fixable_count": sum(1 for issue in issues if issue["fixable"]),
        "warning_count": len(impossible_cues) + len(low_info) + len(timestamp_issues),
        "impossible_cue_count": len(impossible_cues),
        "seam_impossible_cue_count": sum(
            1 for issue in impossible_cues if issue["seam_adjacent"]
        ),
        "long_low_info_count": len(low_info),
        "timestamp_issue_count": len(timestamp_issues),
        "loops": loops,
        "issues": issues,
    }


def collect_vtt_paths(inputs: Sequence[str]) -> list[Path]:
    """
    Collect active VTT files from explicit files or directories.

    :param inputs: Input paths
    :return: Sorted VTT paths
    """
    paths: list[Path] = []
    for item in inputs:
        path = Path(item).expanduser().resolve()
        if not path.exists() or should_skip_path(path):
            continue
        if path.is_file() and path.suffix.casefold() == ".vtt":
            paths.append(path)
            continue
        if path.is_dir():
            for candidate in path.rglob("*.vtt"):
                if not should_skip_path(candidate):
                    paths.append(candidate.resolve())
    return sorted(set(paths))


def build_report(
    *,
    entity: str,
    model: str,
    input_path: str,
    settings: TranscriptAuditSettings,
    results: list[dict[str, Any]],
    project_root: Path,
) -> dict[str, Any]:
    """
    Build the machine-readable transcript audit report.

    :param entity: Entity handle
    :param model: Transcription model suffix
    :param input_path: Audited VTT input path
    :param settings: Audit thresholds
    :param results: Per-file audit results
    :param project_root: Project root for portable path serialization
    :return: Complete audit report
    """
    status_counts = {
        status: sum(1 for result in results if result["status"] == status)
        for status in ("block", "rerun", "review", "pass")
    }
    return {
        "artifact_kind": AUDIT_ARTIFACT_KIND,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "entity": entity,
        "model": model,
        "input_path": serialize_project_path(input_path, project_root),
        "summary": {
            "scanned_vtt_files": len(results),
            "status_counts": status_counts,
            "issue_counts": {
                "asr_loop": sum(result["loop_count"] for result in results),
                "impossible_cue": sum(
                    result["impossible_cue_count"] for result in results
                ),
                "seam_impossible_cue": sum(
                    result["seam_impossible_cue_count"] for result in results
                ),
                "long_low_info_cue": sum(
                    result["long_low_info_count"] for result in results
                ),
                "timestamp_issue": sum(
                    result["timestamp_issue_count"] for result in results
                ),
                "fixable": sum(result["fixable_count"] for result in results),
            },
            "settings": {
                "prefix_words": settings.prefix_words,
                "max_words_per_second": settings.max_words_per_second,
                "chunk_duration": settings.chunk_duration,
                "overlap_seconds": settings.overlap_seconds,
                "seam_threshold": settings.seam_threshold,
            },
        },
        "files": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """
    Render a human-review transcript audit report.

    :param report: Complete transcript audit report
    :return: Markdown report
    """
    summary = report["summary"]
    status_counts = summary["status_counts"]
    issue_counts = summary["issue_counts"]
    lines = [
        "# Transcript Audit",
        "",
        f"Entity: `{report['entity']}`",
        "",
        f"Transcription model: `{report['model']}`",
        "",
        f"Input: `{report['input_path']}`",
        "",
        "This report is deterministic and read-only. ASR loop findings may require "
        "retranscription; timing findings require review before repair.",
        "",
        "## Corpus Summary",
        "",
        f"- VTT files scanned: {summary['scanned_vtt_files']}",
        f"- Block: {status_counts['block']}",
        f"- Rerun: {status_counts['rerun']}",
        f"- Review: {status_counts['review']}",
        f"- Pass: {status_counts['pass']}",
        f"- ASR loops: {issue_counts['asr_loop']}",
        f"- Fixable timing candidates: {issue_counts['fixable']}",
        "",
        "## File Inventory",
        "",
        "| Status | File | Duration | Cues | Loops | Max loop | Fixable | Warnings |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in sorted(
        report["files"],
        key=lambda item: (
            -STATUS_RANK[item["status"]],
            -item["max_loop_span"],
            item["filename"].casefold(),
        ),
    ):
        lines.append(
            f"| {result['status']} | {result['filename']} | "
            f"{result['duration_hours']:.2f}h | {result['cue_count']} | "
            f"{result['loop_count']} | {result['max_loop_span']:.1f}s | "
            f"{result['fixable_count']} | {result['warning_count']} |"
        )

    detailed = [result for result in report["files"] if result["issues"]]
    lines.extend(["", "## Findings", ""])
    if not detailed:
        lines.extend(["No findings.", ""])
        return "\n".join(lines)

    for result in detailed:
        lines.extend([f"### {result['filename']}", ""])
        for issue in result["issues"]:
            lines.append(
                f"- **{issue['severity'].upper()} — {issue['type']}** "
                f"{issue['start']} → {issue['end']}: {issue['sample']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_transcript_audit(
    *,
    global_config_path: str,
    entity: str,
    model: str | None = None,
    input_path: str | None = None,
    output_dir: str | None = None,
    output_format: str = "all",
    prefix_words: int = 16,
    max_words_per_second: float = 4.0,
    chunk_duration: float = 600.0,
    overlap_seconds: float = 2.0,
    seam_threshold: float = 3.0,
) -> Dict[str, Any]:
    """
    Audit one entity's VTT transcript artifacts and write reports.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional transcription model suffix override
    :param input_path: Optional VTT file or directory override
    :param output_dir: Optional report output directory override
    :param output_format: all, json, or markdown
    :param prefix_words: Leading words used for repeated cue matching
    :param max_words_per_second: Maximum plausible cue speech rate
    :param chunk_duration: Expected transcription chunk duration
    :param overlap_seconds: Expected transcription chunk overlap
    :param seam_threshold: Distance from a chunk seam considered adjacent
    :return: Audit execution metadata and report paths
    :raises ValidationError: If arguments or input paths are invalid
    """
    if not entity:
        raise ValidationError("Transcript audit requires --entity")
    if output_format not in {"all", "json", "markdown"}:
        raise ValidationError(
            f"Unsupported transcript audit output format '{output_format}'"
        )
    if prefix_words <= 0:
        raise ValidationError("Transcript audit --prefix-words must be positive")
    if max_words_per_second <= 0:
        raise ValidationError(
            "Transcript audit --max-words-per-second must be positive"
        )
    if chunk_duration <= 0 or overlap_seconds < 0:
        raise ValidationError("Transcript audit chunk settings are invalid")
    if overlap_seconds >= chunk_duration:
        raise ValidationError(
            "Transcript audit overlap must be less than chunk duration"
        )
    if seam_threshold < 0:
        raise ValidationError("Transcript audit --seam-threshold cannot be negative")

    resolved = resolve_transcript_audit_paths(
        global_config_path,
        entity=entity,
        model=model,
        input_path=input_path,
        output_dir=output_dir,
    )
    input_paths = collect_vtt_paths([resolved["input_path"]])
    if not input_paths:
        raise ValidationError(f"No VTT files found in: {resolved['input_path']}")

    settings = TranscriptAuditSettings(
        prefix_words=prefix_words,
        max_words_per_second=max_words_per_second,
        chunk_duration=chunk_duration,
        overlap_seconds=overlap_seconds,
        seam_threshold=seam_threshold,
    )
    project_root = Path(global_config_path).expanduser().resolve().parent
    results = [
        scan_file(path, settings, project_root=project_root)
        for path in input_paths
    ]
    report = build_report(
        entity=entity,
        model=resolved["model"],
        input_path=resolved["input_path"],
        settings=settings,
        results=results,
        project_root=project_root,
    )

    ensure_directory(resolved["output_dir"])
    paths = {
        "json": str(Path(resolved["output_dir"]) / "report.json"),
        "markdown": str(Path(resolved["output_dir"]) / "report.md"),
    }
    written_paths: dict[str, str] = {}
    if output_format in {"all", "json"}:
        write_text_atomic(
            paths["json"],
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        )
        written_paths["json"] = paths["json"]
    if output_format in {"all", "markdown"}:
        write_text_atomic(paths["markdown"], render_markdown(report))
        written_paths["markdown"] = paths["markdown"]

    status_counts = report["summary"]["status_counts"]
    return {
        "entity": entity,
        "model": resolved["model"],
        "input_path": resolved["input_path"],
        "output_dir": resolved["output_dir"],
        "file_count": report["summary"]["scanned_vtt_files"],
        "status_counts": status_counts,
        "issue_counts": report["summary"]["issue_counts"],
        "report_paths": written_paths,
        "report": report,
        "exit_code": 1 if status_counts["block"] or status_counts["rerun"] else 0,
    }
