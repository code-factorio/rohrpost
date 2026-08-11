"""Identifiers: ticket ids and event ULIDs.

Section 13.1 of the spec names the event log as the single load-bearing
decision, and events are keyed by ids, so this module is foundational. Two
distinct schemes live here:

* **Ticket ids** — 6 lowercase Crockford base32 characters drawn from 30 random
  bits. The collision domain is one repository, so ~2**30 (~1 billion) values is
  comfortable, and random suffixes need no central allocation authority. The
  prefix (e.g. ``FAC``) is display-only and never enters the log; ``rp`` accepts
  either the bare id ``a1b2c3`` or the rendered ``FAC-a1b2c3``.

* **Event ids** — ULIDs: 26 Crockford-base32 characters encoding a 48-bit
  millisecond timestamp and 80 bits of randomness. Lexicographically sortable by
  creation time, which gives the fold a stable, deterministic tiebreak.

Everything is built from the stdlib (``secrets`` for entropy, ``time`` for the
ULID clock) — no dependency, deliberately, per the spec.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Final

from rohrpost.exceptions import IdError

#: Crockford base32 alphabet, lowercased (excludes i/l/o/u). Ticket ids are drawn from this.
_TICKET_ALPHABET: Final[str] = "0123456789abcdefghjkmnpqrstvwxyz"
#: Crockford base32 alphabet (excludes I, L, O, U to avoid look-alikes). ULIDs use this.
_CROCKFORD_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TICKET_LENGTH: Final[int] = 6
_TICKET_BITS: Final[int] = _TICKET_LENGTH * 5  # 30 bits -> ~1e9 collision domain
_ULID_LENGTH: Final[int] = 26
_TIMESTAMP_BITS: Final[int] = 48
_RANDOMNESS_BITS: Final[int] = 80

_TICKET_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-hjkmnp-tv-z]{6}\Z")
_ULID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")

_BASE32_MASK: Final[int] = 0x1F  # 5 bits


def _encode_base32(value: int, length: int, alphabet: str) -> str:
    """Encode ``value`` as ``length`` base32 chars, most-significant group first."""
    return "".join(alphabet[(value >> (5 * i)) & _BASE32_MASK] for i in reversed(range(length)))


def new_ticket_id() -> str:
    """Return a fresh 6-char lowercase base32 ticket id (e.g. ``a1b2c3``)."""
    return _encode_base32(secrets.randbits(_TICKET_BITS), _TICKET_LENGTH, _TICKET_ALPHABET)


def is_valid_ticket_id(value: str) -> bool:
    """True if ``value`` is a bare 6-char lowercase base32 ticket id."""
    return _TICKET_RE.match(value) is not None


def new_ulid(*, timestamp_ms: int | None = None) -> str:
    """Return a fresh 26-char Crockford-base32 ULID, time-ordered.

    The timestamp defaults to the current wall clock in milliseconds. Pass an
    explicit ``timestamp_ms`` for deterministic output in tests.
    """
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    if not 0 <= timestamp_ms < (1 << _TIMESTAMP_BITS):
        raise IdError(f"timestamp out of range for a 48-bit ULID: {timestamp_ms}")
    value = (timestamp_ms << _RANDOMNESS_BITS) | secrets.randbits(_RANDOMNESS_BITS)
    return _encode_base32(value, _ULID_LENGTH, _CROCKFORD_ALPHABET)


def is_valid_ulid(value: str) -> bool:
    """True if ``value`` is a well-formed 26-char Crockford-base32 ULID."""
    return _ULID_RE.match(value) is not None


def render_id(prefix: str, ticket_id: str) -> str:
    """Render a bare ticket id with its project prefix, e.g. ``FAC-a1b2c3``.

    The prefix is display-only; it never enters the log.
    """
    if not prefix:
        raise IdError("prefix must be non-empty")
    if not is_valid_ticket_id(ticket_id):
        raise IdError(f"not a valid ticket id: {ticket_id!r}")
    return f"{prefix}-{ticket_id}"


def normalize_id(value: str) -> str:
    """Return the bare ticket id from either ``a1b2c3`` or ``PREFIX-a1b2c3``.

    Agents routinely drop the prefix; humans often type it. Both resolve to the
    same canonical bare id. Raises :class:`IdError` if the id portion is invalid.
    """
    candidate = value.rsplit("-", 1)[-1] if "-" in value else value
    if not is_valid_ticket_id(candidate):
        raise IdError(f"not a valid ticket id: {value!r}")
    return candidate
