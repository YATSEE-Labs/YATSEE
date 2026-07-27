"""
Reviewed transcript reset support for VTT rebuild workflows.

The reset is dry-run by default. Applying it deletes selected VTT files and
removes matching lines from likely transcript tracker files after creating
non-clobbering backups.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from yatsee.audit.resolve import resolve_transcript_audit_paths
from yatsee.core.errors import ValidationError
from yatsee.core.io import write_text_atomic

AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".m4a", ".mp4", ".webm"}
SKIP_DIR_MARKERS = {"old", "bak", "backup", "backups"}
TRACKER_SUFFIXES = {"", ".txt", ".log", ".csv", ".tsv", ".hash", ".hashes"}
TRACKER_NAME_HINTS = {"tracker", "hash", "processed"}
TRANSCRIPT_TRACKER_HINTS = {"vtt", "transcript", "transcribe", "audio"}
DEFAULT_QA_STATUSES = {"block", "rerun"}


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


def resolve_child_path(parent: Path, child: Path) -> Path:
    """
    Resolve a child path and verify it remains inside the parent.

    :param parent: Expected parent directory
    :param child: Candidate child path
    :return: Resolved child path
    :raises ValidationError: If the child escapes the parent
    """
    resolved_parent = parent.resolve()
    resolved_child = child.resolve()
    try:
        resolved_child.relative_to(resolved_parent)
    except ValueError as exc:
        raise ValidationError(
            f"Refusing path outside {resolved_parent}: {resolved_child}"
        ) from exc
    return resolved_child


def normalize_target(raw_target: str) -> str:
    """
    Normalize a target to a VTT stem or source ID.

    :param raw_target: User-supplied target
    :return: Normalized target
    """
    value = raw_target.strip()
    if not value:
        return ""
    path = Path(value)
    return path.stem if path.suffix.casefold() == ".vtt" else path.name


def source_id_from_stem(stem: str) -> str:
    """
    Extract the source ID from a YATSEE artifact stem.

    :param stem: Artifact stem
    :return: Source ID
    """
    return stem.split(".", 1)[0]


def load_targets_from_report(
    report_path: Path,
    *,
    entity: str,
    statuses: set[str],
) -> list[str]:
    """
    Load selected VTT targets from a transcript audit JSON report.

    :param report_path: Audit report path
    :param entity: Expected entity handle
    :param statuses: File statuses eligible for reset
    :return: Sorted target stems
    :raises ValidationError: If the report is missing or invalid
    """
    if not report_path.is_file():
        raise ValidationError(f"Transcript audit report not found: {report_path}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"Invalid transcript audit JSON '{report_path}': {exc}"
        ) from exc

    if report.get("entity") not in {None, entity}:
        raise ValidationError(
            f"Transcript audit report entity does not match '{entity}'"
        )
    files = report.get("files")
    if not isinstance(files, list):
        raise ValidationError("Transcript audit report does not contain a files list")

    targets: list[str] = []
    for item in files:
        if not isinstance(item, dict) or item.get("status") not in statuses:
            continue
        path_value = item.get("path") or item.get("filename")
        if not isinstance(path_value, str):
            continue
        target = normalize_target(Path(path_value).name)
        if target:
            targets.append(target)
    return sorted(set(targets))


def target_matches_vtt(path: Path, targets: Sequence[str]) -> bool:
    """
    Return whether a VTT path matches a selected source ID or stem.

    :param path: VTT path
    :param targets: Selected targets
    :return: True when matched
    """
    stem = path.stem
    source_id = source_id_from_stem(stem)
    return any(
        target == source_id or target == stem or stem.startswith(f"{target}.")
        for target in targets
    )


def collect_matching_vtts(
    transcript_root: Path,
    targets: Sequence[str],
) -> list[Path]:
    """
    Find selected active VTT files beneath the transcript root.

    :param transcript_root: VTT file or directory
    :param targets: Selected targets
    :return: Sorted matching VTT paths
    :raises ValidationError: If the transcript root does not exist
    """
    if not transcript_root.exists():
        raise ValidationError(f"Transcript input does not exist: {transcript_root}")
    candidates = (
        [transcript_root]
        if transcript_root.is_file()
        else list(transcript_root.rglob("*.vtt"))
    )
    matches = [
        path.resolve()
        for path in candidates
        if path.suffix.casefold() == ".vtt"
        and not should_skip_path(path)
        and target_matches_vtt(path, targets)
    ]
    return sorted(set(matches))


def find_matching_audio_files(entity_root: Path, vtt_path: Path) -> list[Path]:
    """
    Find source audio files sharing a VTT source ID.

    :param entity_root: Trusted entity data directory
    :param vtt_path: Selected VTT path
    :return: Matching audio paths
    """
    audio_dir = entity_root / "audio"
    if not audio_dir.is_dir():
        return []
    source_id = source_id_from_stem(vtt_path.stem)
    return sorted(
        path.resolve()
        for path in audio_dir.glob(f"{source_id}.*")
        if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS
    )


def sha256_file(path: Path) -> str:
    """
    Compute a file SHA-256 digest.

    :param path: File path
    :return: Hex digest
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_tracker_match_terms(
    entity_root: Path,
    vtt_paths: Sequence[Path],
    targets: Sequence[str],
) -> set[str]:
    """
    Build source names and hashes that may appear in tracker lines.

    :param entity_root: Trusted entity data directory
    :param vtt_paths: Selected VTT files
    :param targets: Original targets
    :return: Tracker line match terms
    """
    terms: set[str] = {target for target in targets if len(target) >= 6}
    for vtt_path in vtt_paths:
        terms.update(
            {
                vtt_path.name,
                vtt_path.stem,
                source_id_from_stem(vtt_path.stem),
                sha256_file(vtt_path),
            }
        )
        for audio_path in find_matching_audio_files(entity_root, vtt_path):
            terms.update(
                {audio_path.name, audio_path.stem, sha256_file(audio_path)}
            )
    return {term for term in terms if len(term) >= 6}


