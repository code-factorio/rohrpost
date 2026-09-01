"""Focused tests for :mod:`rohrpost.fold` — the algorithmic heart of rohrpost."""

from __future__ import annotations

import pytest

from rohrpost.events import Event, Op
from rohrpost.fold import (
    Comment,
    Ticket,
    comment_to_mapping,
    derive_status,
    find_cycle,
    fold,
    is_ready,
    ticket_to_mapping,
)

T0 = "2026-08-11T09:00:00.000Z"
T1 = "2026-08-11T09:00:00.001Z"
T2 = "2026-08-11T09:00:00.002Z"
T3 = "2026-08-11T09:00:00.003Z"


def _ev(
    ticket: str = "a1b2c3",
    *,
    op: Op = "set",
    ts: str = T0,
    eid: str = "01K2X8P4RQ7YFZ3M9NVB6TDHWC",
    actor: str = "user/u@example.com",
    set_payload: dict[str, object] | None = None,
    text: str | None = None,
    remote: str | None = None,
    ref: str | None = None,
    reason: str | None = None,
) -> Event:
    return Event(
        id=eid,
        ts=ts,
        ticket=ticket,
        op=op,
        actor=actor,
        set=set_payload,
        text=text,
        remote=remote,
        ref=ref,
        reason=reason,
    )


def _id(n: int) -> str:
    """Distinct 26-char ULID-ish ids that sort lexicographically with n."""
    base = "01K2X8P4RQ7YFZ3M9NVB6TDHW"
    return base + "ABCDEFGHIJKLMNOPRSTU"[n]


# ---------------------------------------------------------------------------
# Dedup + ordering
# ---------------------------------------------------------------------------
def test_duplicate_event_ids_are_deduped() -> None:
    ev = _ev(eid=_id(0), set_payload={"status": "in_progress"})
    assert fold([ev, ev, ev])["a1b2c3"].status == "in_progress"


def test_events_applied_in_ts_id_order_not_input_order() -> None:
    # Later status wins despite being appended first, because ts orders the fold.
    early = _ev(eid=_id(0), ts=T0, set_payload={"status": "open"})
    late = _ev(eid=_id(1), ts=T3, set_payload={"status": "done"})
    assert fold([late, early])["a1b2c3"].status == "done"


def test_ulid_tiebreaks_equal_timestamps() -> None:
    a = _ev(eid="01K2X8P4RQ7YFZ3M9NVB6TDHW" + "A", ts=T1, set_payload={"status": "open"})
    b = _ev(eid="01K2X8P4RQ7YFZ3M9NVB6TDHW" + "B", ts=T1, set_payload={"status": "done"})
    assert fold([b, a])["a1b2c3"].status == "done"  # 'B' > 'A', so b wins


# ---------------------------------------------------------------------------
# Per-field LWW (the whole point — spec §6)
# ---------------------------------------------------------------------------
def test_concurrent_different_fields_both_survive() -> None:
    status = _ev(eid=_id(0), ts=T1, set_payload={"status": "in_progress"})
    priority = _ev(eid=_id(1), ts=T2, set_payload={"priority": 0})
    ticket = fold([status, priority])["a1b2c3"]
    assert ticket.status == "in_progress"
    assert ticket.priority == 0


def test_same_field_later_ts_wins() -> None:
    first = _ev(eid=_id(0), ts=T1, set_payload={"priority": 3})
    second = _ev(eid=_id(1), ts=T2, set_payload={"priority": 1})
    assert fold([first, second])["a1b2c3"].priority == 1


def test_fieldts_records_last_write_per_field() -> None:
    status = _ev(eid=_id(0), ts=T1, set_payload={"status": "in_progress"})
    status2 = _ev(eid=_id(1), ts=T3, set_payload={"status": "review"})
    ticket = fold([status, status2])["a1b2c3"]
    assert ticket.fieldts["status"] == T3


def test_empty_body_set_folds_to_no_body() -> None:
    """``set {"body": ""}` clears the body, agreeing with the snapshot round-trip.

    The snapshot inverse maps a falsy body to None, so a fold that kept ``""``
    would make ``show`` answer differently depending on whether the (regenerable,
    mtime-gated) snapshot cache or the live fold answered (RP-rf1841).
    """
    create = _ev(eid=_id(0), op="create", set_payload={"title": "t", "body": "old"})
    clear = _ev(eid=_id(1), ts=T1, set_payload={"body": ""})
    ticket = fold([create, clear])["a1b2c3"]
    assert ticket.body is None
    assert ticket.fieldts["body"] == T1
    assert ticket_to_mapping(ticket)["body"] is None


