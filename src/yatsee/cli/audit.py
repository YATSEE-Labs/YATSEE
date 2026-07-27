"""
CLI registration and handlers for top-level YATSEE audit commands.

The command shape is ``yatsee audit <stage>``. Stage-specific report and
remediation behavior remains isolated behind the selected artifact boundary.
"""

from __future__ import annotations

import argparse
import shlex


def register_audit_commands(subparsers: argparse._SubParsersAction) -> None:
    """
    Register top-level audit stage commands.

    :param subparsers: Root argparse subparser registry
    :return: None
    """
    audit_parser = subparsers.add_parser(
        "audit",
        help="Audit pipeline artifacts and prepare reviewed remediation",
    )
    audit_subparsers = audit_parser.add_subparsers(dest="audit_stage")

    records_parser = audit_subparsers.add_parser(
        "records",
        help="Audit existing Records artifacts without invoking an LLM",
    )
    _add_records_audit_arguments(records_parser)
    records_parser.set_defaults(handler=handle_audit_records)

    transcript_parser = audit_subparsers.add_parser(
        "transcript",
        help="Audit VTT transcript artifacts",
    )
    _add_transcript_report_arguments(transcript_parser)
    transcript_parser.set_defaults(handler=handle_audit_transcript)
    transcript_subparsers = transcript_parser.add_subparsers(
        dest="transcript_audit_action"
    )

    reset_parser = transcript_subparsers.add_parser(
        "reset",
        help="Prepare selected VTT artifacts for retranscription",
    )
    _add_transcript_reset_arguments(reset_parser)
    reset_parser.set_defaults(handler=handle_audit_transcript_reset)

    restore_parser = transcript_subparsers.add_parser(
        "restore",
        help="Review or restore VTT cues suppressed during transcription",
    )
    _add_transcript_restore_arguments(restore_parser)
    restore_parser.set_defaults(handler=handle_audit_transcript_restore)


def _add_records_audit_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Register deterministic Records audit arguments.

    :param parser: Records audit parser
    :return: None
    """
    parser.add_argument(
        "-e",
        "--entity",
        required=True,
        help="Entity handle whose Records corpus should be audited",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Configured Records model override",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        help="Audit report directory override",
    )
    parser.add_argument(
        "-i",
        "--meeting",
        dest="meeting_filter",
        help="Audit only meeting directories containing this text",
    )
    profile_group = parser.add_mutually_exclusive_group()
    profile_group.add_argument(
        "--profile",
        dest="audit_profile",
        choices=["balanced", "strict"],
        default="balanced",
        help=(
            "Audit sensitivity profile; balanced is calibrated for practical "
            "small-model extraction"
        ),
    )
    profile_group.add_argument(
        "--strict",
        dest="audit_profile",
        action="store_const",
        const="strict",
        help="Alias for --profile strict",
    )
    rebuild_group = parser.add_mutually_exclusive_group()
    rebuild_group.add_argument(
        "--rebuild",
        dest="rebuild_mode",
        action="store_const",
        const="plan",
        help=(
            "Calculate structurally justified meeting rebuild candidates, "
            "write a manifest, and stop without invoking an LLM"
        ),
    )
    rebuild_group.add_argument(
        "--rebuild-auto",
        dest="rebuild_mode",
        action="store_const",
        const="auto",
        help=(
            "Attempt each structurally justified meeting rebuild once, then "
            "rerun the audit without retrying remaining failures"
        ),
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=["all", "json", "csv", "markdown"],
        default="all",
        help="Report format to write",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Maximum detailed meetings in the Markdown review queue",
    )
    parser.add_argument(
        "--min-risk",
        type=int,
        default=1,
        help="Minimum risk score for detailed Markdown findings",
    )


def _add_transcript_report_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Register read-only transcript report arguments.

    :param parser: Transcript audit parser
    :return: None
    """
    parser.add_argument(
        "-e",
        "--entity",
        help="Entity handle whose VTT artifacts should be audited",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Transcription model suffix override",
    )
    parser.add_argument(
        "-i",
        "--input",
        dest="input_path",
        help="VTT file or directory override",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        help="Audit report directory override",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=["all", "json", "markdown"],
        default="all",
        help="Report format to write",
    )
    parser.add_argument(
        "--prefix-words",
        type=int,
        default=16,
        help="Leading words used to detect repeated cue prefixes",
    )
    parser.add_argument(
        "--max-words-per-second",
        type=float,
        default=4.0,
        help="Maximum plausible cue speech rate",
    )
    parser.add_argument(
        "--chunk-duration",
        type=float,
        default=600.0,
        help="Expected transcription chunk duration in seconds",
    )
    parser.add_argument(
        "--overlap-seconds",
        type=float,
        default=2.0,
        help="Expected transcription chunk overlap in seconds",
    )
    parser.add_argument(
        "--seam-threshold",
        type=float,
        default=3.0,
        help="Distance from an estimated chunk seam considered adjacent",
    )


