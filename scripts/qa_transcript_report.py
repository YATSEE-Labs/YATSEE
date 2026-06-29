#!/usr/bin/env python3
"""
Transcript QA report for YATSEE VTT artifacts.

Default:
    python scripts/qa_transcript_report.py

Scans:
    ./data/**/*.vtt

Skips:
    old/
    bak/
    backup/
    backups/
    *_old/
    *_bak/

Main status signal:
    ASR loops = adjacent repeated cue-prefix events

QA boundaries:
    - ASR loops are content corruption. They are not fixable here.
    - Timestamp/cue plausibility issues are fixable candidates.
    - This script reports issues only. It does not modify files.

Default output shows files with status review/rerun/block.
Use --warnings to also show warning-only files.
Use --all to show every scanned file.
Use --details to print issue details.
Use --json PATH to write a machine-readable report.
Use --json - to print only JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence


TIME_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})"
)

SKIP_DIR_MARKERS = {
    "old",
    "bak",
    "backup",
    "backups",
}

LOOP_SEVERITY_RANK = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 0,
}

STATUS_RANK = {
    "block": 4,
    "rerun": 3,
    "review": 2,
    "pass": 0,
}

ISSUE_SEVERITY_RANK = {
    "block": 5,
    "critical": 4,
    "high": 3,
    "medium": 2,
    "warning": 1,
    "low": 0,
}


def should_skip_path(path: Path) -> bool:
    """
    Return True for old/bak/archive-style paths.

    :param path: Candidate path
    :return: True if path should be skipped
    """
    for part in path.parts:
        lowered = part.casefold()

        if lowered in SKIP_DIR_MARKERS:
            return True

        if lowered.endswith("_old") or lowered.endswith("_bak"):
            return True

    return False


def entity_name_for_path(path: Path) -> str:
    """
    Return entity name from a data/<entity>/... path.

    :param path: VTT path
    :return: Entity handle
    """
    parts = path.parts

    if "data" in parts:
        data_index = parts.index("data")
        if len(parts) > data_index + 1:
            return parts[data_index + 1]

    return "unknown_entity"


def timestamp_to_seconds(timestamp: str) -> float:
    """
    Convert VTT timestamp to seconds.

    :param timestamp: VTT timestamp
    :return: Seconds
    """
    timestamp = timestamp.replace(",", ".")
    hours, minutes, rest = timestamp.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def seconds_to_timestamp(seconds: float) -> str:
    """
    Convert seconds to VTT timestamp.

    :param seconds: Seconds
    :return: VTT timestamp
    """
    if seconds < 0:
        seconds = 0.0

    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))

    if millis == 1000:
        whole += 1
        millis = 0

    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60

    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def word_count(text: str) -> int:
    """
    Count word-like tokens.

    :param text: Input text
    :return: Word count
    """
    return len(re.findall(r"\b[\w']+\b", text))


def normalize_key(text: str, prefix_words: int) -> str:
    """
    Normalize cue text into a repeated-prefix key.

    :param text: Cue text
    :param prefix_words: Number of leading words to use
    :return: Normalized key, or empty string if too short
    """
    normalized = text.lower()
    normalized = re.sub(r"[^a-z0-9 ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    words = normalized.split()
    if len(words) < 8:
        return ""

    key = " ".join(words[:prefix_words])
    if len(key) < 50:
        return ""

    return key


def parse_vtt(path: Path) -> list[dict[str, Any]]:
    """
    Parse VTT cues from a file.

    :param path: VTT file path
    :return: Parsed cue dictionaries
    """
    cues: list[dict[str, Any]] = []
    current: tuple[str, str] | None = None
    text_parts: list[str] = []

    for raw_line in path.read_text(errors="replace").splitlines():
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


def classify_loop(count: int, span_seconds: float, sample: str) -> str:
    """
    Classify an adjacent repeated cue run.
    
    Ceremonial/procedural language is intentionally not exempted.
    Pledge, invocation, roll-call, consent-agenda, and public-hearing
    loops can still be ASR corruption when repeated across cues.

    :param count: Repeated cue count
    :param span_seconds: Repeated span duration
    :param sample: Sample cue text
    :return: Severity label
    """
    if count >= 8 or span_seconds >= 240:
        return "critical"

    if count >= 4 or span_seconds >= 90:
        return "high"

    if count >= 3 or span_seconds >= 45:
        return "medium"

    return "low"


def nearest_chunk_seam_distance(
    start_seconds: float,
    chunk_duration: float,
    overlap_seconds: float,
) -> float:
    """
    Estimate distance from cue start to nearest chunk seam.

    :param start_seconds: Cue start in seconds
    :param chunk_duration: Chunk duration in seconds
    :param overlap_seconds: Chunk overlap in seconds
    :return: Absolute distance to nearest seam in seconds
    """
    stride = chunk_duration - overlap_seconds

    if stride <= 0:
        return 0.0

    nearest_seam = round(start_seconds / stride) * stride
    return abs(start_seconds - nearest_seam)


def find_asr_loops(
    cues: list[dict[str, Any]],
    prefix_words: int,
) -> list[dict[str, Any]]:
    """
    Find adjacent repeated cue-prefix runs.

    Only returns medium/high/critical loops.
    Low/procedural repeats are ignored.

    :param cues: Parsed VTT cues
    :param prefix_words: Prefix word count for matching
    :return: ASR loop issue dictionaries
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
        nonlocal run_key, run_start, run_end, run_text, run_count
        nonlocal run_start_seconds, run_end_seconds

        if not run_key or run_count < 2:
            return

        span_seconds = run_end_seconds - run_start_seconds
        severity = classify_loop(run_count, span_seconds, run_text)

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


