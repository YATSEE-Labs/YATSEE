"""
Tests for production Records-first summary orchestration.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from yatsee.core.errors import ValidationError
from yatsee.intel.llm import LLMRuntime
from yatsee.intel.runner import _resolve_summary_profile, run_summary_stage


def _runtime() -> LLMRuntime:
    """
    Build a fake local runtime.

    :return: LLM runtime
    """
    return LLMRuntime(
        provider_name="ollama",
        provider_target="http://127.0.0.1:11434",
        api_key=None,
        model="gemma4:e4b",
        num_ctx=16384,
    )


def test_summary_cli_accepts_full_source_path() -> None:
    """
    Preserve ``-i`` as a source file or directory rather than an artifact filter.

    :return: None
    """
    from yatsee.cli.main import build_parser

    args = build_parser().parse_args(
        [
            "intel",
            "summarize",
            "-e",
            "test_entity",
            "-i",
            "/tmp/full_meeting_name.txt",
        ]
    )

    assert args.entity == "test_entity"
    assert args.txt_input == "/tmp/full_meeting_name.txt"
    assert args.job_profile is None
    assert args.chunk_style == "sentence"


def test_standalone_summary_requires_explicit_profile() -> None:
    """
    Prevent arbitrary standalone content from silently using civic assumptions.

    :return: None
    """
    with pytest.raises(ValidationError, match="requires --job-profile"):
        _resolve_summary_profile(None, None)

    assert _resolve_summary_profile("entity", None) == "civic"
    assert _resolve_summary_profile(None, "civic") == "civic"


def test_run_summary_stage_uses_canonical_records_then_events(tmp_path: Path) -> None:
    """
    Reuse Records extraction, refresh Events, and then synthesize the summary.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    source_path = tmp_path / "meeting.txt"
    source_path.write_text("meeting transcript", encoding="utf-8")
    records_path = tmp_path / "records" / "meeting" / "meeting.records.json"
    records_path.parent.mkdir(parents=True)
    records_path.write_text(
        '{"status":"complete","meeting_type":"general","records":[]}\n',
        encoding="utf-8",
    )
    events_artifact = {
        "status": "complete",
        "meeting": "meeting",
        "meeting_type": "general",
        "events": [],
    }
    resolved = {
        "global_cfg": {"system": {}},
        "entity_cfg": {},
        "model": "gemma4:e4b",
        "model_key": "gemma4:e4b",
        "model_cfg": {"max_tokens": 1500, "num_ctx": 16384},
        "input_dir": str(source_path),
        "file_list": [str(source_path)],
        "intel_dir": str(tmp_path / "intel_gemma4_e4b"),
        "records_dir": str(tmp_path / "records"),
        "events_dir": str(tmp_path / "events"),
        "output_dir": str(tmp_path / "summaries"),
        "max_tokens": 1500,
        "num_ctx": 16384,
    }
    prompt_bundle = {
        "path": "prompts/civic/prompts.toml",
        "fallback": False,
        "prompts": {
            "first": "{context}\n{text}",
            "multi": "{context}\n{text}",
            "final": "{context}\n{text}",
        },
        "prompt_router": {
            "general": {"first": "first", "multi": "multi", "final": "final"}
        },
        "density_keywords": {"keywords": []},
    }
    records_result = {
        "mode": "run",
        "input_dir": str(source_path),
        "output_dir": str(tmp_path / "records"),
        "model": "gemma4:e4b",
        "prompt_file": "prompts/civic/records_prompt.toml",
        "discovered": 1,
        "extracted": 0,
        "reused": 1,
        "results": [
            {
                "base_name": "meeting",
                "output_path": str(records_path),
                "status": "complete",
                "reused": True,
            }
        ],
    }
    event_result = {
        "meeting": "meeting",
        "records_path": str(records_path),
        "json_path": str(tmp_path / "events" / "meeting" / "meeting.events.json"),
        "markdown_path": str(tmp_path / "events" / "meeting" / "meeting.events.md"),
        "artifact": events_artifact,
    }
    summary_result = {
        "base_name": "meeting",
        "meeting_type": "general",
        "summary": "Summary.",
        "output_path": str(tmp_path / "summaries" / "meeting.summary.md"),
        "structured_input_tokens": 100,
        "pass_count": 0,
        "chunk_counts": [],
        "reduced_tokens": 100,
        "record_count": 0,
        "event_count": 0,
        "input_tokens": 120,
        "output_tokens": 20,
        "total_tokens": 140,
        "pricing": {"enabled": False},
    }

    with (
        patch("yatsee.intel.runner.resolve_summary_paths", return_value=resolved),
        patch(
            "yatsee.intel.runner.load_summary_prompt_bundle",
            return_value=prompt_bundle,
        ),
        patch("yatsee.intel.runner.run_records_stage", return_value=records_result) as records,
        patch(
            "yatsee.intel.runner.generate_events_for_records_artifact",
            return_value=event_result,
        ) as events,
        patch("yatsee.intel.runner.resolve_llm_runtime", return_value=_runtime()),
        patch(
            "yatsee.intel.runner.create_llm_session",
            return_value=nullcontext(object()),
        ),
        patch(
            "yatsee.intel.runner.summarize_one_meeting",
            return_value=summary_result,
        ) as summarize,
    ):
        result = run_summary_stage(
            global_config_path="yatsee.toml",
            entity="test_entity",
            input_path=str(source_path),
        )

    assert result["records_extracted"] == 0
    assert result["records_reused"] == 1
    assert result["events_processed"] == 1
    assert result["processed"] == 1
    assert records.call_args.kwargs["input_path"] == str(source_path)
    assert records.call_args.kwargs["output_dir"] == resolved["records_dir"]
    assert records.call_args.kwargs["job_profile"] == "civic"
    assert records.call_args.kwargs["chunk_style"] == "density"
    assert events.call_args.kwargs["records_path"] == str(records_path)
    assert summarize.call_args.kwargs["events_artifact"] is events_artifact


