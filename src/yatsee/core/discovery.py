"""
Filesystem discovery helpers for YATSEE.

This module centralizes the common pattern of accepting either a directory
or a single file and returning a normalized list of supported files.
"""

from __future__ import annotations

import os
from typing import List, Tuple

from yatsee.core.errors import ValidationError


def discover_files(
    input_path: str,
    supported_exts: Tuple[str, ...],
    exclude_suffixes: Tuple[str, ...] = (),
) -> List[str]:
    """
    Collect supported files from a directory or a single file.

    Excluded suffixes are applied after extension matching. This allows stages
    to reuse generic discovery while skipping known derived sidecar files.

    :param input_path: Directory path or single file path
    :param supported_exts: Tuple of allowed file extensions
    :param exclude_suffixes: Optional suffixes to exclude from results
    :return: Sorted list of matching file paths
    :raises FileNotFoundError: If input_path does not exist
    :raises ValidationError: If a single file has an unsupported extension
    """
    files: List[str] = []

    if os.path.isdir(input_path):
        for entry in os.listdir(input_path):
            lowered = entry.lower()
            full = os.path.join(input_path, entry)

            if not os.path.isfile(full):
                continue

            if not lowered.endswith(supported_exts):
                continue

            if exclude_suffixes and lowered.endswith(exclude_suffixes):
                continue

            files.append(full)

        return sorted(files)

    if os.path.isfile(input_path):
        lowered = input_path.lower()

        if exclude_suffixes and lowered.endswith(exclude_suffixes):
            return []

        if lowered.endswith(supported_exts):
            return [input_path]

        raise ValidationError(f"Unsupported file extension: {os.path.splitext(input_path)[1]}")

    raise FileNotFoundError(f"Path not found: {input_path}")