"""Tests for :mod:`rohrpost.ids`."""

from __future__ import annotations

import re
import time

import pytest

from rohrpost.exceptions import IdError
from rohrpost.ids import (
    _CROCKFORD_ALPHABET,
    _TICKET_ALPHABET,
    is_valid_ticket_id,
    is_valid_ulid,
    new_ticket_id,
    new_ulid,
    normalize_id,
    render_id,
)

_TICKET_ALPHABET_SET = set(_TICKET_ALPHABET)
_CROCKFORD_ALPHABET_SET = set(_CROCKFORD_ALPHABET)


# ---------------------------------------------------------------------------
# Alphabet sanity — guards against the class of bug where the generation
# alphabet and the validation regex drift out of sync.
# ---------------------------------------------------------------------------
def test_ticket_alphabet_is_32_unique_crockford_chars() -> None:
    assert len(_TICKET_ALPHABET_SET) == 32
    assert all(re.match(r"[0-9a-hjkmnp-tv-z]\Z", c) for c in _TICKET_ALPHABET)
    # digits 0-9 present; the look-alike letters i/l/o/u excluded
    assert {c for c in _TICKET_ALPHABET if c.isdigit()} == set("0123456789")
    for excluded in "ilou":
        assert excluded not in _TICKET_ALPHABET_SET


def test_crockford_alphabet_is_32_unique_chars_excluding_look_alikes() -> None:
    assert len(_CROCKFORD_ALPHABET_SET) == 32
    assert all(re.match(r"[0-9A-HJKMNP-TV-Z]\Z", c) for c in _CROCKFORD_ALPHABET)
    for excluded in "ILOU":
        assert excluded not in _CROCKFORD_ALPHABET_SET


# ---------------------------------------------------------------------------
# Ticket ids
# ---------------------------------------------------------------------------
def test_ticket_id_shape() -> None:
    ticket_id = new_ticket_id()
    assert len(ticket_id) == 6
    assert is_valid_ticket_id(ticket_id)


def test_ticket_ids_are_drawn_from_the_full_alphabet() -> None:
    # Every alphabet character should appear at least once across many draws —
    # fails if the generation alphabet is truncated relative to the validation
    # regex (e.g. a missing digit).
    seen: set[str] = set()
    for _ in range(2000):
        seen |= set(new_ticket_id())
    assert seen == _TICKET_ALPHABET_SET


def test_ticket_ids_do_not_collide_at_scale() -> None:
    # 30 bits of entropy (~1e9 space); at 512 samples the birthday-bound chance
    # of any collision is ~1e-4, so zero collisions is the expected, stable
    # outcome. Keeps the deterministic gate flake-free.
    ids = [new_ticket_id() for _ in range(512)]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "a1b2c",  # too short
        "a1b2c3d",  # too long
        "A1B2C3",  # uppercase (the alphabet is lowercased)
        "a1b2ci",  # i is excluded (looks like 1)
        "a1b2cl",  # l is excluded (looks like 1)
        "a1b2co",  # o is excluded (looks like 0)
        "a1b2cu",  # u is excluded (looks like v)
        "a1b2c-",  # stray separator
    ],
)
def test_invalid_ticket_ids_rejected(value: str) -> None:
    assert not is_valid_ticket_id(value)


# ---------------------------------------------------------------------------
# ULIDs
# ---------------------------------------------------------------------------
def test_ulid_shape() -> None:
    ulid = new_ulid()
    assert len(ulid) == 26
    assert is_valid_ulid(ulid)


def test_ulids_with_increasing_timestamps_are_lexicographically_ordered() -> None:
    a = new_ulid(timestamp_ms=1_700_000_000_000)
    b = new_ulid(timestamp_ms=1_700_000_001_000)
    c = new_ulid(timestamp_ms=1_700_000_002_000)
    assert a < b < c


def test_ulids_same_timestamp_differ_in_randomness() -> None:
    ts = 1_700_000_000_000
    ids = {new_ulid(timestamp_ms=ts) for _ in range(1000)}
    assert len(ids) == 1000


def test_ulid_first_char_is_in_timestamp_range() -> None:
    # 48-bit timestamp encoded MSB-first into 26 base32 chars: the first char
    # carries only the top 3 bits, so its value is 0-7.
    first_chars = {new_ulid(timestamp_ms=0)[0] for _ in range(200)}
    assert first_chars == {"0"}


@pytest.mark.parametrize("ts", [-1, 1 << 48, (1 << 48) + 1])
def test_ulid_rejects_out_of_range_timestamps(ts: int) -> None:
    with pytest.raises(IdError):
        new_ulid(timestamp_ms=ts)


def test_ulid_uses_real_clock_by_default() -> None:
    before = time.time_ns() // 1_000_000
    ulid = new_ulid()
    after = time.time_ns() // 1_000_000
    # The timestamp occupies the first 10 chars; decode them back to ms.
    decoded_ts = _decode_ulid_timestamp_ms(ulid)
    assert before <= decoded_ts <= after


def _decode_ulid_timestamp_ms(ulid: str) -> int:
    """Best-effort decode of the 48-bit timestamp from a ULID for the clock test."""
    crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    lookup = {c: i for i, c in enumerate(crockford)}
    value = 0
    for ch in ulid:
        value = (value << 5) | lookup[ch]
    # 26 chars * 5 bits = 130 bits; the top 2 bits are zero padding above the
    # 128-bit ULID, whose top 48 bits are the timestamp. Shift away the 80
    # randomness bits (the padding bits are zero, so they need no masking).
    return value >> 80


@pytest.mark.parametrize(
    "value",
    [
        "",
        "01K2X8P4RQ7YFZ3M9NVB6TDHW",  # 25 chars
        "01K2X8P4RQ7YFZ3M9NVB6TDHWCX",  # 27 chars
        "01K2X8P4RQ7YFZ3M9NVB6TDHWI",  # contains I (excluded)
        "01K2X8P4RQ7YFZ3M9NVB6TDHWL",  # contains L (excluded)
        "01K2X8P4RQ7YFZ3M9NVB6TDHWO",  # contains O (excluded)
        "01K2X8P4RQ7YFZ3M9NVB6TDHWU",  # contains U (excluded)
        "01k2x8p4rq7yfz3m9nvb6tdhwc",  # lowercase
    ],
)
def test_invalid_ulids_rejected(value: str) -> None:
    assert not is_valid_ulid(value)


# ---------------------------------------------------------------------------
# Prefix rendering / normalisation
# ---------------------------------------------------------------------------
def test_render_id_and_normalize_round_trip() -> None:
    ticket_id = new_ticket_id()
    rendered = render_id("FAC", ticket_id)
    assert rendered == f"FAC-{ticket_id}"
    assert normalize_id(rendered) == ticket_id


def test_normalize_accepts_bare_and_prefixed_forms() -> None:
    assert normalize_id("a1b2c3") == "a1b2c3"
    assert normalize_id("FAC-a1b2c3") == "a1b2c3"


@pytest.mark.parametrize("value", ["FAC-notvalid", "a1b2c3d", "-", "FAC-"])
def test_normalize_rejects_invalid_id_portion(value: str) -> None:
    with pytest.raises(IdError):
        normalize_id(value)


def test_render_rejects_empty_prefix_and_invalid_id() -> None:
    with pytest.raises(IdError):
        render_id("", "a1b2c3")
    with pytest.raises(IdError):
        render_id("FAC", "notvalid")
