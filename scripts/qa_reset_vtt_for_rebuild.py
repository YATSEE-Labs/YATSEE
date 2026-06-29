#!/usr/bin/env python3
"""
Reset selected VTT transcript artifacts so they can be rebuilt.

This is the manual QA rerun-prep script for YATSEE transcripts.

Default behavior is dry-run. Destructive changes require --apply.

Common workflow:
    python scripts/qa_transcript_report.py --json qa_report.json

    python scripts/qa_reset_vtt_for_rebuild.py \
      --entity us_il_freeport_city_council \
      --qa-report qa_report.json

    python scripts/qa_reset_vtt_for_rebuild.py \
      --entity us_il_freeport_city_council \
      --qa-report qa_report.json \
      --apply

    yatsee audio transcribe \
      -e us_il_freeport_city_council \
      --faster \
      --get-chunks \
      --transcription-profile qa_cleanup

The script:
    - accepts explicit source IDs / stems / VTT filenames
    - accepts --target-file for newline-delimited targets
    - accepts --qa-report from qa_transcript_report.py --json
    - defaults QA report selection to status=block/rerun
    - finds active VTTs under data/<entity>/transcripts_<model>/
    - skips old/bak/backup directories
    - deletes matching VTTs only with --apply
    - removes matching lines from likely VTT hash tracker files
    - writes tracker .bak files before editing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Sequence


AUDIO_EXTENSIONS = {
    ".flac",
    ".wav",
    ".mp3",
    ".m4a",
    ".mp4",
    ".webm",
}

SKIP_DIR_MARKERS = {
    "old",
    "bak",
    "backup",
    "backups",
}

TRACKER_SUFFIXES = {
    "",
    ".txt",
    ".log",
    ".csv",
    ".tsv",
    ".hash",
    ".hashes",
}

TRACKER_NAME_HINTS = {
    "tracker",
    "hash",
    "processed",
}

TRANSCRIPT_TRACKER_HINTS = {
    "vtt",
    "transcript",
    "transcribe",
    "audio",
}

DEFAULT_QA_STATUSES = {"block", "rerun"}
DEFAULT_REBUILD_TRANSCRIPTION_PROFILE = "qa_cleanup"


class ResetError(Exception):
    """Raised for reset-script validation errors."""


def should_skip_path(path: Path) -> bool:
    """
    Return True for old/bak/archive paths.

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


def resolve_child_path(parent: Path, child: Path) -> Path:
    """
    Resolve a child path and verify it remains inside the parent path.

    :param parent: Expected parent directory
    :param child: Candidate child path
    :return: Resolved child path
    :raises ResetError: If child escapes parent
    """
    resolved_parent = parent.resolve()
    resolved_child = child.resolve()

    try:
        resolved_child.relative_to(resolved_parent)
    except ValueError as exc:
        raise ResetError(
            f"Refusing path outside {resolved_parent}: {resolved_child}"
        ) from exc

    return resolved_child


def display_path(path: Path) -> str:
    """
    Return a readable path, relative to cwd when possible.

    :param path: Path to display
    :return: Display path string
    """
    cwd = Path.cwd().resolve()
    resolved = path.resolve()

    try:
        return str(resolved.relative_to(cwd))
    except ValueError:
        return str(resolved)


def normalize_target(raw_target: str) -> str:
    """
    Normalize a user-supplied target to a filename stem or source ID.

    :param raw_target: Raw target argument
    :return: Normalized target
    """
    value = raw_target.strip()

    if not value:
        return ""

    path = Path(value)

    if path.suffix == ".vtt":
        return path.stem

    return path.name


def source_id_from_stem(stem: str) -> str:
    """
    Extract source ID from a YATSEE artifact stem.

    :param stem: File stem
    :return: Source ID
    """
    return stem.split(".", 1)[0]


