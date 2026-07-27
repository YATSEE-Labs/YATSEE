"""
Deterministic records audit package.

The public import path is ``yatsee.audit.records``. Implementation is split by
responsibility for maintainability and testability.
"""

from .policy import (
    AUDIT_PROFILE_BALANCED,
    AUDIT_PROFILE_STRICT,
    REBUILD_MODE_AUTO,
    REBUILD_MODE_PLAN,
)
from .runner import run_records_audit

__all__ = [
    "AUDIT_PROFILE_BALANCED",
    "AUDIT_PROFILE_STRICT",
    "REBUILD_MODE_AUTO",
    "REBUILD_MODE_PLAN",
    "run_records_audit",
]
