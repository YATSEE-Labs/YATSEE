"""
Tests for top-level transcript audit reporting and reviewed remediation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from yatsee.audit.transcript import (
    run_suppressed_cue_restore,
    run_transcript_audit,
    run_transcript_reset,
)
from yatsee.cli.main import build_parser


def _write_project(root: Path) -> tuple[Path, Path]:
    """
    Write minimal entity configuration and a transcript directory.

    :param root: Temporary project root
    :return: Global config path and transcript directory
    """
    entity_dir = root / "data" / "test_entity"
    transcript_dir = entity_dir / "transcripts_medium"
    transcript_dir.mkdir(parents=True)
    (entity_dir / "config.toml").write_text(
        """
[settings]
data_path = "./data/test_entity"
transcription_model = "medium"
records_model = "gemma4:e4b"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config_path = root / "yatsee.toml"
    config_path.write_text(
        """
[system]
root_data_dir = "./data"
default_transcription_model = "medium"
default_records_model = "gemma4:e4b"

[models."gemma4:e4b"]
records_max_tokens = 1500
num_ctx = 16384

[entities.test_entity]
display_name = "Test Entity"
entity = "test_entity"
base = ""
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_path, transcript_dir


def _write_loop_vtt(path: Path) -> None:
    """
    Write a VTT containing one actionable repeated ASR loop.

    :param path: VTT destination
    :return: None
    """
    repeated = (
        "this is a deliberately repeated civic meeting sentence with enough words "
        "to create one stable prefix for transcript loop detection"
    )
    path.write_text(
        "WEBVTT\n\n"
        f"00:00:00.000 --> 00:00:30.000\n{repeated}\n\n"
        f"00:00:30.000 --> 00:01:00.000\n{repeated}\n\n"
        f"00:01:00.000 --> 00:01:30.000\n{repeated}\n\n"
        f"00:01:30.000 --> 00:02:00.000\n{repeated}\n",
        encoding="utf-8",
    )


def test_transcript_audit_writes_entity_level_reports(tmp_path: Path) -> None:
    """
    Write transcript audit reports outside the transcript artifact tree.
    """
    config_path, transcript_dir = _write_project(tmp_path)
    _write_loop_vtt(transcript_dir / "abc.meeting.vtt")

    previous_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        result = run_transcript_audit(
            global_config_path=str(config_path),
            entity="test_entity",
        )
    finally:
        os.chdir(previous_cwd)

    expected = tmp_path / "data" / "test_entity" / "audit" / "transcript" / "medium"
    assert Path(result["output_dir"]) == expected
    assert result["status_counts"]["rerun"] == 1
    assert result["exit_code"] == 1
    assert Path(result["report_paths"]["json"]).is_file()
    assert Path(result["report_paths"]["markdown"]).is_file()

    report = json.loads(Path(result["report_paths"]["json"]).read_text())
    assert report["artifact_kind"] == "transcript_audit"
    assert report["files"][0]["status"] == "rerun"


def test_transcript_reset_uses_canonical_report_and_requires_apply(
    tmp_path: Path,
) -> None:
    """
    Select rerun targets from the canonical report and remain dry-run by default.
    """
    config_path, transcript_dir = _write_project(tmp_path)
    vtt_path = transcript_dir / "abc.meeting.vtt"
    _write_loop_vtt(vtt_path)

    previous_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        run_transcript_audit(
            global_config_path=str(config_path),
            entity="test_entity",
        )
        preview = run_transcript_reset(
            global_config_path=str(config_path),
            entity="test_entity",
        )
        assert preview["apply"] is False
        assert preview["vtt_count"] == 1
        assert vtt_path.is_file()

        applied = run_transcript_reset(
            global_config_path=str(config_path),
            entity="test_entity",
            apply=True,
        )
    finally:
        os.chdir(previous_cwd)

    assert applied["vtt_count"] == 1
    assert not vtt_path.exists()
    assert applied["rebuild_command"][:4] == [
        "yatsee",
        "audio",
        "transcribe",
        "-e",
    ]


def test_suppressed_cue_restore_is_review_gated(tmp_path: Path) -> None:
    """
    Create a review CSV and restore only an approved cue with an explicit apply.
    """
    config_path, transcript_dir = _write_project(tmp_path)
    vtt_path = transcript_dir / "abc.meeting.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nOpening remarks.\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "transcribe.log"
    log_path.write_text(
        "Suppressed likely hotword hallucination from VTT output: "
        "abc.meeting 00:00:03.000 --> 00:00:05.000 | "
        "Mayor Miller is absent.\n",
        encoding="utf-8",
    )
    review_path = tmp_path / "review.csv"

    previous_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        review = run_suppressed_cue_restore(
            global_config_path=str(config_path),
            entity="test_entity",
            log_file=str(log_path),
            write_review_path=str(review_path),
        )
        assert review["cue_count"] == 1

        preview = run_suppressed_cue_restore(
            global_config_path=str(config_path),
            entity="test_entity",
            log_file=str(log_path),
            review_csv=str(review_path),
        )
        assert preview["inserted_count"] == 1
        assert "Mayor Miller is absent." not in vtt_path.read_text()

        applied = run_suppressed_cue_restore(
            global_config_path=str(config_path),
            entity="test_entity",
            log_file=str(log_path),
            review_csv=str(review_path),
            apply=True,
        )
    finally:
        os.chdir(previous_cwd)

    assert applied["inserted_count"] == 1
    assert "Mayor Miller is absent." in vtt_path.read_text()
    assert vtt_path.with_suffix(".vtt.bak").is_file()


def test_top_level_audit_cli_routes_by_stage() -> None:
    """
    Register Records and transcript audits beneath the top-level audit command.
    """
    parser = build_parser()

    records = parser.parse_args(
        ["audit", "records", "-e", "test_entity", "--strict"]
    )
    assert records.audit_stage == "records"
    assert records.audit_profile == "strict"

    transcript = parser.parse_args(
        ["audit", "transcript", "-e", "test_entity", "--model", "medium"]
    )
    assert transcript.audit_stage == "transcript"
    assert transcript.entity == "test_entity"

    reset = parser.parse_args(
        ["audit", "transcript", "reset", "-e", "test_entity", "abc"]
    )
    assert reset.transcript_audit_action == "reset"
    assert reset.targets == ["abc"]

    restore = parser.parse_args(
        [
            "audit",
            "transcript",
            "restore",
            "-e",
            "test_entity",
            "--log-file",
            "transcribe.log",
            "--write-review-csv",
            "review.csv",
        ]
    )
    assert restore.transcript_audit_action == "restore"

    try:
        parser.parse_args(["intel", "audit", "-e", "test_entity"])
    except SystemExit:
        pass
    else:
        raise AssertionError("Legacy intel audit command should not remain registered")