def entity_name_for_path(path: Path) -> str:
    """
    Return entity name from a data/<entity>/... path.

    :param path: Artifact path
    :return: Entity name, or empty string if unknown
    """
    parts = path.parts

    if "data" in parts:
        data_index = parts.index("data")
        if len(parts) > data_index + 1:
            return parts[data_index + 1]

    return ""


def load_targets_from_args(args: argparse.Namespace) -> list[str]:
    """
    Load target source IDs/stems from positional args and optional target file.

    :param args: Parsed CLI args
    :return: Sorted unique target strings
    """
    targets: list[str] = []

    for item in args.targets:
        target = normalize_target(item)
        if target:
            targets.append(target)

    if args.target_file:
        target_file = Path(args.target_file)

        if not target_file.exists():
            raise ResetError(f"Target file does not exist: {target_file}")

        for raw_line in target_file.read_text().splitlines():
            line = raw_line.strip()

            if not line or line.startswith("#"):
                continue

            target = normalize_target(line)
            if target:
                targets.append(target)

    return sorted(set(targets))


def load_targets_from_qa_report(args: argparse.Namespace) -> list[str]:
    """
    Load targets from qa_transcript_report.py JSON output.

    Defaults to status=block/rerun unless --qa-status is provided.

    :param args: Parsed CLI args
    :return: Sorted target source IDs/stems
    """
    if not args.qa_report:
        return []

    report_path = Path(args.qa_report)

    if not report_path.exists():
        raise ResetError(f"QA report does not exist: {report_path}")

    try:
        report = json.loads(report_path.read_text())
    except json.JSONDecodeError as exc:
        raise ResetError(f"Invalid QA report JSON: {report_path}: {exc}") from exc

    files = report.get("files")
    if not isinstance(files, list):
        raise ResetError("QA report does not contain a files list")

    selected_statuses = set(args.qa_status or DEFAULT_QA_STATUSES)
    targets: list[str] = []

    for item in files:
        if not isinstance(item, dict):
            continue

        status = item.get("status")
        if status not in selected_statuses:
            continue

        if args.entity and item.get("entity") != args.entity:
            continue

        path_value = item.get("path") or item.get("filename")
        if not isinstance(path_value, str) or not path_value.strip():
            continue

        path = Path(path_value)
        target = normalize_target(path.name)
        if target:
            targets.append(target)

    return sorted(set(targets))


def target_matches_vtt(path: Path, targets: Sequence[str]) -> bool:
    """
    Check whether a VTT path matches one of the requested targets.

    :param path: VTT path
    :param targets: Target source IDs or stems
    :return: True if matched
    """
    stem = path.stem
    source_id = source_id_from_stem(stem)

    for target in targets:
        if target == source_id:
            return True

        if target == stem:
            return True

        if stem.startswith(f"{target}."):
            return True

    return False


def is_active_transcript_vtt(path: Path, model: str | None) -> bool:
    """
    Check whether a VTT is in an active transcripts directory.

    :param path: Candidate path
    :param model: Optional transcript model suffix, such as medium
    :return: True if path should be considered
    """
    if path.suffix.lower() != ".vtt":
        return False

    if should_skip_path(path):
        return False

    parent_name = path.parent.name

    if model:
        return parent_name == f"transcripts_{model}"

    return parent_name.startswith("transcripts")


def collect_matching_vtts(
    data_dir: Path,
    entity: str | None,
    model: str | None,
    targets: Sequence[str],
) -> list[Path]:
    """
    Find matching VTT files under data.

    :param data_dir: Data directory
    :param entity: Optional entity handle
    :param model: Optional transcript model suffix
    :param targets: Target source IDs or stems
    :return: Sorted matching VTT paths
    """
    search_root = data_dir / entity if entity else data_dir
    search_root = resolve_child_path(data_dir, search_root)

    if not search_root.exists():
        raise ResetError(f"Search root does not exist: {search_root}")

    matches: list[Path] = []

    for path in search_root.rglob("*.vtt"):
        if not is_active_transcript_vtt(path, model):
            continue

        if target_matches_vtt(path, targets):
            matches.append(resolve_child_path(data_dir, path))

    return sorted(set(matches))