def looks_like_tracker(path: Path, include_all_trackers: bool) -> bool:
    """
    Return whether a file looks like a line-based transcript tracker.

    :param path: Candidate path
    :param include_all_trackers: Relax transcript-specific name matching
    :return: True when the file should be inspected
    """
    if not path.is_file() or should_skip_path(path):
        return False
    if path.suffix.casefold() not in TRACKER_SUFFIXES:
        return False
    lowered_name = path.name.casefold()
    if not any(hint in lowered_name for hint in TRACKER_NAME_HINTS):
        return False
    if include_all_trackers:
        return True
    lowered_stem = path.stem.casefold()
    return any(hint in lowered_stem for hint in TRANSCRIPT_TRACKER_HINTS)


def collect_tracker_files(
    entity_root: Path,
    explicit_trackers: Sequence[str],
    include_all_trackers: bool,
) -> list[Path]:
    """
    Collect explicit or discovered tracker files inside the entity directory.

    :param entity_root: Trusted entity data directory
    :param explicit_trackers: Explicit tracker paths
    :param include_all_trackers: Relax transcript-specific discovery
    :return: Sorted tracker paths
    :raises ValidationError: If an explicit path is invalid or escapes the entity
    """
    if explicit_trackers:
        trackers: list[Path] = []
        for item in explicit_trackers:
            path = resolve_child_path(entity_root, Path(item).expanduser())
            if not path.is_file():
                raise ValidationError(f"Tracker file does not exist: {path}")
            trackers.append(path)
        return sorted(set(trackers))

    return sorted(
        path.resolve()
        for path in entity_root.rglob("*")
        if looks_like_tracker(path, include_all_trackers)
    )


def line_matches_terms(line: str, terms: set[str]) -> bool:
    """
    Return whether a tracker line contains a selected source term.

    :param line: Tracker line
    :param terms: Match terms
    :return: True when the line should be removed
    """
    lowered_line = line.casefold()
    return any(term.casefold() in lowered_line for term in terms)


