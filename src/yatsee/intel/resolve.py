"""
Resolution helpers for YATSEE intelligence jobs.

This module owns intelligence-stage configuration and filesystem resolution.
Generic file reading, writing, directory creation, and containment remain in the
shared core modules.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict

from yatsee.core.config import load_entity_config, load_global_config
from yatsee.core.discovery import discover_files
from yatsee.core.errors import ValidationError
from yatsee.core.io import ensure_directory
from yatsee.intel.layout import resolve_intel_layout

TRANSCRIPT_INPUT_EXTENSIONS = (".txt",)
TRANSCRIPT_EXCLUDE_SUFFIXES = ("punct.txt",)
DEFAULT_TRANSCRIPT_INPUT_DIRNAME = "normalized"


def extract_context_from_filename(
    filename: str,
    extra_note: str | None = None,
) -> str:
    """
    Derive a human-readable meeting context from a transcript filename.

    :param filename: Transcript file path
    :param extra_note: Optional additional context note
    :return: Human-readable context string
    """
    base = os.path.basename(filename)
    base = re.sub(r"^[\w\-]+?\.", "", base)
    base = base.removesuffix(".txt")
    base = base.replace("_", " ").replace("-", " ")

    meeting_match = re.match(
        r"(.*?) (\d{1,2})[ -](\d{1,2})[ -](\d{2,4})",
        base,
    )
    if meeting_match:
        kind = meeting_match.group(1).strip().title()
        month = int(meeting_match.group(2))
        day = int(meeting_match.group(3))
        year = int(meeting_match.group(4))
        if year < 100:
            year += 2000

        context = f"{kind} - {year:04d}-{month:02d}-{day:02d}"
    else:
        context = base.title()

    if extra_note:
        context += f" ({extra_note.strip()})"

    return context


def resolve_transcript_stage_paths(
    *,
    entity: str | None,
    entity_cfg: Dict[str, Any],
    input_path: str | None,
    output_dir: str | None,
    default_output_dir: str | None,
) -> Dict[str, Any]:
    """
    Resolve normalized transcript inputs and an intelligence artifact directory.

    Explicit paths are required when no entity is supplied. Entity-backed runs
    default to the entity's ``normalized`` transcript directory and the stage's
    configured artifact directory beneath ``data_path``.

    :param entity: Optional entity handle
    :param entity_cfg: Loaded entity configuration dictionary
    :param input_path: Optional transcript file or directory override
    :param output_dir: Optional artifact output directory override
    :param default_output_dir: Resolved entity-backed output directory
    :return: Resolved input path, output directory, entity data path, and files
    :raises ValidationError: If required paths or entity configuration are missing
    """
    if not entity and not input_path:
        raise ValidationError("Input path must be defined when no entity is supplied")
    if not entity and not output_dir:
        raise ValidationError("Output directory must be defined when no entity is supplied")

    data_path = entity_cfg.get("data_path") if entity_cfg else None
    if entity and not data_path:
        raise ValidationError(f"Entity '{entity}' does not define data_path")

    resolved_input_path = input_path or os.path.join(
        data_path,
        DEFAULT_TRANSCRIPT_INPUT_DIRNAME,
    )
    resolved_output_dir = output_dir or default_output_dir
    if not resolved_output_dir:
        raise ValidationError("No output directory could be resolved")
    file_list = discover_files(
        resolved_input_path,
        TRANSCRIPT_INPUT_EXTENSIONS,
        exclude_suffixes=TRANSCRIPT_EXCLUDE_SUFFIXES,
    )

    ensure_directory(resolved_output_dir)

    return {
        "data_path": data_path,
        "input_path": resolved_input_path,
        "output_dir": resolved_output_dir,
        "file_list": file_list,
    }



def _resolve_model_profile(
    global_cfg: Dict[str, Any],
    model: str,
) -> tuple[str, Dict[str, Any]]:
    """
    Resolve a configured model name case-insensitively.

    :param global_cfg: Loaded global configuration
    :param model: Requested model name
    :return: Configured model key and model configuration
    :raises ValidationError: If the model is not configured
    """
    model_map = global_cfg.get("models", {})
    model_key = next(
        (key for key in model_map if key.lower() == model.lower()),
        None,
    )
    if not model_key:
        raise ValidationError(
            f"Unsupported model '{model}'. Supported: "
            f"{', '.join(model_map.keys())}"
        )

    return model_key, model_map[model_key]


def resolve_records_model(
    global_cfg: Dict[str, Any],
    entity_cfg: Dict[str, Any],
    model: str | None,
) -> tuple[str, str, Dict[str, Any]]:
    """
    Resolve records model policy shared by extraction and audit commands.

    :param global_cfg: Loaded global configuration
    :param entity_cfg: Loaded entity configuration
    :param model: Optional records model override
    :return: Resolved model name, configured model key, and model settings
    :raises ValidationError: If no records model is configured
    """
    system_cfg = global_cfg.get("system", {})
    resolved_model = (
        model
        or entity_cfg.get("records_model")
        or entity_cfg.get("summarization_model")
        or system_cfg.get("default_records_model")
        or system_cfg.get("default_summarization_model")
    )
    if not resolved_model:
        raise ValidationError("No records model specified")

    model_key, model_cfg = _resolve_model_profile(global_cfg, resolved_model)
    return resolved_model, model_key, model_cfg


def resolve_summary_paths(
    global_config_path: str,
    *,
    entity: str | None = None,
    input_path: str | None = None,
    output_dir: str | None = None,
    model: str | None = None,
    max_words: int | None = None,
    max_tokens: int | None = None,
) -> Dict[str, Any]:
    """
    Resolve configuration, model selection, and paths for summary execution.

    This function owns summary-stage policy such as the configured summarization
    model, model-specific output directory, transcript discovery, and source
    chunk token budget. It is intentionally separate from generic filesystem I/O
    and from CLI argument parsing.

    :param global_config_path: Path to the global ``yatsee.toml`` file
    :param entity: Optional entity handle
    :param input_path: Optional normalized transcript file or directory
    :param output_dir: Optional summary output directory override
    :param model: Optional summarization model override
    :param max_words: Optional approximate word threshold for source chunks
    :param max_tokens: Optional approximate token threshold for source chunks
    :return: Dictionary of resolved summary-stage settings
    :raises ValidationError: If required configuration or runtime inputs are invalid
    """
    global_cfg = load_global_config(global_config_path)
    entity_cfg = load_entity_config(global_cfg, entity) if entity else {}

    if not entity and not input_path:
        raise ValidationError("Without --entity, --txt-input must be defined")
    if entity and not entity_cfg.get("data_path"):
        raise ValidationError("No entity data_path configured")

    resolved_model = (
        model
        or entity_cfg.get("summarization_model")
        or global_cfg.get("system", {}).get("default_summarization_model")
    )
    if not resolved_model:
        raise ValidationError("No summarization model specified")

    model_key, model_cfg = _resolve_model_profile(global_cfg, resolved_model)

    if not entity and not output_dir:
        raise ValidationError("Without --entity, --output-dir must be defined")

    intel_layout = (
        resolve_intel_layout(entity_cfg["data_path"], model_key, model_cfg)
        if entity
        else None
    )
    paths = resolve_transcript_stage_paths(
        entity=entity,
        entity_cfg=entity_cfg,
        input_path=input_path,
        output_dir=output_dir,
        default_output_dir=(
            intel_layout["summaries_dir"] if intel_layout else None
        ),
    )
    if not paths["file_list"]:
        raise ValidationError(
            f"No transcript files found in: {paths['input_path']}"
        )

    if max_words and not max_tokens:
        resolved_max_tokens = int(max_words / 0.75)
    else:
        resolved_max_tokens = max_tokens or model_cfg.get("max_tokens", 2500)

    num_ctx = model_cfg.get("num_ctx", 8192)
    if resolved_max_tokens >= num_ctx:
        raise ValidationError(
            f"Chunk size ({resolved_max_tokens}) exceeds or collides with "
            f"model context ({num_ctx})"
        )

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "model": resolved_model,
        "model_key": model_key,
        "model_cfg": model_cfg,
        "input_dir": paths["input_path"],
        "file_list": paths["file_list"],
        "output_dir": paths["output_dir"],
        "intel_dir": intel_layout["intel_dir"] if intel_layout else None,
        "max_tokens": resolved_max_tokens,
        "num_ctx": num_ctx,
    }


def resolve_record_paths(
    global_config_path: str,
    *,
    entity: str | None = None,
    input_path: str | None = None,
    output_dir: str | None = None,
    model: str | None = None,
    max_words: int | None = None,
    max_tokens: int | None = None,
) -> Dict[str, Any]:
    """
    Resolve configuration, model selection, and paths for records extraction.

    Records may define a dedicated model through ``records_model`` or
    ``default_records_model``. Existing summary model settings remain valid
    fallbacks while all entity-backed outputs share one model lineage workspace.

    :param global_config_path: Path to the global ``yatsee.toml`` file
    :param entity: Optional entity handle
    :param input_path: Optional normalized transcript file or directory
    :param output_dir: Optional records output directory override
    :param model: Optional records model override
    :param max_words: Optional approximate word threshold per source chunk
    :param max_tokens: Optional approximate token threshold per source chunk
    :return: Dictionary of resolved records-stage settings
    :raises ValidationError: If required configuration or inputs are invalid
    """
    global_cfg = load_global_config(global_config_path)
    entity_cfg = load_entity_config(global_cfg, entity) if entity else {}

    if not entity and not input_path:
        raise ValidationError("Without --entity, --txt-input must be defined")
    if entity and not entity_cfg.get("data_path"):
        raise ValidationError("No entity data_path configured")

    resolved_model, model_key, model_cfg = resolve_records_model(
        global_cfg,
        entity_cfg,
        model,
    )

    if not entity and not output_dir:
        raise ValidationError("Without --entity, --output-dir must be defined")

    intel_layout = (
        resolve_intel_layout(entity_cfg["data_path"], model_key, model_cfg)
        if entity
        else None
    )
    paths = resolve_transcript_stage_paths(
        entity=entity,
        entity_cfg=entity_cfg,
        input_path=input_path,
        output_dir=output_dir,
        default_output_dir=(
            intel_layout["records_dir"] if intel_layout else None
        ),
    )
    if not paths["file_list"]:
        raise ValidationError(
            f"No transcript files found in: {paths['input_path']}"
        )

    if max_words and not max_tokens:
        resolved_max_tokens = int(max_words / 0.75)
    else:
        resolved_max_tokens = (
            max_tokens
            or model_cfg.get("records_max_tokens")
            or model_cfg.get("max_tokens", 2500)
        )

    num_ctx = model_cfg.get("num_ctx", 8192)
    if resolved_max_tokens >= num_ctx:
        raise ValidationError(
            f"Chunk size ({resolved_max_tokens}) exceeds or collides with "
            f"model context ({num_ctx})"
        )

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "model": resolved_model,
        "model_key": model_key,
        "model_cfg": model_cfg,
        "input_dir": paths["input_path"],
        "file_list": paths["file_list"],
        "output_dir": paths["output_dir"],
        "intel_dir": intel_layout["intel_dir"] if intel_layout else None,
        "max_tokens": resolved_max_tokens,
        "num_ctx": num_ctx,
    }


def resolve_audit_paths(
    global_config_path: str,
    *,
    entity: str,
    model: str | None = None,
    output_dir: str | None = None,
) -> Dict[str, Any]:
    """
    Resolve an entity's model-specific records root and audit output directory.

    Audit intentionally does not discover normalized transcripts or create the
    records root. It inspects existing artifacts and writes reports beneath a
    sibling ``audit`` directory beneath the shared model workspace unless the
    operator supplies an explicit output.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional configured records model override
    :param output_dir: Optional audit report directory override
    :return: Resolved configuration and audit paths
    :raises ValidationError: If entity or records configuration is invalid
    """
    global_cfg = load_global_config(global_config_path)
    entity_cfg = load_entity_config(global_cfg, entity)
    data_path = entity_cfg.get("data_path")
    if not data_path:
        raise ValidationError(f"Entity '{entity}' does not define data_path")

    resolved_model, model_key, model_cfg = resolve_records_model(
        global_cfg,
        entity_cfg,
        model,
    )
    intel_layout = resolve_intel_layout(data_path, model_key, model_cfg)
    records_dir = intel_layout["records_dir"]
    resolved_output_dir = os.path.abspath(
        output_dir or intel_layout["audit_dir"]
    )

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "model": resolved_model,
        "model_key": model_key,
        "model_cfg": model_cfg,
        "intel_dir": intel_layout["intel_dir"],
        "records_dir": records_dir,
        "output_dir": resolved_output_dir,
    }


def resolve_event_paths(
    global_config_path: str,
    *,
    entity: str,
    model: str | None = None,
    output_dir: str | None = None,
) -> Dict[str, Any]:
    """
    Resolve model-specific records input and deterministic events output paths.

    Events are derived from records rather than normalized transcripts. The
    default output is the ``events`` child beneath the shared model workspace so
    source records remain immutable and independently reviewable.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional configured records model override
    :param output_dir: Optional events output directory override
    :return: Resolved configuration, records input, and events output paths
    :raises ValidationError: If entity or records configuration is invalid
    """
    global_cfg = load_global_config(global_config_path)
    entity_cfg = load_entity_config(global_cfg, entity)
    data_path = entity_cfg.get("data_path")
    if not data_path:
        raise ValidationError(f"Entity '{entity}' does not define data_path")

    resolved_model, model_key, model_cfg = resolve_records_model(
        global_cfg,
        entity_cfg,
        model,
    )
    intel_layout = resolve_intel_layout(data_path, model_key, model_cfg)
    records_dir = intel_layout["records_dir"]
    resolved_output_dir = os.path.abspath(
        output_dir or intel_layout["events_dir"]
    )

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "model": resolved_model,
        "model_key": model_key,
        "model_cfg": model_cfg,
        "intel_dir": intel_layout["intel_dir"],
        "records_dir": records_dir,
        "output_dir": resolved_output_dir,
    }
