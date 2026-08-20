"""Focused tests for :mod:`rohrpost.api` — the one write path."""

from __future__ import annotations

from pathlib import Path

import pytest

from rohrpost import api
from rohrpost.exceptions import TicketError, TicketNotFoundError


def _new(
    repo: Path,
    title: str = "t",
    *,
    actor: str = "user/u@example.com",
    type: str = "task",
    priority: int = 2,
    labels: list[str] | None = None,
    blocked_by: list[str] | None = None,
    parent: str | None = None,
) -> str:
    return api.create_ticket(
        repo,
        title,
        actor=actor,
        type=type,
        priority=priority,
        labels=labels or [],
        blocked_by=blocked_by or [],
        parent=parent,
    ).ticket.id


# ---------------------------------------------------------------------------
# init / propose_prefix
# ---------------------------------------------------------------------------
def test_propose_prefix_from_dir_name(tmp_path: Path) -> None:
    (tmp_path / "My-Project_42").mkdir()
    assert api.propose_prefix(tmp_path / "My-Project_42") == "MYPRO"


def test_propose_prefix_falls_back_when_too_short(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert api.propose_prefix(tmp_path / "a") == "RP"


def test_init_creates_layout_and_config(tmp_path: Path) -> None:
    result = api.init_repo(tmp_path, prefix="TST")
    assert (tmp_path / ".rohrpost" / "config.toml").is_file()
    assert (tmp_path / ".rohrpost" / "log.jsonl").is_file()
    assert (tmp_path / ".rohrpost" / "archive").is_dir()
    assert result.created_config
    assert result.prefix == "TST"


def test_init_is_idempotent(tmp_path: Path) -> None:
    api.init_repo(tmp_path, prefix="TST")
    second = api.init_repo(tmp_path, prefix="XXX")
    assert not second.created_config  # did not clobber existing config
    assert second.prefix == "TST"  # kept the original


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def test_create_ticket_records_initial_fields(tmp_repo: Path) -> None:
    result = api.create_ticket(
        tmp_repo, "Fix race", type="bug", priority=1, labels=["auth"], actor="user/x"
    )
    t = result.ticket
    assert result.wrote
    assert t.title == "Fix race"
    assert t.type == "bug"
    assert t.priority == 1
    assert t.status == "open"
    assert t.labels == ["auth"]


def test_create_rejects_bad_type(tmp_repo: Path) -> None:
    with pytest.raises(TicketError):
        api.create_ticket(tmp_repo, "t", type="chore", actor="user/x")


def test_create_rejects_empty_title(tmp_repo: Path) -> None:
    with pytest.raises(TicketError):
        api.create_ticket(tmp_repo, "   ", actor="user/x")


# ---------------------------------------------------------------------------
# set + idempotency
# ---------------------------------------------------------------------------
def test_set_applies_scalar_fields(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    result = api.set_fields(
        tmp_repo,
        tid,
        [api.parse_assignment("status=in_progress"), api.parse_assignment("priority=0")],
        actor="user/x",
    )
    assert result.wrote
    assert result.ticket.status == "in_progress"
    assert result.ticket.priority == 0


def test_set_is_idempotent_no_second_event(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    api.set_fields(tmp_repo, tid, [api.parse_assignment("status=in_progress")], actor="user/x")
    again = api.set_fields(
        tmp_repo, tid, [api.parse_assignment("status=in_progress")], actor="user/x"
    )
    assert not again.wrote
    assert len(api.event_log(tmp_repo, tid)) == 2  # create + first set only


def test_set_labels_add_remove(tmp_repo: Path) -> None:
    tid = _new(tmp_repo, labels=["a"])
    api.set_fields(tmp_repo, tid, [api.parse_assignment("labels+=b,c")], actor="user/x")
    api.set_fields(tmp_repo, tid, [api.parse_assignment("labels-=a")], actor="user/x")
    assert api.show_ticket(tmp_repo, tid).labels == ["b", "c"]


def test_set_validates_status(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    with pytest.raises(TicketError):
        api.set_fields(tmp_repo, tid, [api.parse_assignment("status=closed")], actor="user/x")


def test_set_unknown_field_raises(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    with pytest.raises(TicketError):
        api.set_fields(tmp_repo, tid, [api.parse_assignment("bogus=1")], actor="user/x")


def test_parse_assignment_rejects_set_op_on_scalar() -> None:
    with pytest.raises(TicketError):
        api.parse_assignment("title+=x")


def test_set_on_missing_ticket_raises(tmp_repo: Path) -> None:
    with pytest.raises(TicketNotFoundError):
        api.set_fields(tmp_repo, "zzzzzz", [api.parse_assignment("status=open")], actor="user/x")


# ---------------------------------------------------------------------------
# claim / close / drop
# ---------------------------------------------------------------------------
def test_claim_stamps_status_and_assignee(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    t = api.claim(tmp_repo, tid, actor="runner/cc").ticket
    assert t.status == "in_progress"
    assert t.assignee == "runner/cc"


def test_claim_idempotent(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    api.claim(tmp_repo, tid, actor="runner/cc")
    assert not api.claim(tmp_repo, tid, actor="runner/cc").wrote


def test_close_records_reason(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    t = api.close(tmp_repo, tid, reason="shipped it", actor="user/x").ticket
    assert t.status == "done"
    assert t.last_close_reason == "shipped it"


def test_close_idempotent(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    api.close(tmp_repo, tid, actor="user/x")
    assert not api.close(tmp_repo, tid, actor="user/x").wrote


def test_drop_sets_dropped(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    assert api.drop(tmp_repo, tid, reason="wontfix", actor="user/x").ticket.status == "dropped"


# ---------------------------------------------------------------------------
# comment / link / log
# ---------------------------------------------------------------------------
def test_comment_appends_note(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    api.add_comment(tmp_repo, tid, "a note", actor="user/x")
    api.add_comment(tmp_repo, tid, "second", actor="user/x")
    notes = api.comments(tmp_repo, tid)
    assert [n.text for n in notes] == ["a note", "second"]


def test_comment_rejects_empty(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    with pytest.raises(TicketError):
        api.add_comment(tmp_repo, tid, "  ", actor="user/x")


def test_link_binds_remote(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    t = api.link_remote(tmp_repo, tid, "github", "42", actor="user/x").ticket
    assert t.remotes == {"github": "42"}


def test_link_and_unlink_are_idempotent(tmp_repo: Path) -> None:
    tid = _new(tmp_repo)
    assert api.link_remote(tmp_repo, tid, "github", "42", actor="user/x").wrote
    assert not api.link_remote(tmp_repo, tid, "github", "42", actor="user/x").wrote
    assert api.unlink_remote(tmp_repo, tid, "github", actor="user/x").wrote
    assert not api.unlink_remote(tmp_repo, tid, "github", actor="user/x").wrote
    assert api.show_ticket(tmp_repo, tid).remotes == {}


def test_load_template_reads_defaults_and_normalises_ids(tmp_repo: Path) -> None:
    template = tmp_repo / "templates" / "bug.toml"
    template.write_text(
        '[defaults]\ntype = "bug"\npriority = 1\nlabels = ["bug", "auth"]\n'
        'blocked_by = ["TST-9f8e7d"]\nbody = "Investigate"\n'
    )
    assert api.load_template(tmp_repo, "bug") == {
        "type": "bug",
        "priority": 1,
        "labels": ["bug", "auth"],
        "blocked_by": ["9f8e7d"],
        "body": "Investigate",
    }


def test_load_template_rejects_unknown_top_level_field(tmp_repo: Path) -> None:
    template = tmp_repo / "templates" / "typo.toml"
    template.write_text("priorit = 1\n")

    with pytest.raises(TicketError, match=r"unknown template field\(s\): priorit"):
        api.load_template(tmp_repo, "typo")


def test_event_log_filtered_to_ticket(tmp_repo: Path) -> None:
    a = _new(tmp_repo, "a")
    _new(tmp_repo, "b")
    assert len(api.event_log(tmp_repo, a)) == 1
    assert len(api.event_log(tmp_repo)) == 2


# ---------------------------------------------------------------------------
# ready / list / tree
# ---------------------------------------------------------------------------
def test_ready_excludes_blocked_until_blocker_done(tmp_repo: Path) -> None:
    dep = _new(tmp_repo, "dep")
    tid = _new(tmp_repo, "blocked", blocked_by=[dep])
    assert [t.id for t in api.ready_tickets(tmp_repo)] == [dep]
    api.close(tmp_repo, dep, actor="user/x")
    ready = [t.id for t in api.ready_tickets(tmp_repo)]
    assert ready == [tid]


def test_ready_limit(tmp_repo: Path) -> None:
    for _ in range(5):
        _new(tmp_repo)
    assert len(api.ready_tickets(tmp_repo, limit=2)) == 2


def test_ready_excludes_epics(tmp_repo: Path) -> None:
    _new(tmp_repo, "epic", type="epic")
    assert api.ready_tickets(tmp_repo) == []


def test_list_filters_by_label_and_status(tmp_repo: Path) -> None:
    _new(tmp_repo, "a", labels=["auth"])
    _new(tmp_repo, "b", labels=["ui"])
    assert len(api.list_tickets(tmp_repo, label="auth")) == 1
    assert len(api.list_tickets(tmp_repo, status="open")) == 2


def test_list_match_is_a_case_insensitive_title_substring(tmp_repo: Path) -> None:
    _new(tmp_repo, "Fix token refresh race")
    _new(tmp_repo, "Profile the fold loop")
    found = api.list_tickets(tmp_repo, match="TOKEN")
    assert [t.title for t in found] == ["Fix token refresh race"]


def test_list_match_composes_with_other_filters(tmp_repo: Path) -> None:
    _new(tmp_repo, "[addr-1] typing", labels=["wayfinder:grilling"])
    _new(tmp_repo, "[addr-2] research")
    found = api.list_tickets(tmp_repo, match="[addr-", label="wayfinder:grilling")
    assert [t.title for t in found] == ["[addr-1] typing"]


def test_bracketed_handle_does_not_match_a_longer_number(tmp_repo: Path) -> None:
    """Wayfinder handles rely on the brackets delimiting the number.

    ``[addr-2]`` must not find ``[addr-20]`` — that is what lets a plain
    substring match address one ticket exactly, so no special matching logic is
    needed. See the "Handles" section of docs/agents/issue-tracker.md.
    """
    _new(tmp_repo, "[addr-2] the second")
    _new(tmp_repo, "[addr-20] the twentieth")
    found = api.list_tickets(tmp_repo, match="[addr-2]")
    assert [t.title for t in found] == ["[addr-2] the second"]


def test_tree_returns_children(tmp_repo: Path) -> None:
    epic = _new(tmp_repo, "epic", type="epic")
    child = _new(tmp_repo, "child", parent=epic)
    tree = api.tree(tmp_repo, epic)
    assert tree.root.id == epic
    assert [c.id for c in tree.children] == [child]


# ---------------------------------------------------------------------------
# prefix / rendering
# ---------------------------------------------------------------------------
def test_snapshot_mapping_renders_prefix(tmp_repo: Path) -> None:
    tid = _new(tmp_repo, "t")
    mapping = api.snapshot_mapping(api.show_ticket(tmp_repo, tid), prefix="TST")
    assert mapping["id"] == f"TST-{tid}"