# ---------------------------------------------------------------------------
# Set add/remove ops (labels, blocked_by)
# ---------------------------------------------------------------------------
def test_set_add_remove_compose() -> None:
    add = _ev(eid=_id(0), ts=T1, set_payload={"labels+": ["auth", "bug"]})
    add_more = _ev(eid=_id(1), ts=T2, set_payload={"labels+": ["ui"]})
    remove = _ev(eid=_id(2), ts=T3, set_payload={"labels-": ["bug"]})
    assert fold([add, add_more, remove])["a1b2c3"].labels == ["auth", "ui"]


def test_blocked_by_values_normalised_to_bare_ids() -> None:
    ev = _ev(eid=_id(0), ts=T1, set_payload={"blocked_by+": ["TST-9f8e7d"]})
    assert fold([ev])["a1b2c3"].blocked_by == ["9f8e7d"]


def test_labels_are_freeform_not_id_normalised() -> None:
    ev = _ev(eid=_id(0), ts=T1, set_payload={"labels+": ["auth", "needs-review"]})
    assert fold([ev])["a1b2c3"].labels == ["auth", "needs-review"]


# ---------------------------------------------------------------------------
# Close reasons on the event (spec §5.2)
# ---------------------------------------------------------------------------
def test_last_close_reason_tracks_most_recent_close() -> None:
    close1 = _ev(eid=_id(0), ts=T1, set_payload={"status": "done"}, reason="first")
    reopen = _ev(eid=_id(1), ts=T2, set_payload={"status": "in_progress"})
    close2 = _ev(eid=_id(2), ts=T3, set_payload={"status": "done"}, reason="second")
    ticket = fold([close1, reopen, close2])["a1b2c3"]
    assert ticket.status == "done"
    assert ticket.last_close_reason == "second"


def test_close_without_reason_leaves_reason_none() -> None:
    close = _ev(eid=_id(0), ts=T1, set_payload={"status": "done"})
    assert fold([close])["a1b2c3"].last_close_reason is None


def test_drop_reason_recorded() -> None:
    drop = _ev(eid=_id(0), ts=T1, set_payload={"status": "dropped"}, reason="wontfix")
    assert fold([drop])["a1b2c3"].last_close_reason == "wontfix"


# ---------------------------------------------------------------------------
# Comments (spec §9)
# ---------------------------------------------------------------------------
def test_comments_fold_in_order() -> None:
    c1 = _ev(eid=_id(0), ts=T1, op="comment", text="first")
    c2 = _ev(eid=_id(1), ts=T2, op="comment", text="second")
    ticket = fold([c1, c2])["a1b2c3"]
    assert [c.text for c in ticket.comments] == ["first", "second"]


def test_link_unlink_affect_remotes() -> None:
    link = _ev(eid=_id(0), ts=T1, op="link", remote="github", ref="42")
    unlink = _ev(eid=_id(1), ts=T2, op="unlink", remote="github")
    assert fold([link, unlink])["a1b2c3"].remotes == {}
    assert fold([link])["a1b2c3"].remotes == {"github": "42"}


# ---------------------------------------------------------------------------
# Derived status / readiness (spec §5.4, §5.5)
# ---------------------------------------------------------------------------
def _ticket(tid: str = "a1b2c3", **kw: object) -> Ticket:
    base: dict[str, object] = {
        "id": tid,
        "title": "t",
        "type": "task",
        "status": "open",
        "priority": 2,
        "parent": None,
        "blocked_by": [],
        "labels": [],
        "assignee": None,
        "body": None,
        "remotes": {},
        "last_close_reason": None,
        "comments": [],
        "created": T0,
        "updated": T0,
        "fieldts": {},
    }
    base.update(kw)
    return Ticket(**base)  # type: ignore[arg-type]


def test_is_ready_for_open_unblocked_ticket() -> None:
    by_id = {"a1b2c3": _ticket()}
    assert is_ready(by_id["a1b2c3"], by_id)


def test_not_ready_when_blocked_by_unfinished() -> None:
    by_id = {
        "a1b2c3": _ticket(blocked_by=["9f8e7d"]),
        "9f8e7d": _ticket("9f8e7d", status="in_progress"),
    }
    assert not is_ready(by_id["a1b2c3"], by_id)


