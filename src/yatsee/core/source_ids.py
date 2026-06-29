"""
Source identifier helpers for YATSEE artifacts.

These helpers preserve compatibility with older YouTube-backed trackers while
using source-neutral names in the core pipeline.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import string
from typing import Dict, Tuple

_SOURCE_ID_RE = re.compile(r"([A-Za-z0-9_-]{11})")


def generate_placeholder_source_id(base_name: str | None = None) -> str:
    """
    Generate an 11-character placeholder source ID.

    A deterministic ID is generated when a basename is supplied so reruns remain
    stable even when no canonical source ID is available.

    :param base_name: Optional seed string
    :return: 11-character source identifier
    """
    if base_name:
        digest = hashlib.sha256(base_name.encode("utf-8")).hexdigest()
        return "".join(char for char in digest if char.isalnum())[:11]

    return "".join(random.choices(string.ascii_letters + string.digits, k=11))


def load_source_id_map(id_map_path: str) -> Dict[str, str]:
    """
    Load canonical source IDs from a tracker file.

    This supports the existing downloads/.downloaded shape where each line may
    contain an 11-character source identifier.

    :param id_map_path: Path to a source tracker file
    :return: Mapping from lowercase source IDs to canonical source IDs
    """
    id_map: Dict[str, str] = {}

    if not id_map_path or not os.path.exists(id_map_path):
        return id_map

    with open(id_map_path, "r", encoding="utf-8") as handle:
        for line in handle:
            source_id = line.strip()
            if source_id and len(source_id) == 11:
                id_map[source_id.lower()] = source_id

    return id_map


def resolve_source_id(base_name: str, source_id_map: Dict[str, str]) -> Tuple[str, bool]:
    """
    Resolve a canonical source ID for an artifact basename.

    :param base_name: Artifact basename
    :param source_id_map: Mapping loaded from a source tracker
    :return: Tuple of (source_id, used_placeholder)
    """
    match = _SOURCE_ID_RE.match(base_name)
    source_id = source_id_map.get(match.group(1).lower()) if match else None
    if source_id:
        return source_id, False

    return generate_placeholder_source_id(base_name), True


# Compatibility aliases for older code and artifact terminology.
generate_placeholder_id = generate_placeholder_source_id
load_video_id_map = load_source_id_map
