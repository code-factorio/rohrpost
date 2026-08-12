"""Hypothesis property tests for the fold's algebraic invariants (decision §3).

These guard the fold *algebra* — properties that MUST hold for both the inline
and sidecar event-store designs, so they are always-on regression guards, not
opt-in experiments. They complement the example-based ``test_fold.py`` with
randomized checks of:

* **order-independence** — the fold re-sorts by ``(ts, id)``, so input order is
  irrelevant;
* **idempotence** — appending the same log twice folds identically;
* **duplicate-id no-op** — repeating events by id changes nothing;
* **per-field last-write-wins** — each scalar field takes the value written by
  the greatest ``(ts, id)`` event that touched it;
* **set add/remove composition** — label adds commute, and a remove after an add
  drops the right element;
* **totality** — an unknown op (``synced``) or unknown payload keys never raise.

The fold treats an event's ``id`` as an opaque dedup/sort key, so the strategy
uses fixed-width decimal ids that sort lexicographically the same as numerically
— enough to exercise the ``(ts, id)`` tiebreak deterministically without needing
real Crockford-base32 ULIDs.
"""

from __future__ import annotations

import random
from typing import Any

import hypothesis.strategies as st
from hypothesis import given, settings

from rohrpost.events import Event
from rohrpost.fold import fold

# Timestamps share a fixed date prefix and zero-pad the millisecond field, so
# lexicographic string order matches chronological order across the drawn window.
_TS_BASE = "2026-08-11T09:00:00"


def _ts(ms: int) -> str:
    """Render ``ms`` as a zero-padded RFC 3339 UTC millisecond timestamp."""
    return f"{_TS_BASE}.{ms:03d}Z"


def _eid(n: int) -> str:
    """Fixed-width decimal id — opaque to the fold, lexicographically ordered."""
    return f"{n:026d}"


_TICKETS = st.sampled_from(["a1b2c3", "d4e5f6"])
_EIDS = st.integers(0, 9_999).map(_eid)
_TIMESTAMPS = st.integers(0, 50).map(_ts)
_ACTOR = st.just("user/alice")
_OP_SET = st.just("set")
_OP_COMMENT = st.just("comment")

_TITLES = st.sampled_from(["alpha", "beta", "gamma", "delta"])
_STATUSES = st.sampled_from(["open", "in_progress", "review", "waiting", "done", "dropped"])
_PRIORITIES = st.integers(0, 4)
_BODIES = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=12
)
_TEXTS = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=16
)
_LABEL_VALUES = st.lists(st.sampled_from(["a", "b", "c", "d"]), min_size=1, max_size=3, unique=True)

# Each set event mutates exactly one field, which keeps per-field LWW reasoning crisp.
_SCALAR_PAYLOAD = st.one_of(
    st.fixed_dictionaries({"title": _TITLES}),
    st.fixed_dictionaries({"status": _STATUSES}),
    st.fixed_dictionaries({"priority": _PRIORITIES}),
    st.fixed_dictionaries({"body": _BODIES}),
)
_SET_PAYLOAD = st.one_of(
    st.fixed_dictionaries({"labels+": _LABEL_VALUES}),
    st.fixed_dictionaries({"labels-": _LABEL_VALUES}),
)
_PAYLOAD = st.one_of(_SCALAR_PAYLOAD, _SET_PAYLOAD)


def _set_events(
    ticket: st.SearchStrategy[str],
    payload: st.SearchStrategy[dict[str, Any]],
    *,
    min_size: int,
    max_size: int,
) -> st.SearchStrategy[list[Event]]:
    """A list of distinct-id ``set`` events drawing ``ticket``/``payload`` from strategies."""
    one = st.builds(
        Event,
        id=_EIDS,
        ts=_TIMESTAMPS,
        ticket=ticket,
        op=_OP_SET,
        actor=_ACTOR,
        set=payload,
    )
    return st.lists(one, min_size=min_size, max_size=max_size, unique_by=lambda e: e.id)


# A comment is the fold's only *non-idempotent* op (it appends). The mixed event
# list below deliberately interleaves comments with field-set events so that the
# idempotence/dedup properties are load-bearing: re-applying a scalar or set op
# is a no-op, so without comments a dedup regression on those ops would be invisible.
_SET_EVENT = st.builds(
    Event,
    id=_EIDS,
    ts=_TIMESTAMPS,
    ticket=_TICKETS,
    op=_OP_SET,
    actor=_ACTOR,
    set=_PAYLOAD,
)
_COMMENT_EVENT = st.builds(
    Event,
    id=_EIDS,
    ts=_TIMESTAMPS,
    ticket=_TICKETS,
    op=_OP_COMMENT,
    actor=_ACTOR,
    text=_TEXTS,
)
_ONE_EVENT = st.one_of(_SET_EVENT, _COMMENT_EVENT)