def test_ready_when_blocker_done() -> None:
    by_id = {
        "a1b2c3": _ticket(blocked_by=["9f8e7d"]),
        "9f8e7d": _ticket("9f8e7d", status="done"),
    }
    assert is_ready(by_id["a1b2c3"], by_id)


def test_epics_are_never_ready() -> None:
    by_id = {"a1b2c3": _ticket(type="epic")}
    assert not is_ready(by_id["a1b2c3"], by_id)


def test_waiting_excluded_from_ready() -> None:
    by_id = {"a1b2c3": _ticket(status="waiting")}
    assert not is_ready(by_id["a1b2c3"], by_id)


def test_epic_derived_done_when_all_children_done() -> None:
    by_id = {
        "epic1": _ticket("epic1", type="epic", status="open"),
        "c1": _ticket("c1", parent="epic1", status="done"),
        "c2": _ticket("c2", parent="epic1", status="done"),
    }
    assert derive_status(by_id["epic1"], by_id) == "done"


def test_epic_derived_open_if_any_child_pending() -> None:
    by_id = {
        "epic1": _ticket("epic1", type="epic", status="open"),
        "c1": _ticket("c1", parent="epic1", status="done"),
        "c2": _ticket("c2", parent="epic1", status="in_progress"),
    }
    assert derive_status(by_id["epic1"], by_id) == "open"


# ---------------------------------------------------------------------------
# Cycle detection (for doctor)
# ---------------------------------------------------------------------------
def test_find_cycle_detects_dependency_loop() -> None:
    by_id = {
        "a1b2c3": _ticket("a1b2c3", blocked_by=["b2c3d4"]),
        "b2c3d4": _ticket("b2c3d4", blocked_by=["a1b2c3"]),
    }
    assert find_cycle(by_id) is not None


def test_no_cycle_returns_none() -> None:
    by_id = {
        "a1b2c3": _ticket("a1b2c3", blocked_by=["b2c3d4"]),
        "b2c3d4": _ticket("b2c3d4"),
    }
    assert find_cycle(by_id) is None


# ---------------------------------------------------------------------------
# Snapshot round-trip (the cache must be lossless)
# ---------------------------------------------------------------------------
def test_ticket_mapping_round_trips_through_snapshot() -> None:
    from rohrpost.fold import _mapping_to_ticket

    ticket = _ticket(
        labels=["auth"],
        blocked_by=["9f8e7d"],
        assignee="runner/x",
        comments=[Comment(T1, "a", "note")],
        fieldts={"status": T1},
    )
    mapping = ticket_to_mapping(ticket)
    assert _mapping_to_ticket(mapping) == ticket


def test_mapping_renders_prefix_when_given() -> None:
    mapping = ticket_to_mapping(_ticket("a1b2c3", parent="9f8e7d"), prefix="TST")
    assert mapping["id"] == "TST-a1b2c3"
    assert mapping["parent"] == "TST-9f8e7d"


def test_short_mapping_omits_fieldts_and_comments() -> None:
    mapping = ticket_to_mapping(_ticket(), include_fieldts=False, include_comments=False)
    assert "_fieldts" not in mapping
    assert "comments" not in mapping


def test_short_mapping_omits_body_on_request() -> None:
    # The work-queue shape drops the body so prose never reaches `rp ready`/`rp list`
    # (decision experiment E7). The default (full / snapshot) shape keeps it.
    short = ticket_to_mapping(_ticket(body="prose"), include_body=False)
    assert "body" not in short
    assert ticket_to_mapping(_ticket(body="prose"))["body"] == "prose"


def test_comment_mapping_shape() -> None:
    from rohrpost.fold import Comment

    assert comment_to_mapping(Comment(T1, "user/x", "hi")) == {
        "ts": T1,
        "actor": "user/x",
        "text": "hi",
    }


@pytest.mark.parametrize("bad_status", ["ready", "closed", "pending", "DONE"])
def test_unknown_payload_keys_ignored_by_fold(bad_status: str) -> None:
    # A future/typo status should not crash the fold; doctor reports it separately.
    ev = _ev(eid=_id(0), ts=T1, set_payload={"weirdfield": bad_status})
    folded = fold([ev])["a1b2c3"]
    assert folded.title == ""  # unaffected