def next_backup_path(path: Path) -> Path:
    """
    Return a non-clobbering backup path.

    :param path: Original file path
    :return: Available backup path
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
    Remove selected lines from a tracker after writing a backup.

    :param path: Tracker file
    :param terms: Match terms
    :param apply: Whether to modify the file
    :return: Number of matching lines
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(
        keepends=True
    )
    kept = [line for line in lines if not line_matches_terms(line, terms)]
    removed = len(lines) - len(kept)
    if apply and removed:
        next_backup_path(path).write_text("".join(lines), encoding="utf-8")
        write_text_atomic(str(path), "".join(kept))
    return removed


def run_transcript_reset(
    *,
    global_config_path: str,
    entity: str,
    model: str | None = None,
    targets: Sequence[str] = (),
    target_file: str | None = None,
    report_path: str | None = None,
    statuses: Sequence[str] = (),
    tracker_paths: Sequence[str] = (),
    include_all_trackers: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """
    Reset selected VTT files and tracker entries for retranscription.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional transcription model suffix override
    :param targets: Explicit source IDs, VTT names, or stems
    :param target_file: Optional newline-delimited target file
    :param report_path: Optional transcript audit JSON report
    :param statuses: Report statuses to select; defaults to block and rerun
    :param tracker_paths: Optional explicit tracker files
    :param include_all_trackers: Relax tracker discovery
    :param apply: Whether to perform destructive changes
    :return: Reset plan and execution summary
    :raises ValidationError: If targets, reports, or paths are invalid
    """
    resolved = resolve_transcript_audit_paths(
        global_config_path,
        entity=entity,
        model=model,
    )
    entity_root = Path(resolved["data_path"]).resolve()
    transcript_root = Path(resolved["input_path"]).resolve()

    selected = [normalize_target(item) for item in targets]
    selected = [item for item in selected if item]
    if target_file:
        path = Path(target_file).expanduser().resolve()
        if not path.is_file():
            raise ValidationError(f"Target file does not exist: {path}")
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                selected.append(normalize_target(line))

    selected_statuses = set(statuses or DEFAULT_QA_STATUSES)
    invalid_statuses = selected_statuses - {"block", "rerun", "review", "pass"}
    if invalid_statuses:
        raise ValidationError(
            "Unsupported transcript audit status: "
            + ", ".join(sorted(invalid_statuses))
        )

    canonical_report = Path(resolved["output_dir"]) / "report.json"
    report = Path(report_path).expanduser().resolve() if report_path else None
    if report is None and not selected and canonical_report.is_file():
        report = canonical_report
    if report is not None:
        selected.extend(
            load_targets_from_report(
                report,
                entity=entity,
                statuses=selected_statuses,
            )
        )

    selected = sorted(set(item for item in selected if item))
    if not selected:
        raise ValidationError(
            "No transcript reset targets were provided or selected from a report"
        )

    vtt_paths = collect_matching_vtts(transcript_root, selected)
    terms = build_tracker_match_terms(entity_root, vtt_paths, selected)
    trackers = collect_tracker_files(
        entity_root,
        tracker_paths,
        include_all_trackers,
    )

    tracker_updates: list[dict[str, Any]] = []
    total_removed = 0
    for tracker in trackers:
        removed = update_tracker_file(tracker, terms, apply)
        if removed:
            tracker_updates.append({"path": str(tracker), "removed_lines": removed})
            total_removed += removed

    deleted = 0
    for vtt_path in vtt_paths:
        if not vtt_path.exists():
            continue
        deleted += 1
        if apply:
            vtt_path.unlink()

    rebuild_command = [
        "yatsee",
        "audio",
        "transcribe",
        "-e",
        entity,
        "--model",
        resolved["model"],
        "--faster",
        "--get-chunks",
    ]
    return {
        "entity": entity,
        "model": resolved["model"],
        "apply": apply,
        "targets": selected,
        "report_path": str(report) if report else None,
        "vtt_paths": [str(path) for path in vtt_paths],
        "tracker_updates": tracker_updates,
        "vtt_count": deleted,
        "tracker_line_count": total_removed,
        "rebuild_command": rebuild_command,
    }