# Mixed-field, multi-ticket event lists (order / idempotence / duplicate / totality).
_EVENTS = st.lists(_ONE_EVENT, min_size=3, max_size=15, unique_by=lambda e: e.id)
# All scalar-field events on one ticket (per-field last-write-wins).
_SCALAR_EVENTS = _set_events(
    ticket=st.just("a1b2c3"), payload=_SCALAR_PAYLOAD, min_size=2, max_size=12
)
# Pure label-add events on one ticket (set-add commutativity).
_LABEL_ADD_EVENTS = _set_events(
    ticket=st.just("a1b2c3"),
    payload=st.fixed_dictionaries({"labels+": _LABEL_VALUES}),
    min_size=2,
    max_size=8,
)


@given(events=_EVENTS)
@settings(max_examples=100, deadline=None)
def test_fold_is_order_independent(events: list[Event]) -> None:
    """Input order never matters: the fold re-sorts by ``(ts, id)`` internally."""
    result = fold(events)
    assert result  # non-empty: guards against a fold that always returns {}
    shuffled = list(events)
    random.shuffle(shuffled)
    assert result == fold(shuffled)


@given(events=_EVENTS)
@settings(max_examples=100, deadline=None)
def test_fold_is_idempotent(events: list[Event]) -> None:
    """Doubling the log changes nothing: duplicate ids dedupe to the same fold."""
    result = fold(events)
    assert result
    assert result == fold(events + events)


@given(events=_EVENTS, k=st.integers(0, 12))
@settings(max_examples=100, deadline=None)
def test_fold_ignores_duplicate_event_ids(events: list[Event], k: int) -> None:
    """Repeating events (by id) is a no-op: dedup keeps the canonical fold."""
    result = fold(events)
    assert result
    extras = random.choices(events, k=k)
    assert result == fold(events + extras)


@given(events=_SCALAR_EVENTS)
@settings(max_examples=100, deadline=None)
def test_fold_scalar_last_write_wins_picks_max_ts_id(events: list[Event]) -> None:
    """Each scalar field takes the value from the greatest ``(ts, id)`` writer."""
    folded = fold(events)["a1b2c3"]
    for field in ("title", "status", "priority", "body"):
        setters = [e for e in events if e.set is not None and field in e.set]
        if not setters:
            continue
        winner = max(setters, key=lambda e: (e.ts, e.id))
        payload = winner.set
        assert payload is not None  # narrows the optional for the type checker
        assert getattr(folded, field) == payload[field]


@given(events=_LABEL_ADD_EVENTS)
@settings(max_examples=100, deadline=None)
def test_set_adds_commute_regardless_of_input_order(events: list[Event]) -> None:
    """Label adds commute (input order irrelevant) and compose to the sorted union."""
    shuffled = list(events)
    random.shuffle(shuffled)
    folded = fold(events)["a1b2c3"]
    assert folded.labels == fold(shuffled)["a1b2c3"].labels
    expected: set[str] = set()
    for event in events:
        payload = event.set
        assert payload is not None
        raw = payload["labels+"]
        assert isinstance(raw, list)
        expected.update(str(item) for item in raw)
    assert folded.labels == sorted(expected)


@given(
    add_ts=st.integers(0, 40),
    remove_delta=st.integers(1, 10),
    add_id=st.integers(0, 500),
    remove_id=st.integers(501, 999),
    keep=st.sampled_from(["y", "z"]),
    drop=st.sampled_from(["x", "w"]),
)
@settings(max_examples=100, deadline=None)
def test_set_remove_after_add_drops_correctly(
    add_ts: int,
    remove_delta: int,
    add_id: int,
    remove_id: int,
    keep: str,
    drop: str,
) -> None:
    """A ``labels-`` replayed after a ``labels+`` removes the added element."""
    add = Event(
        id=_eid(add_id),
        ts=_ts(add_ts),
        ticket="a1b2c3",
        op="set",
        actor="user/alice",
        set={"labels+": [drop, keep]},
    )
    remove = Event(
        id=_eid(remove_id),
        ts=_ts(add_ts + remove_delta),  # strictly later -> replayed after the add
        ticket="a1b2c3",
        op="set",
        actor="user/alice",
        set={"labels-": [drop]},
    )
    # Input order is irrelevant because the fold re-sorts by (ts, id).
    assert fold([add, remove])["a1b2c3"].labels == [keep]
    assert fold([remove, add])["a1b2c3"].labels == [keep]


@given(events=_EVENTS)
@settings(max_examples=80, deadline=None)
def test_fold_is_total_for_synced_and_unknown_payloads(events: list[Event]) -> None:
    """Unknown ops/payload keys never raise; ``synced`` contributes no ticket."""
    synced = Event(
        id=_eid(10_000_000),
        ts=_ts(0),
        ticket="deadbe",
        op="synced",
        actor="user/alice",
        at=_ts(0),
    )
    rogue = Event(
        id=_eid(20_000_000),
        ts=_ts(1),
        ticket="cafe00",
        op="set",
        actor="user/alice",
        set={"bogus_key": "x", "future_field": 7, "another": [1, 2]},
    )
    result = fold([*events, synced, rogue])  # must not raise
    assert "deadbe" not in result  # the synced watermark is skipped entirely
    assert "cafe00" in result  # the rogue set event still creates a ticket
    assert result["cafe00"].title == ""  # unknown keys leave scalars at their defaults
