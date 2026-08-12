"""Event log primitives: the append-only envelope that is the source of truth.

Section 13.1 of the spec names the event log as the single load-bearing
decision — everything else is derived. This module defines the shape of an
event and the JSONL codec it travels through.

``msgspec`` gives struct types with schema validation plus line-oriented
(de)serialisation that is substantially faster than pydantic for this workload.
Events are frozen value objects: once written they are never mutated, which keeps
the fold deterministic.
"""

from __future__ import annotations

from typing import Any, Literal

import msgspec

#: The closed set of operations an event can record. See spec §5.2.
type Op = Literal["create", "set", "comment", "link", "unlink", "synced"]

# ``synced`` is a remote-level watermark rather than a ticket mutation. The
# envelope still requires a ticket string, so this reserved value keeps the
# event schema strict while fold ignores the watermark.
SYNC_TICKET: str = "__sync__"


class Event(msgspec.Struct, kw_only=True, omit_defaults=True, frozen=True):
    """One line in ``log.jsonl`` — an immutable, append-only mutation record.

    The five required fields (``id``, ``ts``, ``ticket``, ``op``, ``actor``) are
    the load-bearing envelope every event carries. Op-dependent payloads (``set``
    for the workhorse field update, ``text`` for comments, ``remote``/``ref`` for
    links, ``at`` for sync watermarks, ``reason`` for close reasons) are optional
    and omitted from the encoded line when absent.
    """

    id: str
    ts: str
    ticket: str
    op: Op
    actor: str
    # Op-dependent payloads — present only when the op needs them.
    set: dict[str, Any] | None = None
    text: str | None = None
    remote: str | None = None
    ref: str | None = None
    at: str | None = None
    reason: str | None = None


_ENCODER: msgspec.json.Encoder = msgspec.json.Encoder()
_DECODER: msgspec.json.Decoder[Event] = msgspec.json.Decoder(Event)


def encode(event: Event) -> bytes:
    """Serialise an event to a single JSONL line (no trailing newline)."""
    return _ENCODER.encode(event)


def decode_line(line: str | bytes) -> Event:
    """Decode one JSONL line into an :class:`Event`.

    Raises :class:`msgspec.MsgspecError` (``DecodeError`` for invalid JSON,
    ``ValidationError`` for a schema mismatch such as an unknown ``op``).
    """
    return _DECODER.decode(line)
