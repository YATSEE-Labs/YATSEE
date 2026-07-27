"""
Reviewed restoration of VTT cues suppressed during transcription.

Suppression logs can contain valid civic roll-call or vote cues alongside true
hotword hallucinations. This helper creates a review CSV and restores only rows
explicitly marked ``action=restore``. Writes remain dry-run unless requested.
"""

from __future__ import annotations

import csv
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from yatsee.audit.resolve import resolve_transcript_audit_paths
from yatsee.core.errors import ValidationError
from yatsee.core.io import write_text_atomic
from yatsee.core.paths import resolve_contained_path, sanitize_path_component

SUPPRESSION_RE = re.compile(
    r"Suppressed likely hotword hallucination from VTT output: "
    r"(?P<base>.+?) "
    r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3}) --> "
    r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3}) \| "
    r"(?P<text>.*)$"
)
REVIEW_FIELDS = (
    "action",
    "base",
    "start",
    "end",
    "text",
    "assessment",
    "reason",
)


@dataclass(frozen=True)
class SuppressedCue:
    """
    Suppressed VTT cue parsed from a transcription log.

    :param base: Source artifact base name without .vtt
    :param start: VTT start timestamp
    :param end: VTT end timestamp
    :param text: Suppressed cue text
    """

    base: str
    start: str
    end: str
    text: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        """
        Return a stable review CSV matching key.

        :return: Base, start, end, and text tuple
        """
        return (self.base, self.start, self.end, self.text)


def parse_suppression_log(log_path: Path) -> list[SuppressedCue]:
    """
    Parse suppressed cue entries from a transcription log.

    :param log_path: Transcription log path
    :return: Parsed suppressed cues
    :raises ValidationError: If the log cannot be read
    """
    if not log_path.is_file():
        raise ValidationError(f"Transcription log does not exist: {log_path}")
    cues: list[SuppressedCue] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = SUPPRESSION_RE.search(line.strip())
        if not match:
            continue
        cues.append(
            SuppressedCue(
                base=match.group("base"),
                start=match.group("start"),
                end=match.group("end"),
                text=match.group("text").strip(),
            )
        )
    return cues


def default_assessment(cue: SuppressedCue) -> tuple[str, str, str]:
    """
    Produce a conservative review starting point for a suppressed cue.

    :param cue: Suppressed cue
    :return: Action, assessment, and reason
    """
    text = cue.text.casefold()
    alias_explosion_terms = {"huffines", "heimerdinger", "dovie", "gertrude", "donald"}
    civic_terms = {"absent", "alderperson", "alderpersons", "mayor", "sanders is absent"}
    if sum(1 for term in alias_explosion_terms if term in text) >= 2:
        return (
            "skip",
            "likely_hotword_explosion",
            "contains multiple alias-family terms",
        )
    if any(term in text for term in civic_terms):
        return (
            "restore",
            "likely_civic_roll_call",
            "contains civic attendance or office terms",
        )
    return (
        "review",
        "ambiguous_roster",
        "roster-like cue needs local context before restore",
    )


def write_review_csv(cues: Iterable[SuppressedCue], output_path: Path) -> int:
    """
    Write a review CSV for parsed suppressed cues.

    :param cues: Suppressed cues
    :param output_path: CSV destination
    :return: Number of rows written
    """
    rows = list(cues)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for cue in rows:
            action, assessment, reason = default_assessment(cue)
            writer.writerow(
                {
                    "action": action,
                    "base": cue.base,
                    "start": cue.start,
                    "end": cue.end,
                    "text": cue.text,
                    "assessment": assessment,
                    "reason": reason,
                }
            )
    return len(rows)


def load_restore_allowlist(
    review_path: Path,
) -> set[tuple[str, str, str, str]]:
    """
    Load reviewed cues explicitly marked action=restore.

    :param review_path: Review CSV path
    :return: Approved cue keys
    :raises ValidationError: If the review CSV does not exist
    """
    if not review_path.is_file():
        raise ValidationError(f"Review CSV does not exist: {review_path}")
    approved: set[tuple[str, str, str, str]] = set()
    with review_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("action", "").strip().casefold() != "restore":
                continue
            approved.add(
                (
                    row.get("base", "").strip(),
                    row.get("start", "").strip(),
                    row.get("end", "").strip(),
                    row.get("text", "").strip(),
                )
            )
    return approved


def read_vtt_cues(vtt_path: Path) -> tuple[str, list[str]]:
    """
    Read a VTT into a header and cue blocks.

    :param vtt_path: VTT path
    :return: Header and cue blocks
    """
    raw = vtt_path.read_text(encoding="utf-8")
    parts = raw.split("\n\n")
    header = parts[0].rstrip() if parts else "WEBVTT"
    blocks = [part.strip() for part in parts[1:] if part.strip()]
    return header, blocks


def cue_start(block: str) -> str:
    """
    Return a cue block start timestamp for sorting.

    :param block: VTT cue block
    :return: Start timestamp or a high sentinel
    """
    lines = block.splitlines()
    first_line = lines[0] if lines else ""
    if "-->" not in first_line:
        return "99:99:99.999"
    return first_line.split("-->", 1)[0].strip()


def cue_block(cue: SuppressedCue) -> str:
    """
    Format a suppressed cue as a VTT block.

    :param cue: Suppressed cue
    :return: VTT cue block
    """
    return f"{cue.start} --> {cue.end}\n{cue.text}"


def restore_cues_for_file(
    vtt_path: Path,
    cues: list[SuppressedCue],
    *,
    apply: bool,
) -> tuple[int, int]:
    """
    Restore approved cues not already present in one VTT.

    :param vtt_path: VTT path
    :param cues: Approved cues
    :param apply: Whether to write changes
    :return: Inserted and duplicate counts
    """
    header, blocks = read_vtt_cues(vtt_path)
    existing = set(blocks)
    inserted = 0
    duplicates = 0
    for cue in cues:
        block = cue_block(cue)
        if block in existing:
            duplicates += 1
            continue
        blocks.append(block)
        existing.add(block)
        inserted += 1

    if inserted and apply:
        backup_path = vtt_path.with_suffix(vtt_path.suffix + ".bak")
        if not backup_path.exists():
            shutil.copy2(vtt_path, backup_path)
        sorted_blocks = sorted(blocks, key=cue_start)
        write_text_atomic(
            str(vtt_path),
            header + "\n\n" + "\n\n".join(sorted_blocks) + "\n",
        )
    return inserted, duplicates


def run_suppressed_cue_restore(
    *,
    global_config_path: str,
    entity: str,
    log_file: str,
    model: str | None = None,
    review_csv: str | None = None,
    write_review_path: str | None = None,
    vtt_dir: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """
    Create a suppressed-cue review CSV or restore approved cues.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param log_file: Transcription log containing suppression messages
    :param model: Optional transcription model suffix override
    :param review_csv: Reviewed CSV containing action=restore rows
    :param write_review_path: Optional review CSV output path
    :param vtt_dir: Optional VTT directory override
    :param apply: Whether to modify VTT files
    :return: Review generation or restoration summary
    :raises ValidationError: If required inputs are missing or invalid
    """
    resolved = resolve_transcript_audit_paths(
        global_config_path,
        entity=entity,
        model=model,
        input_path=vtt_dir,
    )
    log_path = Path(log_file).expanduser().resolve()
    cues = parse_suppression_log(log_path)

    if write_review_path:
        output_path = Path(write_review_path).expanduser().resolve()
        count = write_review_csv(cues, output_path)
        return {
            "mode": "review",
            "entity": entity,
            "model": resolved["model"],
            "log_file": str(log_path),
            "review_csv": str(output_path),
            "cue_count": count,
            "apply": False,
        }

    if not review_csv:
        raise ValidationError(
            "--review-csv is required unless --write-review-csv is used"
        )
    review_path = Path(review_csv).expanduser().resolve()
    approved = load_restore_allowlist(review_path)
    approved_cues = [cue for cue in cues if cue.key in approved]
    grouped: dict[str, list[SuppressedCue]] = {}
    for cue in approved_cues:
        grouped.setdefault(cue.base, []).append(cue)

    transcript_root = Path(resolved["input_path"]).resolve()
    if not transcript_root.is_dir():
        raise ValidationError(f"VTT directory does not exist: {transcript_root}")

    results: list[dict[str, Any]] = []
    total_inserted = 0
    total_duplicates = 0
    missing = 0
    for base, file_cues in sorted(grouped.items()):
        safe_base = sanitize_path_component(base)
        vtt_path = Path(
            resolve_contained_path(transcript_root, f"{safe_base}.vtt")
        )
        if not vtt_path.is_file():
            missing += 1
            results.append(
                {
                    "base": base,
                    "vtt_path": str(vtt_path),
                    "status": "missing",
                    "inserted": 0,
                    "duplicates": 0,
                }
            )
            continue
        inserted, duplicates = restore_cues_for_file(
            vtt_path,
            file_cues,
            apply=apply,
        )
        total_inserted += inserted
        total_duplicates += duplicates
        results.append(
            {
                "base": base,
                "vtt_path": str(vtt_path),
                "status": "applied" if apply else "dry-run",
                "inserted": inserted,
                "duplicates": duplicates,
            }
        )

    return {
        "mode": "restore",
        "entity": entity,
        "model": resolved["model"],
        "log_file": str(log_path),
        "review_csv": str(review_path),
        "vtt_dir": str(transcript_root),
        "approved_count": len(approved_cues),
        "inserted_count": total_inserted,
        "duplicate_count": total_duplicates,
        "missing_vtt_count": missing,
        "apply": apply,
        "results": results,
    }
