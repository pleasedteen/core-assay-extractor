"""Shared error types for run lifecycle conflicts.

A *conflict* is different from a bad request: the run id already exists and is
bound to another intent (another operation kind, rule text or page manifest),
or it protects an already confirmed baseline.  The web layer maps these to
HTTP ``409 Conflict`` rather than ``400``.
"""
from __future__ import annotations


class RunConflictError(RuntimeError):
    """Raised when reusing a run id with a different intent or protected state."""
