"""
Stable schema constants shared by YATSEE intelligence artifacts.

This module remains dependency-light so deterministic inspection commands can
validate records without importing model providers or other optional runtime
dependencies.
"""

from __future__ import annotations

ALLOWED_RECORD_TYPES = frozenset(
    {
        "attendance",
        "procedural_action",
        "presentation",
        "discussion",
        "motion",
        "vote",
        "public_comment",
        "financial",
        "metric",
        "ordinance",
        "resolution",
        "contract",
        "project",
        "appointment",
        "proclamation",
        "recognition",
        "follow_up",
        "other",
    }
)