def test_standalone_input_passes_selected_profile_to_records(tmp_path: Path) -> None:
    """
    Route standalone extraction and presentation through the declared profile.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    source_path = tmp_path / "document.txt"
    source_path.write_text("source", encoding="utf-8")
    resolved = {
        "global_cfg": {"system": {}},
        "entity_cfg": {},
        "model": "gemma4:e4b",
        "model_key": "gemma4:e4b",
        "model_cfg": {"max_tokens": 1500, "num_ctx": 16384},
        "input_dir": str(source_path),
        "file_list": [str(source_path)],
        "intel_dir": str(tmp_path / "intel_gemma4_e4b"),
        "records_dir": str(tmp_path / "intel_gemma4_e4b" / "records"),
        "events_dir": str(tmp_path / "intel_gemma4_e4b" / "events"),
        "output_dir": str(tmp_path / "intel_gemma4_e4b" / "summaries"),
        "max_tokens": 1500,
        "num_ctx": 16384,
    }
    prompt_bundle = {
        "path": "prompts/custom/prompts.toml",
        "fallback": False,
        "prompts": {},
        "prompt_router": {},
        "density_keywords": {"keywords": []},
    }

    with (
        patch("yatsee.intel.runner.resolve_summary_paths", return_value=resolved),
        patch(
            "yatsee.intel.runner.load_summary_prompt_bundle",
            return_value=prompt_bundle,
        ),
        patch("yatsee.intel.runner.validate_prompt"),
        patch(
            "yatsee.intel.runner.run_records_stage",
            side_effect=ValidationError("profile has no Records contract"),
        ) as records,
    ):
        with pytest.raises(ValidationError, match="no Records contract"):
            run_summary_stage(
                global_config_path="yatsee.toml",
                input_path=str(source_path),
                job_profile="custom",
            )

    assert records.call_args.kwargs["job_profile"] == "custom"
