"""
Hashing helpers for YATSEE.

This module centralizes the file hashing used by multiple pipeline stages
for idempotency and change tracking.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from yatsee.core.errors import ConfigError


def compute_sha256(path: str) -> str:
    """
    Compute the SHA-256 hash of a file.

    :param path: Path to the file
    :return: Hex-encoded SHA-256 hash string
    :raises ConfigError: If the file cannot be read
    """
    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError as exc:
        raise ConfigError(f"Failed to hash file: {path}") from exc


def compute_text_sha256(text: str) -> str:
    """
    Compute the SHA-256 hash of UTF-8 text.

    This is used when the exact in-memory content consumed by a pipeline stage
    is the cache identity, rather than filesystem metadata or a path string.

    :param text: Text content to hash
    :return: Hex-encoded SHA-256 hash string
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_json_sha256(value: Any) -> str:
    """
    Compute a stable SHA-256 hash for a JSON-compatible value.

    Canonical serialization prevents dictionary insertion order and formatting
    choices from changing an artifact fingerprint.

    :param value: JSON-compatible value to hash
    :return: Hex-encoded SHA-256 hash string
    :raises TypeError: If the value cannot be serialized as JSON
    """
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return compute_text_sha256(payload)