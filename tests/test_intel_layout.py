"""
Tests for consolidated model-specific intelligence workspaces.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from yatsee.core.errors import ValidationError
from yatsee.intel.layout import resolve_intel_layout
from yatsee.audit.resolve import resolve_records_audit_paths
from yatsee.intel.resolve import (
    resolve_event_paths,
    resolve_record_paths,
    resolve_summary_paths,
)


def _write_project(root: Path, *, intel_append_dir: str | None = None) -> Path:
    """
    Write a minimal entity and model configuration.

    :param root: Temporary project root
    :param intel_append_dir: Optional model workspace override
    :return: Global configuration path
    """
    entity_dir = root / "data" / "test_entity"
    (entity_dir / "normalized").mkdir(parents=True)
    (entity_dir / "normalized" / "meeting.txt").write_text(
        "The board met.",
        encoding="utf-8",
    )
    (entity_dir / "config.toml").write_text(
        """
[settings]
data_path = "./data/test_entity"
records_model = "gemma4:e4b"
summarization_model = "gemma4:e4b"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    intel_line = (
        f'intel_append_dir = "{intel_append_dir}"\n'
        if intel_append_dir is not None
        else ""
    )
    config_path = root / "yatsee.toml"
    config_path.write_text(
        f"""
[system]
root_data_dir = "./data"
default_records_model = "gemma4:e4b"
default_summarization_model = "gemma4:e4b"

[models."gemma4:e4b"]
{intel_line}max_tokens = 1500
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
    return config_path


def test_resolve_intel_layout_uses_one_model_workspace(tmp_path: Path) -> None:
    """
    Resolve all intelligence artifact families beneath one model lineage root.
    """
    entity_dir = tmp_path / "data" / "test_entity"
    entity_dir.mkdir(parents=True)

    layout = resolve_intel_layout(str(entity_dir), "gemma4:e4b", {})

    assert Path(layout["intel_dir"]) == entity_dir / "intel_gemma4_e4b"
    assert Path(layout["summaries_dir"]) == (
        entity_dir / "intel_gemma4_e4b" / "summaries"
    )
    assert Path(layout["records_dir"]) == (
        entity_dir / "intel_gemma4_e4b" / "records"
    )
    assert Path(layout["events_dir"]) == (
        entity_dir / "intel_gemma4_e4b" / "events"
    )


def test_entity_stage_resolvers_use_shared_model_workspace(
    tmp_path: Path,
) -> None:
    """
    Resolve intelligence stages together while audit reports remain entity-level.
    """
    config_path = _write_project(tmp_path)
    previous_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        summary = resolve_summary_paths(str(config_path), entity="test_entity")
        records = resolve_record_paths(str(config_path), entity="test_entity")
        audit = resolve_records_audit_paths(str(config_path), entity="test_entity")
        events = resolve_event_paths(str(config_path), entity="test_entity")
    finally:
        os.chdir(previous_cwd)

    workspace = tmp_path / "data" / "test_entity" / "intel_gemma4_e4b"
    assert Path(summary["records_dir"]) == workspace / "records"
    assert Path(summary["events_dir"]) == workspace / "events"
    assert Path(summary["output_dir"]) == workspace / "summaries"
    assert Path(records["output_dir"]) == workspace / "records"
    assert Path(audit["records_dir"]) == workspace / "records"
    assert Path(audit["output_dir"]) == (
        tmp_path / "data" / "test_entity" / "audit" / "records" / "gemma4_e4b"
    )
    assert Path(events["records_dir"]) == workspace / "records"
    assert Path(events["output_dir"]) == workspace / "events"


def test_intel_workspace_override_cannot_escape_entity_root(tmp_path: Path) -> None:
    """
    Reject a configured intelligence workspace that escapes entity storage.
    """
    config_path = _write_project(tmp_path, intel_append_dir="../../outside")
    previous_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(ValidationError):
            resolve_records_audit_paths(str(config_path), entity="test_entity")
    finally:
        os.chdir(previous_cwd)


def test_summary_resolution_supports_standalone_input(tmp_path: Path) -> None:
    """
    Place standalone Records, Events, and summaries beside the source workspace.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    config_path = _write_project(tmp_path)
    source_path = tmp_path / "external" / "document.txt"
    source_path.parent.mkdir()
    source_path.write_text("Research notes.", encoding="utf-8")

    previous_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        resolved = resolve_summary_paths(
            str(config_path),
            entity=None,
            input_path=str(source_path),
        )
    finally:
        os.chdir(previous_cwd)

    workspace = source_path.parent / "intel_gemma4_e4b"
    assert Path(resolved["records_dir"]) == workspace / "records"
    assert Path(resolved["events_dir"]) == workspace / "events"
    assert Path(resolved["output_dir"]) == workspace / "summaries"
    assert resolved["file_list"] == [str(source_path)]
