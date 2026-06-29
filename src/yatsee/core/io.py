"""
Shared filesystem I/O helpers for YATSEE.

These helpers keep common read/write/directory behavior consistent while
preserving clean project-specific error messages at call sites.
"""

from __future__ import annotations

import os
from pathlib import Path

from yatsee.core.errors import ConfigError


def ensure_directory(path: str) -> str:
    """
    Create a directory if it does not already exist.

    :param path: Directory path to create
    :return: Absolute directory path
    :raises ConfigError: If the directory cannot be created
    """
    resolved = os.path.abspath(path)
    try:
        os.makedirs(resolved, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Failed to create directory '{resolved}': {exc}") from exc
    return resolved


def read_text(path: str) -> str:
    """
    Read a UTF-8 text file.

    :param path: Path to read
    :return: File contents
    :raises ConfigError: If the file cannot be read
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Failed to read text file '{path}': {exc}") from exc


def write_text(path: str, content: str) -> None:
    """
    Write UTF-8 text to a file, creating the parent directory when needed.

    :param path: Destination path
    :param content: Text content to write
    :return: None
    :raises ConfigError: If the file cannot be written
    """
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Failed to write text file '{path}': {exc}") from exc
