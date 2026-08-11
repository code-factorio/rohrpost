"""Domain errors raised by rohrpost."""

from __future__ import annotations


class RohrpostError(Exception):
    """Base class for all errors raised by rohrpost."""


class IdError(RohrpostError):
    """Raised when a ticket or event id is malformed or out of range."""


class StoreError(RohrpostError):
    """Raised on log/store integrity failures (parse errors, lock contention, ...)."""
