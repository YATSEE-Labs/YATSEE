"""
Reusable provider, pricing, and tokenization helpers for YATSEE.

Imports are intentionally lazy so the root CLI and security validators do not
require HTTP/provider optional dependencies until a provider is actually used.
"""

from __future__ import annotations

from typing import Any


def build_pricing_summary(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """
    Build a structured pricing summary using the pricing helper module.

    :param args: Positional arguments forwarded to the helper
    :param kwargs: Keyword arguments forwarded to the helper
    :return: Pricing summary dictionary
    """
    from .pricing import build_pricing_summary as _build_pricing_summary

    return _build_pricing_summary(*args, **kwargs)


def estimate_cost(*args: Any, **kwargs: Any) -> float | None:
    """
    Estimate provider/model cost using the pricing helper module.

    :param args: Positional arguments forwarded to the helper
    :param kwargs: Keyword arguments forwarded to the helper
    :return: Estimated cost or None
    """
    from .pricing import estimate_cost as _estimate_cost

    return _estimate_cost(*args, **kwargs)


def get_pricing(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    """
    Resolve pricing metadata using the pricing helper module.

    :param args: Positional arguments forwarded to the helper
    :param kwargs: Keyword arguments forwarded to the helper
    :return: Pricing metadata or None
    """
    from .pricing import get_pricing as _get_pricing

    return _get_pricing(*args, **kwargs)


def get_provider(*args: Any, **kwargs: Any) -> Any:
    """
    Resolve a provider adapter using the provider registry.

    :param args: Positional arguments forwarded to the registry
    :param kwargs: Keyword arguments forwarded to the registry
    :return: Provider adapter module
    """
    from .registry import get_provider as _get_provider

    return _get_provider(*args, **kwargs)


__all__ = [
    "build_pricing_summary",
    "estimate_cost",
    "get_pricing",
    "get_provider",
]
