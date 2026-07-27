"""
Shared filesystem layout helpers for model-specific intelligence artifacts.

Entity-backed intelligence stages share one model lineage root named
``intel_<sanitized-model>``. Probabilistic records and summaries and
deterministic events remain separate artifact families beneath that root. Audit
reports live at the entity root.
"""

from __future__ import annotations

from typing import Any, Dict

from yatsee.core.paths import resolve_contained_path, sanitize_path_component

INTEL_DIRECTORY_PREFIX = "intel_"
INTEL_ARTIFACT_DIRECTORIES = {
    "summaries": "summaries",
    "records": "records",
    "events": "events",
}


def model_directory_name(model_key: str) -> str:
    """
    Convert a configured model key into one stable filesystem component.

    :param model_key: Exact model key from the global model configuration
    :return: Sanitized model directory component
    :raises ValidationError: If the model key cannot produce a safe component
    """
    return sanitize_path_component(model_key.replace(":", "_"))


def resolve_intel_layout(
    data_path: str,
    model_key: str,
    model_cfg: Dict[str, Any],
) -> Dict[str, str]:
    """
    Resolve the shared model lineage root and artifact-family directories.

    ``intel_append_dir`` may override the entity-relative workspace name. The
    resulting root and every artifact child are containment checked beneath the
    entity data directory.

    :param data_path: Trusted entity data directory
    :param model_key: Exact configured model key
    :param model_cfg: Model configuration dictionary
    :return: Resolved intelligence root and artifact-family directories
    :raises ValidationError: If a configured path escapes the entity directory
    """
    safe_model_name = model_directory_name(model_key)
    intel_dirname = model_cfg.get("intel_append_dir") or (
        f"{INTEL_DIRECTORY_PREFIX}{safe_model_name}"
    )
    intel_dir = resolve_contained_path(data_path, intel_dirname)

    resolved = {"intel_dir": intel_dir}
    for key, dirname in INTEL_ARTIFACT_DIRECTORIES.items():
        resolved[f"{key}_dir"] = resolve_contained_path(intel_dir, dirname)
    return resolved
