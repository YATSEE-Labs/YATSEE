"""
Shared deterministic formatting helpers for YATSEE artifacts.

These helpers contain presentation-only transformations that are reused across
multiple artifact families. They do not perform filesystem, provider, or model
operations.
"""

from __future__ import annotations

import html
from typing import Any


def format_timestamp(seconds: float) -> str:
    """
    Format elapsed seconds as ``MM:SS`` or ``HH:MM:SS``.

    Negative values are clamped to zero so malformed source metadata cannot
    produce a negative display timestamp.

    :param seconds: Elapsed timestamp in seconds
    :return: Human-readable timestamp
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def format_markdown_inline(value: Any) -> str:
    """
    Escape untrusted text for one inline Markdown field.

    Generated summaries, subjects, attributions, and evidence may contain
    Markdown control characters or raw HTML. Collapse embedded whitespace and
    escape those controls so downstream renderers display the source text rather
    than interpreting it as links, emphasis, code, or HTML.

    :param value: Value to render as inline Markdown
    :return: Markdown-safe single-line text
    """
    collapsed = " ".join(str(value if value is not None else "").split())
    escaped = html.escape(collapsed, quote=False).replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped
