"""
Path helpers for YATSEE.

This module centralizes filesystem path construction so CLI commands and later
pipeline stages do not each re-implement path logic.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

from yatsee.core.errors import ValidationError

_ENTITY_HANDLE_RE = re.compile(r"[a-z0-9_]+")


def validate_entity_handle(entity: str) -> str:
    """
    Validate an entity handle against the project naming rules.

    Entity handles are used as registry keys and filesystem path components.
    Keeping them restricted prevents accidental path nesting, path traversal,
    and inconsistent naming across configuration and artifacts.

    :param entity: Proposed entity handle
    :return: Normalized entity handle
    :raises ValidationError: If the handle is empty or invalid
    """
    normalized = entity.strip() if entity else ""
    if not _ENTITY_HANDLE_RE.fullmatch(normalized):
        raise ValidationError(
            "Invalid entity handle. Only lowercase letters, numbers, and underscores are allowed."
        )
    return normalized


def sanitize_path_component(value: str) -> str:
    """
    Normalize a value into one safe filesystem path component.

    This helper is intended for artifact filenames and directory names derived
    from transcript metadata, configuration, or other untrusted text. It removes
    path separators and null bytes before callers combine the value with a
    trusted output root.

    :param value: Raw path component
    :return: Safe single path component
    :raises ValidationError: If the value is empty after normalization
    """
    normalized = (value or "").replace("\\", "/").strip().strip(".")
    normalized = normalized.replace("/", "_").replace("\x00", "")

    if not normalized or normalized in {".", ".."}:
        raise ValidationError("Invalid filesystem path component")

    return normalized


def resolve_contained_path(root: str | os.PathLike[str], *parts: str) -> str:
    """
    Resolve a child path and ensure it remains inside a trusted root directory.

    This helper is intended for entity storage, generated artifacts, and any
    future destructive operations. It catches accidental or malicious path
    traversal before callers create, write, or delete files.

    :param root: Trusted root directory
    :param parts: Child path components to append under the root
    :return: Absolute resolved child path
    :raises ValidationError: If the resolved child escapes the root
    """
    root_path = Path(root).expanduser().resolve()
    candidate = root_path.joinpath(*parts).expanduser().resolve()

    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise ValidationError(
            f"Resolved path '{candidate}' escapes trusted root '{root_path}'."
        ) from exc

    return str(candidate)


def serialize_project_path(
    path: str | os.PathLike[str],
    project_root: str | os.PathLike[str],
) -> str:
    """
    Serialize a filesystem path relative to the YATSEE project root.

    Generated artifact metadata should remain portable across workstations and
    repository locations. Paths outside the project root remain explicit via
    ``..`` components rather than being converted back to absolute paths.

    :param path: Filesystem path to serialize
    :param project_root: Directory containing the active ``yatsee.toml``
    :return: POSIX-style project-relative path
    """
    root_path = Path(project_root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    relative = os.path.relpath(candidate.resolve(), root_path)
    normalized = Path(relative).as_posix()
    if normalized == "." or normalized.startswith("."):
        return normalized
    return f"./{normalized}"


def get_root_data_dir(cfg: Dict[str, Any]) -> str:
    """
    Resolve the absolute root data directory from configuration.

    :param cfg: Configuration dictionary containing system settings
    :return: Absolute path to the root data directory
    """
    root = cfg.get("system", {}).get("root_data_dir", "./data")
    return os.path.abspath(root)


def get_entity_dir(cfg: Dict[str, Any], entity: str) -> str:
    """
    Resolve the absolute directory for a specific entity.

    :param cfg: Global configuration dictionary
    :param entity: Entity handle
    :return: Absolute path to the entity directory
    :raises ValidationError: If the entity handle or resolved path is invalid
    """
    entity = validate_entity_handle(entity)
    return resolve_contained_path(get_root_data_dir(cfg), entity)


def get_entity_config_path(cfg: Dict[str, Any], entity: str) -> str:
    """
    Resolve the absolute path to an entity's local config.toml file.

    :param cfg: Global configuration dictionary
    :param entity: Entity handle
    :return: Absolute path to the entity config.toml
    :raises ValidationError: If the entity handle or resolved path is invalid
    """
    return resolve_contained_path(get_entity_dir(cfg, entity), "config.toml")