def _add_transcript_reset_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Register reviewed VTT reset arguments.

    :param parser: Transcript reset parser
    :return: None
    """
    parser.add_argument(
        "targets",
        nargs="*",
        help="Source IDs, VTT filenames, or stems to reset",
    )
    parser.add_argument("-e", "--entity", required=True, help="Entity handle")
    parser.add_argument("-m", "--model", help="Transcription model suffix override")
    parser.add_argument(
        "--target-file",
        help="Newline-delimited source IDs, VTT filenames, or stems",
    )
    parser.add_argument(
        "--report",
        dest="report_path",
        help=(
            "Transcript audit JSON report. When no explicit targets are supplied, "
            "the canonical report is used if present"
        ),
    )
    parser.add_argument(
        "--status",
        dest="statuses",
        action="append",
        choices=["block", "rerun", "review", "pass"],
        help="Report status to reset; repeat as needed. Defaults to block/rerun",
    )
    parser.add_argument(
        "--tracker",
        dest="tracker_paths",
        action="append",
        default=[],
        help="Explicit tracker file inside the entity directory; repeat as needed",
    )
    parser.add_argument(
        "--include-all-trackers",
        action="store_true",
        help="Relax tracker discovery to all likely line-based tracker files",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete selected VTTs and update trackers; default is dry-run",
    )


def _add_transcript_restore_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Register suppressed-cue review and restoration arguments.

    :param parser: Transcript restore parser
    :return: None
    """
    parser.add_argument("-e", "--entity", required=True, help="Entity handle")
    parser.add_argument("-m", "--model", help="Transcription model suffix override")
    parser.add_argument(
        "--log-file",
        required=True,
        help="Transcription log containing suppression messages",
    )
    parser.add_argument(
        "--vtt-dir",
        help="VTT directory override",
    )
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument(
        "--review-csv",
        help="Reviewed CSV containing action=restore rows",
    )
    review_group.add_argument(
        "--write-review-csv",
        dest="write_review_path",
        help="Write a review CSV and stop",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write restored cues; default is dry-run",
    )


