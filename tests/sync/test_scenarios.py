"""Table-driven sync semantics, invariants, edge cases, and crash recovery."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rohrpost import api, doctor, shadow, store, sync
from rohrpost.config import Config
from rohrpost.fold import Ticket
from rohrpost.merge import MergeResult, Policy, three_way

from .fake_remote import FakeRemote, FakeRemoteError
from .scenarios import CONFLICT_SCENARIOS, SCENARIOS, Scenario

FIELD_MAP: dict[str, object] = {
    "title": "summary",
    "body": "description",
    "labels": "tags",
    "status": {"open": "Open", "review": "Review", "done": "Done"},
    "priority": "rank",
}
REF = "CASE-1"


def _config(fields: set[str], policy: str = "flag") -> Config:
    selected = {name: FIELD_MAP[name] for name in fields}
    return Config(prefix="TST", remotes={"fake": {"policy": policy, "fields": selected}})


def _make_ticket(repo: Path, local: dict[str, object]) -> str:
    raw_labels = local.get("labels", [])
    labels = [str(value) for value in raw_labels] if isinstance(raw_labels, list) else []
    result = api.create_ticket(
        repo,
        str(local.get("title", "scenario ticket")),
        body=str(local["body"]) if "body" in local else None,
        labels=labels,
        actor="user/test",
    )
    tid = result.ticket.id
    if "status" in local and local["status"] != "open":
        api.set_fields(
            repo,
            tid,
            [api.Assignment("set", "status", local["status"])],
            actor="user/test",
        )
    if "priority" in local and local["priority"] != 2:
        api.set_fields(
            repo,
            tid,
            [api.Assignment("set", "priority", local["priority"])],
            actor="user/test",
        )
    api.link_remote(repo, tid, "fake", REF, actor="user/test")
    return tid


def _setup(
    repo: Path, scenario: Scenario, policy: str | None = None
) -> tuple[str, Config, FakeRemote]:
    fields = set(scenario.base) | set(scenario.local) | set(scenario.remote)
    config = _config(fields, policy or scenario.policy)
    tid = _make_ticket(repo, scenario.local)
    remote = FakeRemote(fields=config.remotes["fake"]["fields"])
    remote.seed(REF, scenario.remote)
    shadow.write_shadow(repo, "fake", REF, scenario.base)
    return tid, config, remote


def _ticket_fields(ticket: Ticket, names: set[str]) -> dict[str, object]:
    return {name: getattr(ticket, name) for name in names}


def _snapshot(repo: Path, tid: str, remote: FakeRemote, fields: set[str]) -> tuple[object, ...]:
    ticket = api.show_ticket(repo, tid)
    return (
        _ticket_fields(ticket, fields),
        [(comment.actor, comment.text) for comment in ticket.comments],
        tuple(store.read_events(repo)),
        remote.freeze(),
        shadow.read_shadow(repo, "fake", REF),
    )


def _push_calls(remote: FakeRemote) -> list[dict[str, object]]:
    return [payload for verb, _ref, payload in remote.calls if verb == "push"]


def _remote_key(local_name: str) -> str:
    mapping = FIELD_MAP[local_name]
    return mapping if isinstance(mapping, str) else local_name


def test_fake_remote_keeps_test_affordances_out_of_provider_calls() -> None:
    remote = FakeRemote(fields={"title": "summary"})
    remote.seed(REF, {"title": "seed"})
    before = remote.freeze()

    remote.edit(REF, summary="human edit")

    assert remote.calls == []
    assert before == {REF: {"summary": "seed"}}
    assert remote.freeze() == {REF: {"summary": "human edit"}}
    assert remote.changed_since("00000000000000000001") == [REF]
    assert remote.calls == [("changed_since", "00000000000000000001", {})]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
def test_scenario(
    tmp_repo: Path,
    scenario: Scenario,
) -> None:
    tid, config, remote = _setup(tmp_repo, scenario)
    fields = set(scenario.base) | set(scenario.local) | set(scenario.remote)
    before_dry_run = _snapshot(tmp_repo, tid, remote, fields)

    dry_report = sync.sync_round(tmp_repo, "fake", remote, config, dry_run=True)

    assert _snapshot(tmp_repo, tid, remote, fields) == before_dry_run
    assert _push_calls(remote) == []
    remote.calls.clear()

    first_event = len(store.read_events(tmp_repo))
    report = sync.sync_round(tmp_repo, "fake", remote, config)
    assert report == dry_report

    ticket = api.show_ticket(tmp_repo, tid)
    if scenario.expect_local is not None:
        assert _ticket_fields(ticket, set(scenario.expect_local)) == scenario.expect_local
    if scenario.expect_remote is not None:
        actual_remote = remote.local_item(REF)
        assert {
            name: actual_remote[name] for name in scenario.expect_remote
        } == scenario.expect_remote

    pushed_keys = {key for payload in _push_calls(remote) for key in payload}
    assert pushed_keys == {_remote_key(name) for name in scenario.expect_pushed}
    if scenario.name.startswith("T6"):
        assert _push_calls(remote) == [{"status": "Done"}]

    new_events = store.read_events(tmp_repo)[first_event:]
    assert all(event.actor.startswith("remote/") for event in new_events)
    if scenario.expect_conflict:
        assert ticket.status == "review"
        assert "conflict:fake" in ticket.labels
        assert api.list_conflicts(tmp_repo) == [ticket]
        notes = "\n".join(comment.text for comment in ticket.comments)
        for value in (*scenario.local.values(), *scenario.remote.values()):
            assert str(value).strip() in notes
        if "body" in fields:
            assert ticket.body is not None
            assert "<<<<<<<" in ticket.body
            assert ">>>>>>>" in ticket.body
    else:
        assert "conflict:fake" not in ticket.labels
        local_fields = _ticket_fields(ticket, fields)
        remote_fields = remote.local_item(REF)
        assert local_fields == remote_fields == shadow.read_shadow(tmp_repo, "fake", REF)

    # I1: a completed round is idempotent, including the event log and shadow.
    stable = _snapshot(tmp_repo, tid, remote, fields)
    remote.calls.clear()
    sync.sync_round(tmp_repo, "fake", remote, config)
    assert _snapshot(tmp_repo, tid, remote, fields) == stable
    assert _push_calls(remote) == []


@pytest.mark.parametrize("scenario", CONFLICT_SCENARIOS, ids=lambda scenario: scenario.name)
@pytest.mark.parametrize("policy", ["local", "remote"])
def test_conflict_policy_records_the_losing_value(
    tmp_repo: Path,
    scenario: Scenario,
    policy: str,
) -> None:
    tid, config, remote = _setup(tmp_repo, scenario, policy)
    field = next(iter(scenario.base))

    report = sync.sync_round(tmp_repo, "fake", remote, config)

    ticket = api.show_ticket(tmp_repo, tid)
    winner = scenario.local[field] if policy == "local" else scenario.remote[field]
    assert getattr(ticket, field) == winner
    assert remote.local_item(REF)[field] == winner
    assert report.conflicts == 0
    assert "conflict:fake" not in ticket.labels
    note = ticket.comments[-1]
    assert note.actor == "remote/fake"
    assert "local=" in note.text
    assert "remote=" in note.text
    assert str(scenario.local[field]).strip() in note.text
    assert str(scenario.remote[field]).strip() in note.text
    if field == "body":
        assert "<<<<<<<" not in str(ticket.body)

    stable = _snapshot(tmp_repo, tid, remote, {field})
    remote.calls.clear()
    sync.sync_round(tmp_repo, "fake", remote, config)
    assert _snapshot(tmp_repo, tid, remote, {field}) == stable
    assert _push_calls(remote) == []


def test_first_sync_baselines_without_clobbering_either_side(tmp_repo: Path) -> None:
    scenario = Scenario("S1", {}, {"title": "local"}, {"title": "remote"})
    fields = {"title"}
    config = _config(fields)
    tid = _make_ticket(tmp_repo, scenario.local)
    remote = FakeRemote(fields=config.remotes["fake"]["fields"])
    remote.seed(REF, scenario.remote)

    sync.sync_round(tmp_repo, "fake", remote, config)

    assert api.show_ticket(tmp_repo, tid).title == "local"
    assert remote.local_item(REF)["title"] == "remote"
    assert shadow.read_shadow(tmp_repo, "fake", REF) == {"title": "remote"}
    assert _push_calls(remote) == []


def test_deleted_remote_flags_ticket_and_preserves_shadow(tmp_repo: Path) -> None:
    scenario = Scenario("S2", {"title": "base"}, {"title": "base"}, {"title": "base"})
    tid, config, remote = _setup(tmp_repo, scenario)
    before = shadow.read_shadow(tmp_repo, "fake", REF)
    remote.delete(REF)

    sync.sync_round(tmp_repo, "fake", remote, config)

    ticket = api.show_ticket(tmp_repo, tid)
    assert ticket.status == "review"
    assert "conflict:fake" in ticket.labels
    assert shadow.read_shadow(tmp_repo, "fake", REF) == before


def test_removed_mapping_ignores_stale_shadow_field(tmp_repo: Path) -> None:
    scenario = Scenario("S3", {"title": "same"}, {"title": "same"}, {"title": "same"})
    _tid, config, remote = _setup(tmp_repo, scenario)
    shadow.write_shadow(tmp_repo, "fake", REF, {"title": "same", "obsolete": "old"})

    sync.sync_round(tmp_repo, "fake", remote, config)

    assert _push_calls(remote) == []
    assert shadow.read_shadow(tmp_repo, "fake", REF) == {"title": "same", "obsolete": "old"}


def test_push_preserves_whole_unmapped_remote_item(tmp_repo: Path) -> None:
    scenario = Scenario("S4", {"title": "base"}, {"title": "local"}, {"title": "base"})
    _tid, config, remote = _setup(tmp_repo, scenario)
    remote.edit(REF, custom={"nested": [1, 2, 3]}, owner="human")
    before = remote.freeze()[REF]

    sync.sync_round(tmp_repo, "fake", remote, config)

    after = remote.freeze()[REF]
    assert after["custom"] == before["custom"]
    assert after["owner"] == before["owner"]
    assert after["summary"] == "local"


def test_stale_shadow_recovery_is_redundant_not_duplicate(tmp_repo: Path) -> None:
    scenario = Scenario("S5", {"title": "old"}, {"title": "new"}, {"title": "new"})
    tid, config, remote = _setup(tmp_repo, scenario)
    before_events = tuple(store.read_events(tmp_repo))

    sync.sync_round(tmp_repo, "fake", remote, config)

    assert api.show_ticket(tmp_repo, tid).title == "new"
    assert remote.local_item(REF)["title"] == "new"
    assert tuple(store.read_events(tmp_repo))[:-1] == before_events
    assert shadow.read_shadow(tmp_repo, "fake", REF) == {"title": "new"}


def test_missing_shadow_is_reported_then_rebuilt(tmp_repo: Path) -> None:
    config = _config({"title"})
    tid = _make_ticket(tmp_repo, {"title": "same"})
    remote = FakeRemote(fields=config.remotes["fake"]["fields"])
    remote.seed(REF, {"title": "same"})

    assert doctor.run(tmp_repo) == 1
    sync.sync_round(tmp_repo, "fake", remote, config)

    assert api.show_ticket(tmp_repo, tid).title == "same"
    assert shadow.read_shadow(tmp_repo, "fake", REF) == {"title": "same"}


def test_failure_during_fetch_writes_nothing(tmp_repo: Path) -> None:
    scenario = Scenario("C1", {"title": "base"}, {"title": "base"}, {"title": "remote"})
    tid, config, remote = _setup(tmp_repo, scenario)
    before = _snapshot(tmp_repo, tid, remote, {"title"})
    remote.fail_after(1)

    with pytest.raises(FakeRemoteError):
        sync.sync_round(tmp_repo, "fake", remote, config)

    assert _snapshot(tmp_repo, tid, remote, {"title"}) == before


def test_failure_after_inbound_events_recovers_without_duplicates(tmp_repo: Path) -> None:
    scenario = Scenario(
        "C2",
        {"title": "base", "body": "base body\n"},
        {"title": "local", "body": "base body\n"},
        {"title": "base", "body": "remote body\n"},
    )
    tid, config, remote = _setup(tmp_repo, scenario)
    remote.fail_after(2)

    with contextlib.suppress(FakeRemoteError):
        sync.sync_round(tmp_repo, "fake", remote, config)
    remote.fail_after(None)
    sync.sync_round(tmp_repo, "fake", remote, config)

    ticket = api.show_ticket(tmp_repo, tid)
    assert ticket.title == "local"
    assert ticket.body == "remote body\n"
    assert remote.local_item(REF) == {"title": "local", "body": "remote body\n"}
    inbound_body_events = [
        event
        for event in store.read_events(tmp_repo)
        if event.actor == "remote/fake" and event.set and event.set.get("body") == "remote body\n"
    ]
    assert len(inbound_body_events) == 1


def test_failure_after_push_recovers_from_stale_shadow(
    tmp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = Scenario("C3", {"title": "base"}, {"title": "local"}, {"title": "base"})
    _tid, config, remote = _setup(tmp_repo, scenario)
    real_write = shadow.write_shadow

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected shadow failure")

    monkeypatch.setattr(shadow, "write_shadow", fail_write)
    with pytest.raises(OSError, match="injected"):
        sync.sync_round(tmp_repo, "fake", remote, config)
    assert remote.local_item(REF)["title"] == "local"
    monkeypatch.setattr(shadow, "write_shadow", real_write)

    sync.sync_round(tmp_repo, "fake", remote, config)

    assert shadow.read_shadow(tmp_repo, "fake", REF) == {"title": "local"}
    assert len(_push_calls(remote)) == 1


def test_inverted_shadow_before_inbound_order_would_lose_the_remote_edit(tmp_repo: Path) -> None:
    """Negative control: publishing the shadow first makes recovery push stale local data."""
    scenario = Scenario("C3 inverted", {"title": "base"}, {"title": "base"}, {"title": "remote"})
    _tid, config, remote = _setup(tmp_repo, scenario)

    # This is the deliberately wrong order: publish the fetched remote state,
    # then simulate a crash before its inbound event is appended.
    shadow.write_shadow(tmp_repo, "fake", REF, {"title": "remote"})
    sync.sync_round(tmp_repo, "fake", remote, config)

    assert remote.local_item(REF)["title"] == "base"
    assert _push_calls(remote) == [{"summary": "base"}]


def test_partial_shadow_write_is_not_published(
    tmp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config({"title"})
    _make_ticket(tmp_repo, {"title": "local"})
    remote = FakeRemote(fields=config.remotes["fake"]["fields"])
    remote.seed(REF, {"title": "remote"})
    real_replace = Path.replace

    def fail_replace(_self: Path, _target: Path) -> Path:
        raise OSError("injected replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        sync.sync_round(tmp_repo, "fake", remote, config)
    assert shadow.read_shadow(tmp_repo, "fake", REF) is None
    monkeypatch.setattr(Path, "replace", real_replace)

    sync.sync_round(tmp_repo, "fake", remote, config)
    assert shadow.read_shadow(tmp_repo, "fake", REF) == {"title": "remote"}


def test_local_write_during_fetch_is_deferred_not_clobbered(tmp_repo: Path) -> None:
    fields = {"title", "body"}
    config = _config(fields)
    tid = _make_ticket(tmp_repo, {"title": "base", "body": "base body\n"})

    class MidWriteRemote(FakeRemote):
        mutate: bool = True

        def fetch(self, ref: str) -> dict[str, object]:
            result = super().fetch(ref)
            if self.mutate:
                self.mutate = False
                api.set_fields(
                    tmp_repo,
                    tid,
                    [api.Assignment("set", "title", "late local")],
                    actor="user/test",
                )
            return result

    remote = MidWriteRemote(fields=config.remotes["fake"]["fields"])
    remote.seed(REF, {"title": "base", "body": "remote body\n"})
    shadow.write_shadow(tmp_repo, "fake", REF, {"title": "base", "body": "base body\n"})

    sync.sync_round(tmp_repo, "fake", remote, config)
    assert api.show_ticket(tmp_repo, tid).title == "late local"
    assert api.show_ticket(tmp_repo, tid).body == "base body\n"
    assert shadow.read_shadow(tmp_repo, "fake", REF) == {
        "title": "base",
        "body": "base body\n",
    }

    sync.sync_round(tmp_repo, "fake", remote, config)
    ticket = api.show_ticket(tmp_repo, tid)
    assert ticket.title == "late local"
    assert ticket.body == "remote body\n"
    assert remote.local_item(REF) == {"title": "late local", "body": "remote body\n"}


ticket_fields = st.fixed_dictionaries(
    {
        "title": st.text(min_size=0, max_size=8),
        "status": st.sampled_from(["open", "review", "done"]),
        "labels": st.lists(st.sampled_from(["a", "b", "c"]), unique=True).map(sorted),
    }
)


@settings(max_examples=80, deadline=None)
@given(
    base=ticket_fields,
    local=ticket_fields,
    remote=ticket_fields,
    policy=st.sampled_from(["flag", "local", "remote"]),
)
def test_sync_merge_converges_or_flags_without_inventing_scalars(
    base: dict[str, object],
    local: dict[str, object],
    remote: dict[str, object],
    policy: Policy,
) -> None:
    result = three_way(base, local, remote, policy=policy)
    local_after = {**local, **result.remote_won}
    remote_after = {**remote, **result.local_won}

    if not result.conflicts:
        assert local_after == remote_after
    for field in ("title", "status"):
        assert local_after[field] in {base[field], local[field], remote[field]}
    assert _string_set(local_after["labels"]) <= (
        _string_set(base["labels"]) | _string_set(local["labels"]) | _string_set(remote["labels"])
    )


def _string_set(value: object) -> set[str]:
    assert isinstance(value, list)
    return {str(item) for item in value}


def test_independent_edits_are_commutative() -> None:
    base = {"title": "base", "priority": 2}
    local = {"title": "local", "priority": 2}
    remote = {"title": "base", "priority": 1}

    forward = three_way(base, local, remote)
    reverse_keys = three_way(
        dict(reversed(tuple(base.items()))),
        dict(reversed(tuple(local.items()))),
        dict(reversed(tuple(remote.items()))),
    )

    assert (
        forward
        == reverse_keys
        == MergeResult(remote_won={"priority": 1}, local_won={"title": "local"})
    )