def entity_root_for_path(data_dir: Path, path: Path) -> Path:
    """
    Resolve the entity root for a data/<entity>/... artifact path.

    :param data_dir: Data directory
    :param path: Artifact path
    :return: Entity root path
    :raises ResetError: If no entity root can be resolved
    """
    relative = path.resolve().relative_to(data_dir.resolve())

    if len(relative.parts) < 2:
        raise ResetError(f"Could not resolve entity root from path: {path}")

    return data_dir / relative.parts[0]


def find_matching_audio_files(data_dir: Path, vtt_path: Path) -> list[Path]:
    """
    Find source audio files matching a VTT source ID.

    :param data_dir: Data directory
    :param vtt_path: VTT path
    :return: Matching audio paths
    """
    entity_root = entity_root_for_path(data_dir, vtt_path)
    audio_dir = entity_root / "audio"
    source_id = source_id_from_stem(vtt_path.stem)

    if not audio_dir.exists():
        return []

    matches: list[Path] = []

    for path in audio_dir.glob(f"{source_id}.*"):
        if path.suffix.lower() in AUDIO_EXTENSIONS and path.is_file():
            matches.append(resolve_child_path(data_dir, path))

    return sorted(matches)


def sha256_file(path: Path) -> str:
    """
    Compute SHA-256 for a file.

    :param path: File path
    :return: Hex digest
    """
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def build_tracker_match_terms(
    data_dir: Path,
    vtt_paths: Sequence[Path],
    targets: Sequence[str],
) -> set[str]:
    """
    Build strings that may appear in line-based tracker files.

    Includes:
    - source IDs
    - VTT stems and names
    - matching audio stems and names
    - VTT SHA-256 hashes
    - matching audio SHA-256 hashes

    :param data_dir: Data directory
    :param vtt_paths: Matched VTT paths
    :param targets: Original targets
    :return: Match terms
    """
    terms: set[str] = {target for target in targets if len(target) >= 6}

    for vtt_path in vtt_paths:
        terms.add(vtt_path.name)
        terms.add(vtt_path.stem)
        terms.add(source_id_from_stem(vtt_path.stem))

        if vtt_path.exists():
            terms.add(sha256_file(vtt_path))

        for audio_path in find_matching_audio_files(data_dir, vtt_path):
            terms.add(audio_path.name)
            terms.add(audio_path.stem)
            terms.add(sha256_file(audio_path))

    return {term for term in terms if len(term) >= 6}


def looks_like_tracker(path: Path, include_all_trackers: bool) -> bool:
    """
    Return True if a file looks like a line-based tracker file.

    :param path: Candidate path
    :param include_all_trackers: Relax name hint matching
    :return: True if likely tracker
    """
    if path.is_dir():
        return False

    if should_skip_path(path):
        return False

    lowered_name = path.name.casefold()
    lowered_stem = path.stem.casefold()

    if path.suffix.casefold() not in TRACKER_SUFFIXES:
        return False

    has_tracker_hint = any(hint in lowered_name for hint in TRACKER_NAME_HINTS)

    if not has_tracker_hint:
        return False

    if include_all_trackers:
        return True

    return any(hint in lowered_stem for hint in TRANSCRIPT_TRACKER_HINTS)


def collect_tracker_files(
    data_dir: Path,
    entity: str | None,
    explicit_trackers: Sequence[str],
    include_all_trackers: bool,
) -> list[Path]:
    """
    Collect tracker files to inspect/edit.

    :param data_dir: Data directory
    :param entity: Optional entity handle
    :param explicit_trackers: Explicit tracker paths
    :param include_all_trackers: Relax tracker discovery
    :return: Sorted tracker paths
    """
    trackers: list[Path] = []

    if explicit_trackers:
        for item in explicit_trackers:
            path = Path(item)
            if not path.exists():
                raise ResetError(f"Tracker file does not exist: {path}")
            trackers.append(path.resolve())

        return sorted(set(trackers))

    search_root = data_dir / entity if entity else data_dir
    search_root = resolve_child_path(data_dir, search_root)

    for path in search_root.rglob("*"):
        if looks_like_tracker(path, include_all_trackers):
            trackers.append(resolve_child_path(data_dir, path))

    return sorted(set(trackers))


