"""
Runtime helpers for optional ML/GPU-backed stages.

The functions in this module centralize PyTorch device selection and cache
cleanup without making the root CLI import heavy optional dependencies.
"""

from __future__ import annotations

import gc
from typing import Any


def import_torch() -> Any:
    """
    Import PyTorch only when a stage actually needs it.

    :return: Imported torch module
    :raises RuntimeError: If PyTorch is unavailable
    """
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch is required for this operation. Install the relevant YATSEE optional extra."
        ) from exc
    return torch


def clear_torch_cache() -> None:
    """
    Clear CUDA cache when PyTorch and CUDA are available.

    :return: None
    """
    torch = import_torch()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        gc.collect()


def resolve_torch_device(device_arg: str, allow_mps: bool = True) -> str:
    """
    Resolve a PyTorch execution device from a user-facing CLI value.

    :param device_arg: Requested device: auto, cuda, cpu, or mps
    :param allow_mps: Whether the caller supports Apple's MPS backend
    :return: Resolved device name
    """
    torch = import_torch()

    if torch.cuda.is_available() and device_arg in {"auto", "cuda"}:
        clear_torch_cache()
        return "cuda"

    mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
    if allow_mps and mps_backend and mps_backend.is_available() and device_arg in {"auto", "mps"}:
        return "mps"

    return "cpu"