def find_impossible_cues(
    cues: list[dict[str, Any]],
    max_words_per_second: float,
    chunk_duration: float,
    overlap_seconds: float,
    seam_threshold: float,
) -> list[dict[str, Any]]:
    """
    Find cue timing-density issues that are candidates for timestamp repair.

    :param cues: Parsed VTT cues
    :param max_words_per_second: Plausible max speech rate
    :param chunk_duration: Chunk duration for seam-distance estimate
    :param overlap_seconds: Chunk overlap for seam-distance estimate
    :param seam_threshold: Distance threshold for seam-adjacent labels
    :return: Fixable cue issue dictionaries
    """
    issues: list[dict[str, Any]] = []

    for cue in cues:
        duration = cue["end_seconds"] - cue["start_seconds"]
        words = word_count(cue["text"])

        if duration <= 0:
            continue

        words_per_second = words / duration if duration else 0.0
        minimum_duration = words / max_words_per_second if max_words_per_second else 0.0

        legacy_trigger = (duration < 1.0 and words > 8) or (
            duration < 2.0 and words > 20
        )
        speech_rate_trigger = words >= 6 and duration < minimum_duration

        if not legacy_trigger and not speech_rate_trigger:
            continue

        seam_distance = nearest_chunk_seam_distance(
            cue["start_seconds"], chunk_duration, overlap_seconds
        )
        seam_adjacent = seam_distance <= seam_threshold

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
    Find long cues with very little text.

    These are report-only for now. They may be silence/VAD/timestamp artifacts,
    but are not safe to auto-repair without more context.

    :param cues: Parsed VTT cues
    :return: Report-only cue issue dictionaries
    """
    issues: list[dict[str, Any]] = []

    for cue in cues:
        duration = cue["end_seconds"] - cue["start_seconds"]
        words = word_count(cue["text"])

        if duration >= 20 and words <= 3:
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


def find_timestamp_order_issues(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Find non-monotonic or overlapping timestamps.

    :param cues: Parsed VTT cues
    :return: Fixable timestamp issue dictionaries
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


def count_30_second_cues(cues: list[dict[str, Any]]) -> int:
    """
    Count cues near 30 seconds long.

    :param cues: Parsed VTT cues
    :return: Count
    """
    count = 0

    for cue in cues:
        duration = cue["end_seconds"] - cue["start_seconds"]
        if 29.5 <= duration <= 30.5:
            count += 1

    return count


def classify_file(loops: list[dict[str, Any]]) -> str:
    """
    Classify file status from ASR loops only.

    pass:
        no ASR loops

    review:
        only small/medium loop findings

    rerun:
        high loop, max loop >= 90s, max repeated count >= 4,
        or 3+ loop findings

    block:
        critical loop, max loop >= 240s, or max repeated count >= 8

    :param loops: ASR loop issues
    :return: File status
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


