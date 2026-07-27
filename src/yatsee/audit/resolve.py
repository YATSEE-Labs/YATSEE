"""
Configuration and path resolution for top-level YATSEE audit stages.

Audit reports live beneath ``data/<entity>/audit/<stage>/`` so review artifacts
remain separate from the pipeline artifact trees they inspect.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from yatsee.core.config import load_entity_config, load_global_config
from yatsee.core.errors import ValidationError
from yatsee.core.paths import resolve_contained_path, sanitize_path_component
from yatsee.intel.layout import model_directory_name, resolve_intel_layout
from yatsee.intel.resolve import resolve_records_model

AUDIT_DIRECTORY_NAME = "audit"


def resolve_records_audit_paths(
    global_config_path: str,
    *,
    entity: str,
    model: str | None = None,
    output_dir: str | None = None,
) -> Dict[str, Any]:
    """
    Resolve Records input and top-level Records audit output paths.

    Records remain under the model-specific intelligence workspace. Reports are
    written beneath ``data/<entity>/audit/records/<model>/`` unless an explicit
    output directory is supplied.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional configured Records model override
    :param output_dir: Optional audit report directory override
    :return: Resolved configuration, Records input, and audit output paths
    :raises ValidationError: If entity or model configuration is invalid
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
    default_output_dir = resolve_contained_path(
        data_path,
        AUDIT_DIRECTORY_NAME,
        "records",
        model_directory_name(model_key),
    )

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "model": resolved_model,
        "model_key": model_key,
        "model_cfg": model_cfg,
        "records_dir": intel_layout["records_dir"],
        "output_dir": os.path.abspath(output_dir or default_output_dir),
    }


def resolve_transcript_audit_paths(
    global_config_path: str,
    *,
    entity: str,
    model: str | None = None,
    input_path: str | None = None,
    output_dir: str | None = None,
) -> Dict[str, Any]:
    """
    Resolve transcript VTT input and top-level transcript audit output paths.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional transcription model suffix override
    :param input_path: Optional VTT file or directory override
    :param output_dir: Optional audit report directory override
    :return: Resolved configuration, VTT input, and audit output paths
    :raises ValidationError: If entity configuration is invalid
    """
    global_cfg = load_global_config(global_config_path)
    entity_cfg = load_entity_config(global_cfg, entity)
    data_path = entity_cfg.get("data_path")
    if not data_path:
        raise ValidationError(f"Entity '{entity}' does not define data_path")

    resolved_model = (
        model
        or entity_cfg.get("transcription_model")
        or global_cfg.get("system", {}).get("default_transcription_model", "medium")
    )
    safe_model = sanitize_path_component(str(resolved_model).replace(":", "_"))
    default_input_path = resolve_contained_path(
        data_path,
        f"transcripts_{resolved_model}",
    )
    default_output_dir = resolve_contained_path(
        data_path,
        AUDIT_DIRECTORY_NAME,
        "transcript",
        safe_model,
    )

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "model": str(resolved_model),
        "data_path": os.path.abspath(data_path),
        "input_path": os.path.abspath(input_path or default_input_path),
        "output_dir": os.path.abspath(output_dir or default_output_dir),
    }
