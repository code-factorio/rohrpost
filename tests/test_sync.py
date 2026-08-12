"""Focused tests for :mod:`rohrpost.sync`, the provider, and conflicts/resolve."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from rohrpost import api, shadow, store, sync
from rohrpost.config import Config
from rohrpost.events import SYNC_TICKET
from rohrpost.exceptions import TicketError
from rohrpost.providers.github import GitHubProvider


class FakeProvider:
    """A deterministic in-memory provider for sync_round tests."""

    remote = "github"

    def __init__(self, live: dict[str, dict[str, Any]]) -> None:
        self.live = live
        self.pushed: dict[str, dict[str, Any]] = {}

    def fetch(self, ref: str) -> dict[str, Any]:
        return dict(self.live.get(ref, {}))

    def push(self, ref: str, fields: dict[str, Any]) -> dict[str, Any]:
        self.pushed[ref] = dict(fields)
        self.live[ref] = {**self.live.get(ref, {}), **fields}
        return dict(self.live[ref])


def _config() -> Config:
    return Config(
        prefix="TST",
        remotes={"github": {"policy": "flag", "fields": {"title": "title", "body": "body"}}},
    )


def _linked(repo: Path, ref: str = "42", title: str = "t") -> str:
    tid = api.create_ticket(repo, title, actor="user/x").ticket.id
    api.link_remote(repo, tid, "github", ref, actor="user/x")
    return tid


# ---------------------------------------------------------------------------
# sync_round
# ---------------------------------------------------------------------------
def test_sync_pulls_remote_change_locally(tmp_repo: Path) -> None:
    tid = _linked(tmp_repo, ref="42", title="unchanged")
    shadow.write_shadow(tmp_repo, "github", "42", {"title": "unchanged"})  # base == local
    provider = FakeProvider({"42": {"title": "remote-edit"}})

    report = sync.sync_round(tmp_repo, "github", provider, _config())
    assert report.pulled == 1
    assert report.pushed == 0
    assert api.show_ticket(tmp_repo, tid).title == "remote-edit"


def test_sync_pushes_local_change_to_remote(tmp_repo: Path) -> None:
    _linked(tmp_repo, ref="42", title="local-edit")
    shadow.write_shadow(tmp_repo, "github", "42", {"title": "base"})
    provider = FakeProvider({"42": {"title": "base"}})

    report = sync.sync_round(tmp_repo, "github", provider, _config())
    assert report.pushed == 1
    assert provider.pushed["42"]["title"] == "local-edit"


def test_sync_flags_conflict_for_review(tmp_repo: Path) -> None:
    tid = _linked(tmp_repo, ref="42", title="local")
    shadow.write_shadow(tmp_repo, "github", "42", {"title": "base"})
    provider = FakeProvider({"42": {"title": "remote"}})  # all three differ

    sync.sync_round(tmp_repo, "github", provider, _config())
    ticket = api.show_ticket(tmp_repo, tid)
    assert ticket.status == "review"
    assert "conflict:github" in ticket.labels
    assert len(api.list_conflicts(tmp_repo)) == 1


def test_sync_dry_run_touches_nothing(tmp_repo: Path) -> None:
    tid = _linked(tmp_repo, ref="42", title="unchanged")
    shadow.write_shadow(tmp_repo, "github", "42", {"title": "unchanged"})
    provider = FakeProvider({"42": {"title": "remote-edit"}})

    sync.sync_round(tmp_repo, "github", provider, _config(), dry_run=True)
    assert api.show_ticket(tmp_repo, tid).title == "unchanged"  # not applied
    assert provider.pushed == {}  # not pushed


def test_sync_rewrites_shadow(tmp_repo: Path) -> None:
    tid = _linked(tmp_repo, ref="42", title="unchanged")
    shadow.write_shadow(tmp_repo, "github", "42", {"title": "unchanged"})
    provider = FakeProvider({"42": {"title": "remote-edit"}})

    sync.sync_round(tmp_repo, "github", provider, _config())
    snap = shadow.read_shadow(tmp_repo, "github", "42")
    assert snap is not None
    assert snap["title"] == "remote-edit"
    assert [event.op for event in api.event_log(tmp_repo)].count("synced") == 1
    watermarks = [event for event in store.read_events(tmp_repo) if event.op == "synced"]
    assert len(watermarks) == 1
    assert watermarks[0].ticket == SYNC_TICKET
    assert watermarks[0].ticket != tid


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------
def test_resolve_clears_conflict(tmp_repo: Path) -> None:
    tid = _linked(tmp_repo, ref="42", title="local")
    shadow.write_shadow(tmp_repo, "github", "42", {"title": "base"})
    provider = FakeProvider({"42": {"title": "remote"}})
    sync.sync_round(tmp_repo, "github", provider, _config())
    assert api.list_conflicts(tmp_repo)

    api.resolve_conflict(tmp_repo, tid, "local", actor="user/x")
    ticket = api.show_ticket(tmp_repo, tid)
    assert "conflict:github" not in ticket.labels
    assert ticket.status == "open"
    assert api.list_conflicts(tmp_repo) == []


def test_resolve_without_conflict_is_noop(tmp_repo: Path) -> None:
    tid = _linked(tmp_repo, ref="42")
    result = api.resolve_conflict(tmp_repo, tid, "local", actor="user/x")
    assert not result.wrote


def test_resolve_rejects_bad_take(tmp_repo: Path) -> None:
    tid = _linked(tmp_repo, ref="42")
    with pytest.raises(TicketError):
        api.resolve_conflict(tmp_repo, tid, "sideways", actor="user/x")


# ---------------------------------------------------------------------------
# GitHub provider (gh preferred + httpx fallback)
# ---------------------------------------------------------------------------
def _gh_config() -> dict[str, Any]:
    return {
        "repo": "owner/name",
        "fields": {
            "title": "title",
            "body": "body",
            "labels": "labels",
            "status": {"open": "open", "done": "closed"},
        },
    }


def test_github_provider_fetch_via_gh_runner() -> None:
    issue = {"title": "T", "body": "B", "state": "open", "labels": [{"name": "auth"}]}
    captured: list[list[str]] = []

    def runner(args: list[str]) -> str:
        captured.append(args)
        return json.dumps(issue)

    provider = GitHubProvider(_gh_config(), gh_runner=runner, prefer_gh=True)
    fetched = provider.fetch("7")
    assert fetched["title"] == "T"
    assert fetched["status"] == "open"
    assert fetched["labels"] == ["auth"]
    assert captured == [["api", "repos/owner/name/issues/7"]]


def test_github_provider_push_via_gh_runner() -> None:
    captured: list[list[str]] = []

    def runner(args: list[str]) -> str:
        captured.append(args)
        return json.dumps({"title": "new", "body": "b", "state": "closed"})

    provider = GitHubProvider(_gh_config(), gh_runner=runner, prefer_gh=True)
    result = provider.push("7", {"title": "new", "status": "done"})
    assert result["status"] == "done"
    # The PATCH args carry the mapped field values.
    flat = " ".join(captured[0])
    assert "title=new" in flat
    assert "state=closed" in flat


def test_github_provider_falls_back_to_httpx() -> None:
    issue = {"title": "T", "body": "B", "state": "open"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/name/issues/7"
        return httpx.Response(200, json=issue)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    provider = GitHubProvider(_gh_config(), client=client, prefer_gh=False)
    fetched = provider.fetch("7")
    assert fetched["title"] == "T"
    assert fetched["status"] == "open"
