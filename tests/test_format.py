"""
Tests for shared deterministic formatting helpers.
"""

from __future__ import annotations

from yatsee.core.format import format_markdown_inline, format_timestamp


def test_format_timestamp_handles_minutes_hours_and_negative_values() -> None:
    """
    Render stable elapsed timestamps without allowing negative displays.
    """
    assert format_timestamp(0) == "00:00"
    assert format_timestamp(65.9) == "01:05"
    assert format_timestamp(3661) == "01:01:01"
    assert format_timestamp(-10) == "00:00"


def test_format_markdown_inline_escapes_generated_content() -> None:
    """
    Render generated fields without activating Markdown or raw HTML.
    """
    value = "Use *literal* [text](https://example.invalid) <script>\nnext"
    assert format_markdown_inline(value) == (
        r"Use \*literal\* \[text\](https://example.invalid) "
        "&lt;script&gt; next"
    )
