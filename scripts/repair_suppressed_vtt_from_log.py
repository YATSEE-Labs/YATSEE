#!/usr/bin/env python3
"""
Repair reviewed suppressed VTT cues from a YATSEE transcription log.

The transcription log contains enough information to reconstruct suppressed
candidate cues: source base name, start timestamp, end timestamp, and text. This
script intentionally does not restore everything by default. Operators must pass
a review CSV and mark rows with action=restore before the script writes changes.

:param log_file: Transcription log containing suppression messages
:param vtt_dir: Directory containing generated .vtt files
:return: Exit status code
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SUPPRESSION_RE = re.compile(
    r"Suppressed likely hotword hallucination from VTT output: "
    r"(?P<base>.+?) "
    r"(?P<start>\d{2}:\d{2}:\d{2}\.\d{3}) --> "
    r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3}) \| "
    r"(?P<text>.*)$"
)

REVIEW_FIELDS = ("action", "base", "start", "end", "text", "assessment", "reason")


@dataclass(frozen=True)
class SuppressedCue:
    """
    Suppressed VTT cue parsed from a transcription log line.

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
        Return a stable key for review CSV matching.

        :return: Tuple of base, start, end, and text
        """
        return (self.base, self.start, self.end, self.text)


def parse_suppression_log(log_path: Path) -> list[SuppressedCue]:
    """
    Parse suppressed VTT cue entries from a transcription log.

    :param log_path: Path to the transcription log
    :return: Parsed suppressed cue entries
    """
    cues: list[SuppressedCue] = []
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
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
    Produce a conservative starting assessment for a suppressed cue.

    This heuristic is only a review aid. It intentionally uses three actions:
    restore for high-confidence civic roll-call/vote content, skip for clear
    hotword explosions, and review for ambiguous roster-like cues.

    :param cue: Suppressed cue entry
    :return: Tuple of action, assessment, and reason
    """
    text = cue.text.casefold()
    alias_explosion_terms = {
        "huffines",
        "heimerdinger",
        "dovie",
        "gertrude",
        "donald",
    }
    civic_terms = {
        "absent",
        "alderperson",
        "alderpersons",
        "mayor",
        "sanders is absent",
    }

    if sum(1 for term in alias_explosion_terms if term in text) >= 2:
        return "skip", "likely_hotword_explosion", "contains multiple alias-family terms"

    if any(term in text for term in civic_terms):
        return "restore", "likely_civic_roll_call", "contains civic attendance or office terms"

    return "review", "ambiguous_roster", "roster-like cue needs local context before restore"


def write_review_csv(cues: Iterable[SuppressedCue], output_path: Path) -> None:
    """
    Write a review CSV from parsed suppression entries.

    :param cues: Suppressed cue entries
    :param output_path: CSV path to write
    """
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for cue in cues:
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


def load_restore_allowlist(review_path: Path) -> set[tuple[str, str, str, str]]:
    """
    Load reviewed cues marked action=restore from a CSV.

    :param review_path: Review CSV path
    :return: Set of cue keys approved for restoration
    """
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
    Read a VTT file into a header and cue blocks.

    :param vtt_path: VTT file path
    :return: Tuple of header text and cue blocks
    """
    raw = vtt_path.read_text(encoding="utf-8")
    parts = raw.split("\n\n")
    header = parts[0].rstrip() if parts else "WEBVTT"
    cues = [part.strip() for part in parts[1:] if part.strip()]
    return header, cues


def cue_start(block: str) -> str:
    """
    Return the start timestamp for a VTT cue block.

    :param block: VTT cue block
    :return: Start timestamp or a high sentinel for unknown blocks
    """
    first_line = block.splitlines()[0] if block.splitlines() else ""
    if "-->" not in first_line:
        return "99:99:99.999"
    return first_line.split("-->", 1)[0].strip()


def cue_block(cue: SuppressedCue) -> str:
    """
    Format a suppressed cue as a VTT block.

    :param cue: Suppressed cue entry
    :return: VTT cue block
    """
    return f"{cue.start} --> {cue.end}\n{cue.text}"


def restore_cues_for_file(vtt_path: Path, cues: list[SuppressedCue], apply: bool) -> tuple[int, int]:
    """
    Restore approved cues to a VTT file if they are not already present.

    :param vtt_path: VTT file path
    :param cues: Approved cues for this VTT
    :param apply: Whether to write changes
    :return: Tuple of inserted count and duplicate count
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
        vtt_path.write_text(header + "\n\n" + "\n\n".join(sorted_blocks) + "\n", encoding="utf-8")

    return inserted, duplicates


def build_parser() -> argparse.ArgumentParser:
    """
    Build the command-line parser.

    :return: Configured argument parser
    """
    parser = argparse.ArgumentParser(
        description="Repair reviewed YATSEE VTT cues from suppression log entries."
    )
    parser.add_argument("--log-file", required=True, help="Transcription log containing suppression messages")
    parser.add_argument("--vtt-dir", help="Directory containing .vtt files to repair")
    parser.add_argument("--review-csv", help="CSV with action=restore rows approved for insertion")
    parser.add_argument("--write-review-csv", help="Write a review CSV and exit")
    parser.add_argument("--apply", action="store_true", help="Write repaired VTT files. Default is dry-run only")
    return parser


def main() -> int:
    """
    Run the repair utility.

    :return: Process exit status
    """
    parser = build_parser()
    args = parser.parse_args()

    log_path = Path(args.log_file).expanduser().resolve()
    if not log_path.is_file():
        parser.error(f"log file does not exist: {log_path}")

    cues = parse_suppression_log(log_path)
    if args.write_review_csv:
        write_review_csv(cues, Path(args.write_review_csv).expanduser().resolve())
        print(f"Wrote review CSV for {len(cues)} suppressed cues")
        return 0

    if not args.vtt_dir or not args.review_csv:
        parser.error("--vtt-dir and --review-csv are required unless --write-review-csv is used")

    vtt_dir = Path(args.vtt_dir).expanduser().resolve()
    review_path = Path(args.review_csv).expanduser().resolve()
    if not vtt_dir.is_dir():
        parser.error(f"VTT directory does not exist: {vtt_dir}")
    if not review_path.is_file():
        parser.error(f"review CSV does not exist: {review_path}")

    approved = load_restore_allowlist(review_path)
    approved_cues = [cue for cue in cues if cue.key in approved]
    grouped: dict[str, list[SuppressedCue]] = {}
    for cue in approved_cues:
        grouped.setdefault(cue.base, []).append(cue)

    total_inserted = 0
    total_duplicates = 0
    for base, file_cues in sorted(grouped.items()):
        vtt_path = vtt_dir / f"{base}.vtt"
        if not vtt_path.is_file():
            print(f"missing VTT for approved cue set: {vtt_path}", file=sys.stderr)
            continue
        inserted, duplicates = restore_cues_for_file(vtt_path, file_cues, apply=args.apply)
        total_inserted += inserted
        total_duplicates += duplicates
        mode = "applied" if args.apply else "dry-run"
        print(f"{mode}: {vtt_path.name}: insert={inserted}, duplicate={duplicates}")

    print(
        f"approved={len(approved_cues)}, inserted={total_inserted}, "
        f"duplicates={total_duplicates}, apply={args.apply}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
