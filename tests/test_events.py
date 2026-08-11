"""Tests for :mod:`rohrpost.events`."""

from __future__ import annotations

import msgspec
import pytest

from rohrpost.events import Event, Op, decode_line, encode


def _sample(
    *,
    op: Op = "set",
    actor: str = "runner/claude-code@b-3",
    set_payload: dict[str, object] | None = None,
    text: str | None = None,
    reason: str | None = None,
) -> Event:
    return Event(
        id="01K2X8P4RQ7YFZ3M9NVB6TDHWC",
        ts="2026-08-11T09:20:14.221Z",
        ticket="RP-a1b2c3",
        op=op,
        actor=actor,
        set={"status": "in_progress"} if set_payload is None else set_payload,
        text=text,
        reason=reason,
    )


def _poke_attr(obj: object, name: str, value: object) -> None:
    """Mutate through an opaque reference so the (intentionally invalid) write to
    a frozen field is not flagged by the static type checkers."""
    setattr(obj, name, value)


def test_encode_decode_round_trips() -> None:
    event = _sample()
    line = encode(event)
    assert decode_line(line) == event


def test_encoded_line_is_compact_jsonl() -> None:
    line = encode(_sample())
    assert isinstance(line, bytes)
    assert line.startswith(b"{")
    assert b"\n" not in line


def test_omit_defaults_drops_absent_payloads() -> None:
    # A `comment` event carries only `text`. Built directly (no `set` payload)
    # so msgspec's omit_defaults is the thing under test.
    event = Event(
        id="01K2X8P4RQ7YFZ3M9NVB6TDHWC",
        ts="2026-08-11T09:20:14.221Z",
        ticket="RP-a1b2c3",
        op="comment",
        actor="runner/claude-code@b-3",
        text="retried with backoff",
    )
    line = encode(event)
    assert b'"text"' in line
    assert b'"set"' not in line
    assert b'"remote"' not in line


def test_event_is_frozen() -> None:
    event = _sample()
    with pytest.raises(AttributeError):
        _poke_attr(event, "actor", "user/x")


def test_decode_rejects_unknown_op() -> None:
    line = b'{"id":"x","ts":"t","ticket":"RP-a1b2c3","op":"nuke","actor":"a"}'
    with pytest.raises(msgspec.MsgspecError):
        decode_line(line)


def test_decode_rejects_malformed_json() -> None:
    with pytest.raises(msgspec.MsgspecError):
        decode_line("not json at all")


def test_decode_rejects_missing_required_field() -> None:
    # No `actor`.
    line = b'{"id":"x","ts":"t","ticket":"RP-a1b2c3","op":"set"}'
    with pytest.raises(msgspec.MsgspecError):
        decode_line(line)


def test_close_reason_rides_on_the_event() -> None:
    # Spec §5.2: close reasons live on the event, not the ticket.
    event = _sample(op="set", set_payload={"status": "done"}, reason="implemented with backoff")
    line = encode(event)
    assert b'"reason"' in line
    assert decode_line(line).reason == "implemented with backoff"


def test_actor_namespaces_round_trip() -> None:
    for actor in ("user/vinzenz@example.com", "runner/claude-code@b-3", "remote/jira"):
        event = _sample(actor=actor)
        assert decode_line(encode(event)).actor == actor