def line_matches_terms(line: str, terms: set[str]) -> bool:
    """
    Check whether a tracker line matches one of the removal terms.

    :param line: Tracker line
    :param terms: Match terms
    :return: True if line should be removed
    """
    lowered_line = line.casefold()

    for term in terms:
        lowered_term = term.casefold()

        if lowered_term in lowered_line:
            return True

    return False


def next_backup_path(path: Path) -> Path:
    """
    Return a non-clobbering backup path.

    :param path: Original file path
    :return: Backup path
    """
    candidate = path.with_name(f"{path.name}.bak")
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = path.with_name(f"{path.name}.bak{index}")
        if not candidate.exists():
            return candidate
        index += 1


def update_tracker_file(path: Path, terms: set[str], apply: bool) -> int:
    """
    Remove matching lines from a tracker file.

    Writes a non-clobbering .bak backup before modifying when apply=True.

    :param path: Tracker file path
    :param terms: Match terms
    :param apply: Whether to write changes
    :return: Number of removed lines
    """
    lines = path.read_text(errors="replace").splitlines(keepends=True)
    kept: list[str] = []
    removed = 0

    for line in lines:
        if line_matches_terms(line, terms):
            removed += 1
            continue

        kept.append(line)

    if apply and removed:
        backup_path = next_backup_path(path)
        backup_path.write_text("".join(lines))
        path.write_text("".join(kept))

    return removed



def build_rebuild_command(args: argparse.Namespace) -> list[str]:
    """
    Build the recommended transcription command after a QA reset.

    The reset helper does not run transcription. It prints the follow-up command
    so operators do not accidentally rebuild ASR-loop failures with the same
    transcription behavior that produced them.

    :param args: Parsed CLI args
    :return: Command argument list
    """
    command = ["yatsee", "audio", "transcribe"]

    if args.entity:
        command.extend(["-e", args.entity])
    else:
        command.append("<entity>")

    command.extend(
        [
            "--faster",
            "--get-chunks",
            "--transcription-profile",
            args.rebuild_profile,
        ]
    )

    return command


def print_rebuild_command(args: argparse.Namespace, apply: bool) -> None:
    """
    Print the recommended follow-up transcription command.

    :param args: Parsed CLI args
    :param apply: Whether the reset was applied
    """
    if apply:
        print("Recommended rebuild command:")
    else:
        print("Recommended rebuild command after applying the reset:")

    print(f"  {shlex.join(build_rebuild_command(args))}")

    if args.rebuild_profile == DEFAULT_REBUILD_TRANSCRIPTION_PROFILE:
        print(
            "  qa_cleanup is recommended for QA-selected ASR loop rebuilds "
            "because it disables previous-text conditioning."
        )

