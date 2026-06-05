"""Application error types shared across all scripts."""

from __future__ import annotations


class EmpowerError(Exception):
    """Base error for Empower API operations."""


class EmpowerLoginError(EmpowerError):
    """Raised when the automated Empower login flow fails."""


class BiltError(Exception):
    """Base error for Bilt API operations."""


class UnauthorizedError(BiltError):
    """Raised when a protected Bilt API endpoint returns 401."""