def handle_audit_records(args: argparse.Namespace) -> int:
    """
    Audit existing Records artifacts and print a compact corpus report.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.audit.records import run_records_audit

    result = run_records_audit(
        global_config_path=args.config,
        entity=args.entity,
        model=args.model,
        output_dir=args.output_dir,
        meeting_filter=args.meeting_filter,
        output_format=args.output_format,
        top=args.top,
        min_risk=args.min_risk,
        audit_profile=args.audit_profile,
        rebuild_mode=args.rebuild_mode,
    )

    print(f"Entity: {result['entity']}")
    print(f"Model: {result['model']}")
    print(f"Audit profile: {result['audit_profile']}")
    print(f"Records directory: {result['records_dir']}")
    print(f"Audit output directory: {result['output_dir']}")
    print(f"Meetings audited: {result['meeting_count']}")
    print(f"Meetings with findings: {result['meetings_with_findings']}")
    print(
        "Findings: "
        f"{result['critical_findings']} critical, "
        f"{result['high_findings']} high, "
        f"{result['medium_findings']} medium, "
        f"{result['low_findings']} low"
    )
    for report_format, report_path in sorted(result["report_paths"].items()):
        print(f"{report_format.title()} report: {report_path}")

    if result.get("rebuild_mode"):
        print(
            "Rebuild candidates: "
            f"{result['rebuild_candidate_count']} "
            f"({result['rebuildable_count']} rebuildable, "
            f"{result['rebuild_blocked_count']} blocked)"
        )
        for candidate in result["rebuild_plan"]["candidates"]:
            disposition = "rebuildable" if candidate["rebuildable"] else "blocked"
            print(f"- {candidate['meeting']} ({disposition})")
            for reason in candidate["reasons"]:
                print(f"  Reason: {reason['code']} — {reason['message']}")
            for blocker in candidate.get("blockers", []):
                print(f"  Blocker: {blocker}")
        print(f"Rebuild manifest: {result['rebuild_manifest_path']}")

    if result.get("rebuild_execution_summary"):
        summary = result["rebuild_execution_summary"]
        print(
            "Rebuild execution: "
            f"{summary['attempted']} attempted, "
            f"{summary['complete']} complete, "
            f"{summary['incomplete']} incomplete, "
            f"{summary['failed']} failed, "
            f"{summary['blocked']} blocked"
        )
        print(
            "Remaining rebuild candidates: "
            f"{result['remaining_rebuild_candidate_count']}"
        )
    return 0


def handle_audit_transcript(args: argparse.Namespace) -> int:
    """
    Audit VTT transcript artifacts and print report locations.

    :param args: Parsed CLI arguments
    :return: Process exit code reflecting block/rerun findings
    """
    from yatsee.audit.transcript import run_transcript_audit

    result = run_transcript_audit(
        global_config_path=args.config,
        entity=args.entity,
        model=args.model,
        input_path=args.input_path,
        output_dir=args.output_dir,
        output_format=args.output_format,
        prefix_words=args.prefix_words,
        max_words_per_second=args.max_words_per_second,
        chunk_duration=args.chunk_duration,
        overlap_seconds=args.overlap_seconds,
        seam_threshold=args.seam_threshold,
    )
    counts = result["status_counts"]
    issues = result["issue_counts"]
    print(f"Entity: {result['entity']}")
    print(f"Transcription model: {result['model']}")
    print(f"Transcript input: {result['input_path']}")
    print(f"Audit output directory: {result['output_dir']}")
    print(f"VTT files audited: {result['file_count']}")
    print(
        "Status: "
        f"{counts['block']} block, "
        f"{counts['rerun']} rerun, "
        f"{counts['review']} review, "
        f"{counts['pass']} pass"
    )
    print(
        "Issues: "
        f"{issues['asr_loop']} ASR loops, "
        f"{issues['fixable']} fixable timing candidates"
    )
    for report_format, report_path in sorted(result["report_paths"].items()):
        print(f"{report_format.title()} report: {report_path}")
    return result["exit_code"]


def handle_audit_transcript_reset(args: argparse.Namespace) -> int:
    """
    Preview or apply a reviewed transcript reset.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.audit.transcript import run_transcript_reset

    result = run_transcript_reset(
        global_config_path=args.config,
        entity=args.entity,
        model=args.model,
        targets=args.targets,
        target_file=args.target_file,
        report_path=args.report_path,
        statuses=args.statuses or (),
        tracker_paths=args.tracker_paths,
        include_all_trackers=args.include_all_trackers,
        apply=args.apply,
    )
    print(f"Mode: {'APPLY' if result['apply'] else 'DRY-RUN'}")
    print(f"Entity: {result['entity']}")
    print(f"Transcription model: {result['model']}")
    print(f"Selected targets: {len(result['targets'])}")
    print(
        f"VTT files {'deleted' if result['apply'] else 'to delete'}: "
        f"{result['vtt_count']}"
    )
    print(
        f"Tracker lines {'removed' if result['apply'] else 'to remove'}: "
        f"{result['tracker_line_count']}"
    )
    for path in result["vtt_paths"]:
        print(f"- VTT: {path}")
    for update in result["tracker_updates"]:
        print(f"- Tracker: {update['path']} ({update['removed_lines']} line(s))")
    print("Recommended rebuild command:")
    print(f"  {shlex.join(result['rebuild_command'])}")
    if not result["apply"]:
        print("Dry-run only. Re-run with --apply to make changes.")
    return 0


def handle_audit_transcript_restore(args: argparse.Namespace) -> int:
    """
    Create a suppressed-cue review CSV or restore approved cues.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.audit.transcript import run_suppressed_cue_restore

    result = run_suppressed_cue_restore(
        global_config_path=args.config,
        entity=args.entity,
        log_file=args.log_file,
        model=args.model,
        review_csv=args.review_csv,
        write_review_path=args.write_review_path,
        vtt_dir=args.vtt_dir,
        apply=args.apply,
    )
    if result["mode"] == "review":
        print(f"Review CSV: {result['review_csv']}")
        print(f"Suppressed cues: {result['cue_count']}")
        return 0

    print(f"Mode: {'APPLY' if result['apply'] else 'DRY-RUN'}")
    print(f"Approved cues: {result['approved_count']}")
    print(f"Cues inserted: {result['inserted_count']}")
    print(f"Duplicates skipped: {result['duplicate_count']}")
    print(f"Missing VTT files: {result['missing_vtt_count']}")
    for item in result["results"]:
        print(
            f"- {item['status']}: {item['vtt_path']} "
            f"insert={item['inserted']} duplicate={item['duplicates']}"
        )
    if not result["apply"]:
        print("Dry-run only. Re-run with --apply to write restored cues.")
    return 0