def delete_vtts(vtt_paths: Sequence[Path], apply: bool) -> int:
    """
    Delete selected VTT files.

    :param vtt_paths: VTT paths
    :param apply: Whether to delete
    :return: Number of files deleted or that would be deleted
    """
    count = 0

    for path in vtt_paths:
        if not path.exists():
            continue

        count += 1

        if apply:
            path.unlink()

    return count


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """
    Parse CLI args.

    :param argv: CLI args
    :return: Parsed args
    """
    parser = argparse.ArgumentParser(
        description="Reset selected VTTs and tracker entries so transcripts rebuild."
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Source IDs, VTT filenames, or VTT stems to reset.",
    )
    parser.add_argument(
        "--target-file",
        help="Optional newline-delimited file of source IDs, VTT filenames, or stems.",
    )
    parser.add_argument(
        "--qa-report",
        help="Optional qa_transcript_report.py JSON file. Defaults to status=block/rerun.",
    )
    parser.add_argument(
        "--qa-status",
        action="append",
        choices=["block", "rerun", "review", "pass"],
        help=(
            "Status from --qa-report to reset. Can be passed multiple times. "
            "Defaults to block and rerun when --qa-report is used."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Data directory. Defaults to ./data.",
    )
    parser.add_argument(
        "-e",
        "--entity",
        help="Optional entity handle, such as us_il_freeport_city_council.",
    )
    parser.add_argument(
        "--model",
        default="medium",
        help="Transcript model suffix. Defaults to medium for transcripts_medium.",
    )
    parser.add_argument(
        "--tracker",
        action="append",
        default=[],
        help="Explicit tracker file path. Can be passed multiple times.",
    )
    parser.add_argument(
        "--include-all-trackers",
        action="store_true",
        help=(
            "Relax tracker auto-discovery to any line-based file with "
            "tracker/hash/processed in the name."
        ),
    )
    parser.add_argument(
        "--rebuild-profile",
        default=DEFAULT_REBUILD_TRANSCRIPTION_PROFILE,
        choices=["default", "qa_cleanup"],
        help=(
            "Transcription profile to show in the recommended rebuild command. "
            "Defaults to qa_cleanup for QA-selected ASR loop rebuilds."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete VTTs and edit tracker files. Default is dry-run.",
    )

    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    """
    Run reset script.

    :param argv: CLI args
    :return: Exit code
    """
    args = parse_args(argv)
    data_dir = Path(args.data_dir).resolve()

    if not data_dir.exists():
        print(f"Data directory does not exist: {data_dir}", file=sys.stderr)
        return 2

    try:
        explicit_targets = load_targets_from_args(args)
        qa_targets = load_targets_from_qa_report(args)
        targets = sorted(set(explicit_targets + qa_targets))

        if not targets:
            print("No targets provided or selected from QA report.", file=sys.stderr)
            return 2

        vtt_paths = collect_matching_vtts(
            data_dir=data_dir,
            entity=args.entity,
            model=args.model,
            targets=targets,
        )
        terms = build_tracker_match_terms(data_dir, vtt_paths, targets)
        tracker_files = collect_tracker_files(
            data_dir=data_dir,
            entity=args.entity,
            explicit_trackers=args.tracker,
            include_all_trackers=args.include_all_trackers,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Mode: {mode}")
    print(f"Targets from args/target-file: {len(explicit_targets)}")
    print(f"Targets from QA report: {len(qa_targets)}")
    print(f"Total selected targets: {len(targets)}")
    if args.qa_report:
        print(
            "QA statuses: "
            f"{', '.join(sorted(set(args.qa_status or DEFAULT_QA_STATUSES)))}"
        )
    print(f"Matching VTT files: {len(vtt_paths)}")
    print(f"Tracker files inspected: {len(tracker_files)}")
    print()

    if vtt_paths:
        print("VTT files:")
        for path in vtt_paths:
            print(f"  {display_path(path)}")
    else:
        print("VTT files: none matched")

    print()

    total_removed_tracker_lines = 0

    if tracker_files:
        print("Tracker updates:")
        for tracker_file in tracker_files:
            removed = update_tracker_file(tracker_file, terms, args.apply)
            total_removed_tracker_lines += removed

            if removed:
                print(f"  {display_path(tracker_file)}: remove {removed} line(s)")
    else:
        print("Tracker updates: no likely tracker files discovered")

    deleted_count = delete_vtts(vtt_paths, args.apply)

    print()
    print(f"VTT files {'deleted' if args.apply else 'to delete'}: {deleted_count}")
    print(
        f"Tracker lines {'removed' if args.apply else 'to remove'}: "
        f"{total_removed_tracker_lines}"
    )

    print()
    print_rebuild_command(args, args.apply)

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to make changes.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))