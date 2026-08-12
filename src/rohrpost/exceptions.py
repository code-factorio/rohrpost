"""Domain errors raised by rohrpost."""

from __future__ import annotations


class RohrpostError(Exception):
    """Base class for all errors raised by rohrpost."""


class IdError(RohrpostError):
    """Raised when a ticket or event id is malformed or out of range."""


class StoreError(RohrpostError):
    """Raised on log/store integrity failures (parse errors, lock contention, ...)."""


class ConfigError(RohrpostError):
    """Raised when ``config.toml`` is missing, malformed or has invalid values."""


class TicketError(RohrpostError):
    """Raised for ticket-level problems: not found, bad status, cycle, etc."""


class TicketNotFoundError(TicketError):
    """Raised when a referenced ticket id does not exist in the folded log."""


class RemoteItemNotFoundError(RohrpostError):
    """Raised when a linked item has been deleted from its remote tracker."""