def collect_vtt_paths(inputs: Sequence[str]) -> list[Path]:
    """
    Collect VTT paths from inputs, defaulting to ./data.

    :param inputs: CLI input paths
    :return: Sorted VTT paths
    """
    search_inputs = list(inputs) if inputs else ["data"]
    paths: list[Path] = []

    for item in search_inputs:
        path = Path(item)

        if not path.exists():
            continue

        if should_skip_path(path):
            continue

        if path.is_file():
            if path.suffix.lower() == ".vtt":
                paths.append(path)
            continue

        if path.is_dir():
            for candidate in path.rglob("*.vtt"):
                if should_skip_path(candidate):
                    continue

                paths.append(candidate)

    return sorted(set(paths))


def scan_file(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    """
    Scan one VTT file.

    :param path: VTT path
    :param args: Parsed CLI args
    :return: Result dictionary
    """
    cues = parse_vtt(path)
    loops = find_asr_loops(cues, args.prefix_words)
    impossible_cues = find_impossible_cues(
        cues=cues,
        max_words_per_second=args.max_words_per_second,
        chunk_duration=args.chunk_duration,
        overlap_seconds=args.overlap_seconds,
        seam_threshold=args.seam_threshold,
    )
    long_low_info = find_long_low_info(cues)
    timestamp_issues = find_timestamp_order_issues(cues)

    issues = loops + impossible_cues + long_low_info + timestamp_issues

    thirty_second_cues = count_30_second_cues(cues)
    duration_seconds = cues[-1]["end_seconds"] if cues else 0.0

    max_loop_span = max((loop["span_seconds"] for loop in loops), default=0.0)
    total_loop_span = sum(loop["span_seconds"] for loop in loops)
    max_loop_count = max((loop["count"] for loop in loops), default=0)

    status = classify_file(loops)

    fixable_count = sum(1 for issue in issues if issue["fixable"])
    impossible_count = len(impossible_cues)
    seam_impossible_count = sum(1 for issue in impossible_cues if issue["seam_adjacent"])
    long_low_info_count = len(long_low_info)
    timestamp_issue_count = len(timestamp_issues)
    warning_count = impossible_count + long_low_info_count + timestamp_issue_count

    return {
        "entity": entity_name_for_path(path),
        "path": str(path),
        "filename": path.name,
        "status": status,
        "action": "rerun" if status in {"rerun", "block"} else "review" if status == "review" else "pass",
        "duration_seconds": round(duration_seconds, 3),
        "duration_h": round(duration_seconds / 3600, 3),
        "cue_count": len(cues),
        "thirty_second_cues": thirty_second_cues,
        "loop_count": len(loops),
        "max_loop_span": round(max_loop_span, 3),
        "total_loop_span": round(total_loop_span, 3),
        "max_loop_count": max_loop_count,
        "fixable_count": fixable_count,
        "warning_count": warning_count,
        "impossible_cue_count": impossible_count,
        "seam_impossible_cue_count": seam_impossible_count,
        "long_low_info_count": long_low_info_count,
        "timestamp_issue_count": timestamp_issue_count,
        "loops": loops,
        "issues": issues,
    }


def should_show_result(
    result: dict[str, Any],
    show_all: bool,
    show_warnings: bool,
) -> bool:
    """
    Decide whether result should be printed in the human summary.

    :param result: Scan result
    :param show_all: Show all files
    :param show_warnings: Show warning-only files
    :return: True if visible
    """
    if show_all:
        return True

    if result["status"] != "pass":
        return True

    if show_warnings and result["warning_count"] > 0:
        return True

    return False


def print_summary(
    results: list[dict[str, Any]],
    show_all: bool,
    show_warnings: bool,
) -> None:
    """
    Print grouped human summary.

    :param results: Scan results
    :param show_all: Show all files
    :param show_warnings: Show warning-only files
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for result in results:
        if should_show_result(result, show_all, show_warnings):
            grouped[result["entity"]].append(result)

    if not grouped:
        print("No ASR loop findings.")
        return

    for entity in sorted(grouped):
        print()
        print(entity)
        print("-" * len(entity))
        print(
            f"{'status':8s} "
            f"{'loops':>5s} "
            f"{'max':>8s} "
            f"{'span':>8s} "
            f"{'fix':>4s} "
            f"{'imp':>4s} "
            f"{'seam':>4s} "
            f"{'low':>4s} "
            f"{'30s':>5s} "
            f"{'dur':>6s} "
            f"{'cues':>6s}  "
            f"file"
        )

        for result in sorted(
            grouped[entity],
            key=lambda item: (
                STATUS_RANK[item["status"]],
                item["loop_count"],
                item["max_loop_span"],
                item["fixable_count"],
                item["warning_count"],
                item["thirty_second_cues"],
            ),
            reverse=True,
        ):
            print(
                f"{result['status']:8s} "
                f"{result['loop_count']:5d} "
                f"{result['max_loop_span']:7.1f}s "
                f"{result['total_loop_span']:7.1f}s "
                f"{result['fixable_count']:4d} "
                f"{result['impossible_cue_count']:4d} "
                f"{result['seam_impossible_cue_count']:4d} "
                f"{result['long_low_info_count']:4d} "
                f"{result['thirty_second_cues']:5d} "
                f"{result['duration_h']:5.2f}h "
                f"{result['cue_count']:6d}  "
                f"{result['filename']}"
            )


def print_details(results: list[dict[str, Any]], include_warnings: bool) -> None:
    """
    Print detailed issue output.

    :param results: Scan results
    :param include_warnings: Include warning/fixable timestamp details
    """
    visible = []

    for result in results:
        if result["status"] in {"review", "rerun", "block"}:
            visible.append(result)
            continue

        if include_warnings and result["warning_count"] > 0:
            visible.append(result)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in visible:
        grouped[result["entity"]].append(result)

    for entity in sorted(grouped):
        print()
        print("=" * 100)
        print(entity)
        print("=" * 100)

        for result in sorted(
            grouped[entity],
            key=lambda item: (
                STATUS_RANK[item["status"]],
                item["max_loop_span"],
                item["loop_count"],
                item["fixable_count"],
            ),
            reverse=True,
        ):
            print()
            print(result["filename"])
            print("-" * len(result["filename"]))

            detail_issues = []
            for issue in result["issues"]:
                if issue["type"] == "asr_loop":
                    detail_issues.append(issue)
                    continue

                if include_warnings:
                    detail_issues.append(issue)

            for issue in sorted(
                detail_issues,
                key=lambda item: (
                    ISSUE_SEVERITY_RANK[item["severity"]],
                    item.get("span_seconds", item.get("duration_seconds", 0.0)),
                    item.get("count", 0),
                ),
                reverse=True,
            ):
                if issue["type"] == "asr_loop":
                    print(
                        f"{issue['severity']:8s} "
                        f"{issue['type']:20s} "
                        f"{issue['count']:3d}x "
                        f"{issue['span_seconds']:7.1f}s "
                        f"{issue['start']} -> {issue['end']} "
                        f"action={issue['action']} fixable={issue['fixable']}"
                    )
                elif issue["type"] == "impossible_cue":
                    seam = " seam" if issue["seam_adjacent"] else ""
                    print(
                        f"{issue['severity']:8s} "
                        f"{issue['type']:20s} "
                        f"{issue['words']:3d}w "
                        f"{issue['duration_seconds']:7.3f}s "
                        f"{issue['start']} -> {issue['end']} "
                        f"wps={issue['words_per_second']:.2f}"
                        f"{seam} action={issue['action']} fixable={issue['fixable']}"
                    )
                else:
                    print(
                        f"{issue['severity']:8s} "
                        f"{issue['type']:20s} "
                        f"{issue.get('duration_seconds', 0.0):7.3f}s "
                        f"{issue['start']} -> {issue['end']} "
                        f"action={issue['action']} fixable={issue['fixable']}"
                    )

                print(f"           {issue['sample']}")


def build_report(results: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    """
    Build machine-readable QA report.

    :param results: Scan results
    :param args: Parsed CLI args
    :return: JSON-serializable report
    """
    scanned_entities = sorted({result["entity"] for result in results})
    block_count = sum(1 for result in results if result["status"] == "block")
    rerun_count = sum(1 for result in results if result["status"] == "rerun")
    review_count = sum(1 for result in results if result["status"] == "review")
    pass_count = sum(1 for result in results if result["status"] == "pass")

    return {
        "summary": {
            "scanned_entities": len(scanned_entities),
            "entities": scanned_entities,
            "scanned_vtt_files": len(results),
            "status_counts": {
                "block": block_count,
                "rerun": rerun_count,
                "review": review_count,
                "pass": pass_count,
            },
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
                "prefix_words": args.prefix_words,
                "max_words_per_second": args.max_words_per_second,
                "chunk_duration": args.chunk_duration,
                "overlap_seconds": args.overlap_seconds,
                "seam_threshold": args.seam_threshold,
            },
        },
        "files": results,
    }


def write_json_report(report: dict[str, Any], output: str) -> None:
    """
    Write report JSON.

    :param report: Report dictionary
    :param output: Output path or '-'
    """
    text = json.dumps(report, indent=2, sort_keys=True)

    if output == "-":
        print(text)
        return

    Path(output).write_text(text + "\n")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """
    Parse CLI args.

    :param argv: CLI args
    :return: Parsed args
    """
    parser = argparse.ArgumentParser(
        description="Generate QA report for VTT transcript artifacts."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional VTT files or directories. Defaults to ./data.",
    )
    parser.add_argument(
        "--prefix-words",
        type=int,
        default=16,
        help="Number of leading words used to detect repeated cue prefixes.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Print ASR loop details. Combine with --warnings for cue issues.",
    )
    parser.add_argument(
        "--warnings",
        action="store_true",
        help="Also show warning-only files and warning details.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show all scanned files.",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="Write machine-readable JSON report to PATH. Use '-' for JSON-only stdout.",
    )
    parser.add_argument(
        "--max-words-per-second",
        type=float,
        default=4.0,
        help="Max plausible speech rate for impossible cue detection.",
    )
    parser.add_argument(
        "--chunk-duration",
        type=float,
        default=600.0,
        help="Audio chunk duration in seconds for seam-distance reporting.",
    )
    parser.add_argument(
        "--overlap-seconds",
        type=float,
        default=2.0,
        help="Audio chunk overlap in seconds for seam-distance reporting.",
    )
    parser.add_argument(
        "--seam-threshold",
        type=float,
        default=3.0,
        help="Seconds from estimated chunk seam considered seam-adjacent.",
    )

    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """
    Run transcript QA report.

    :param argv: CLI args
    :return: Exit code
    """
    args = parse_args(argv)
    paths = collect_vtt_paths(args.paths)

    if not paths:
        print("No VTT files found.", file=sys.stderr)
        return 2

    results = [scan_file(path, args) for path in paths]
    report = build_report(results, args)

    if args.json == "-":
        write_json_report(report, args.json)
        return 1 if report["summary"]["status_counts"]["block"] or report["summary"]["status_counts"]["rerun"] else 0

    summary = report["summary"]
    status_counts = summary["status_counts"]
    issue_counts = summary["issue_counts"]

    print(f"Scanned entities: {summary['scanned_entities']}")
    print(f"Scanned VTT files: {summary['scanned_vtt_files']}")
    print(
        f"Status counts: "
        f"block={status_counts['block']}, "
        f"rerun={status_counts['rerun']}, "
        f"review={status_counts['review']}, "
        f"pass={status_counts['pass']}"
    )
    print(
        f"Issue counts: "
        f"asr_loop={issue_counts['asr_loop']}, "
        f"fixable={issue_counts['fixable']}, "
        f"impossible_cue={issue_counts['impossible_cue']}, "
        f"seam_impossible_cue={issue_counts['seam_impossible_cue']}, "
        f"long_low_info_cue={issue_counts['long_low_info_cue']}, "
        f"timestamp_issue={issue_counts['timestamp_issue']}"
    )

    print_summary(results, args.all, args.warnings)

    if args.details:
        print_details(results, include_warnings=args.warnings)

    if args.json:
        write_json_report(report, args.json)
        print(f"\nWrote JSON report: {args.json}")

    return 1 if status_counts["block"] or status_counts["rerun"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))