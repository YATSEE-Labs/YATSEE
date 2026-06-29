"""
Merged runtime configuration inspection for YATSEE.

This module exposes the resolved runtime view that later CLI commands and
pipeline stages will consume.
"""

from __future__ import annotations

from typing import Any, Dict

from yatsee.core.config import resolve_runtime_config

SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def redact_config(value: Any) -> Any:
    """
    Recursively redact sensitive-looking configuration values.

    Redaction is intentionally key-name based so future token/secret settings
    are protected by default when operators inspect resolved runtime config.

    :param value: Configuration value to redact
    :return: Redacted configuration value
    """
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SENSITIVE_KEY_PARTS) and not key_text.endswith("_env"):
                redacted[key] = "***REDACTED***" if item else item
            else:
                redacted[key] = redact_config(item)
        return redacted

    if isinstance(value, list):
        return [redact_config(item) for item in value]

    return value


def resolve_config(
    global_config_path: str,
    entity: str | None = None,
    redact: bool = True,
) -> Dict[str, Any]:
    """
    Resolve the effective runtime configuration.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Optional entity handle
    :param redact: Redact sensitive values before returning
    :return: Resolved configuration dictionary
    """
    resolved = resolve_runtime_config(global_config_path=global_config_path, entity=entity)
    if redact:
        return redact_config(resolved)
    return resolved
