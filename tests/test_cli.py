"""Tests for :mod:`rohrpost.cli` — the ``rp`` command surface."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from rohrpost import __version__, cli, paths


@pytest.fixture
def cwd_repo(tmp_path: Path) -> Iterator[Path]:
    """Initialise a rohrpost repo and ``chdir`` into it for the duration of the test."""
    import subprocess as sp

    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    sp.run(["git", "config", "user.email", "t@e.st"], cwd=tmp_path, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        cli.main(["init", "--prefix", "TST"])  # writes config + layout into tmp_path (cwd)
        yield tmp_path
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------------------
# Top-level behaviour
# ---------------------------------------------------------------------------
def test_version_prints_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    out, _ = capsys.readouterr()
    assert f"rohrpost {__version__}" in out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    out, _ = capsys.readouterr()
    assert "usage:" in out


def test_help_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    out, _ = capsys.readouterr()
    assert "<command>" in out


def test_conflicts_empty_message(cwd_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["conflicts"]) == 0
    assert "No conflicts" in capsys.readouterr().out


def test_resolve_requires_take(cwd_repo: Path) -> None:
    # `resolve` without --take is a domain error (exit 1), per the handler.
    assert cli.main(["resolve", "aaaaaa"]) == 1


# ---------------------------------------------------------------------------
# Mutations and reads through the CLI
# ---------------------------------------------------------------------------
def test_init_outside_git_creates_layout(tmp_path: Path) -> None:
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        assert cli.main(["init", "--prefix", "AB"]) == 0
    finally:
        os.chdir(cwd)
    assert (tmp_path / ".rohrpost" / "config.toml").is_file()


def test_full_lifecycle_via_cli(cwd_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # new --json
    cli.main(["new", "A ticket", "--json"])
    created = json.loads(capsys.readouterr().out)
    tid = created["id"].split("-")[1]
    assert created["title"] == "A ticket"

    # set + claim
    assert cli.main(["claim", tid]) == 0
    capsys.readouterr()  # drain the human-readable claim line
    cli.main(["show", tid, "--json"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"] == "in_progress"
    assert shown["assignee"].startswith("user/")

    # close with reason, then show carries last_close_reason
    cli.main(["close", tid, "--reason", "done"])
    capsys.readouterr()  # drain the human-readable close line
    cli.main(["show", tid, "--json"])
    assert json.loads(capsys.readouterr().out)["last_close_reason"] == "done"


def test_new_applies_template_defaults(cwd_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (cwd_repo / ".rohrpost" / "templates" / "bug.toml").write_text(
        '[defaults]\ntype = "bug"\npriority = 1\nlabels = ["auth"]\nbody = "template body"\n'
    )
    cli.main(["new", "A bug", "--template", "bug", "--json"])
    ticket = json.loads(capsys.readouterr().out)
    assert ticket["type"] == "bug"
    assert ticket["priority"] == 1
    assert ticket["labels"] == ["auth"]
    assert ticket["body"] == "template body"


def test_ready_lists_unblocked_work(cwd_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["new", "blocker", "--json"])
    dep = json.loads(capsys.readouterr().out)["id"].split("-")[1]
    cli.main(["new", "blocked", "--blocked-by", dep, "--json"])
    blocked = json.loads(capsys.readouterr().out)["id"].split("-")[1]

    cli.main(["ready", "--json"])
    ready_ids = [t["id"].split("-")[1] for t in json.loads(capsys.readouterr().out)]
    assert ready_ids == [dep]

    cli.main(["close", dep])
    capsys.readouterr()
    cli.main(["ready", "--json"])
    ready_ids = [t["id"].split("-")[1] for t in json.loads(capsys.readouterr().out)]
    assert ready_ids == [blocked]


def test_set_idempotent_reports_no_change(
    cwd_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["new", "t", "--json"])
    tid = json.loads(capsys.readouterr().out)["id"].split("-")[1]
    cli.main(["set", tid, "status=in_progress"])
    capsys.readouterr()
    cli.main(["set", tid, "status=in_progress"])
    out = capsys.readouterr().out
    assert "No change" in out


def test_show_unknown_ticket_exits_one(cwd_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["show", "zzzzzz"]) == 1
    _, err = capsys.readouterr()
    assert "no such ticket" in err


def test_command_outside_repo_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        assert cli.main(["list"]) == 1
    finally:
        os.chdir(cwd)
    _, err = capsys.readouterr()
    assert "not a rohrpost repository" in err


def test_no_color_env_disables_ansi(
    cwd_repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    cli.main(["new", "t"])  # a ticket so `ready` has something to colour
    capsys.readouterr()
    cli.main(["ready"])
    out = capsys.readouterr().out
    assert "\033[" not in out


def test_doctor_passes_on_fresh_repo(cwd_repo: Path) -> None:
    assert cli.main(["doctor"]) == 0


def test_log_outputs_events(cwd_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["new", "t", "--json"])
    tid = json.loads(capsys.readouterr().out)["id"].split("-")[1]
    cli.main(["log", tid, "--json"])
    events = json.loads(capsys.readouterr().out)
    assert len(events) == 1
    assert events[0]["op"] == "create"


# ---------------------------------------------------------------------------
# paths module is exercised implicitly above; keep a tiny direct check.
# ---------------------------------------------------------------------------
def test_paths_layout_helpers(tmp_repo: Path) -> None:
    assert paths.log_path(tmp_repo).name == "log.jsonl"
    assert paths.snapshot_path(tmp_repo).name == "tickets.jsonl"
    assert paths.archive_files(tmp_repo) == []


# ---------------------------------------------------------------------------
# Text-path coverage for the remaining commands and renderers (human output).
# ---------------------------------------------------------------------------
def test_text_output_for_all_commands(cwd_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Exercise each command's human-readable path + the detail renderers."""
    # An epic, a child with a body/label/dep, and a standalone open ticket.
    cli.main(["new", "Epic", "--type", "epic", "--json"])
    epic = json.loads(capsys.readouterr().out)["id"].split("-")[1]
    cli.main(["new", "Dependency", "--json"])
    dep = json.loads(capsys.readouterr().out)["id"].split("-")[1]
    cli.main(
        [
            "new",
            "Child",
            "--parent",
            epic,
            "--label",
            "auth",
            "--blocked-by",
            dep,
            "--body",
            "some prose body",
            "--assignee",
            "user/x",
            "--json",
        ]
    )
    child = json.loads(capsys.readouterr().out)["id"].split("-")[1]
    capsys.readouterr()

    # ready (text, empty-ish: dep is ready, child is blocked)
    assert cli.main(["ready"]) == 0
    assert dep in capsys.readouterr().out

    # show with every section renders the detail fields and sections.
    assert cli.main(["show", child, "--include", "body,deps,notes,fieldts"]) == 0
    out = capsys.readouterr().out
    assert "status:" in out
    assert "blocked_by:" in out
    assert "some prose body" in out
    assert "_fieldts:" in out

    # tree shows the epic and its child.
    assert cli.main(["tree", epic]) == 0
    out = capsys.readouterr().out
    assert "Epic" in out
    assert "Child" in out

    # list with filters (label + status).
    assert cli.main(["list", "--label", "auth", "--status", "open"]) == 0
    assert child in capsys.readouterr().out

    # set (text) updates, then comment + comments, then link.
    assert cli.main(["set", child, "labels+=ui"]) == 0
    assert "Updated" in capsys.readouterr().out
    assert cli.main(["comment", child, "a note"]) == 0
    capsys.readouterr()
    assert cli.main(["comments", child]) == 0
    assert "a note" in capsys.readouterr().out
    assert cli.main(["link", child, "github", "42"]) == 0
    assert "Linked" in capsys.readouterr().out

    # log (text) shows the create event.
    assert cli.main(["log", child]) == 0
    assert "create" in capsys.readouterr().out

    # close the dep so the child becomes ready, then drop the child.
    assert cli.main(["close", dep, "--reason", "done"]) == 0
    assert "Closed" in capsys.readouterr().out
    assert cli.main(["ready"]) == 0  # child now ready
    assert child in capsys.readouterr().out
    assert cli.main(["drop", child, "--reason", "wontfix"]) == 0
    assert "Dropped" in capsys.readouterr().out


def test_idempotent_close_reports_already_closed(
    cwd_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["new", "t", "--json"])
    tid = json.loads(capsys.readouterr().out)["id"].split("-")[1]
    cli.main(["close", tid])
    capsys.readouterr()
    cli.main(["close", tid])
    assert "Already closed" in capsys.readouterr().out


def test_no_work_message_when_ready_empty(
    cwd_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["ready"]) == 0
    assert (
        "empty" in capsys.readouterr().out.lower()
        or "no actionable" in capsys.readouterr().out.lower()
    )


def test_list_no_matches_message(cwd_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list", "--label", "nonexistent"]) == 0
    assert "No tickets" in capsys.readouterr().out


def test_show_renders_remotes_and_close_reason(
    cwd_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["new", "t", "--json"])
    tid = json.loads(capsys.readouterr().out)["id"].split("-")[1]
    cli.main(["link", tid, "github", "7"])
    cli.main(["close", tid, "--reason", "shipped"])
    capsys.readouterr()
    cli.main(["show", tid])
    out = capsys.readouterr().out
    assert "remotes:" in out
    assert "github/7" in out
    assert "close:" in out
    assert "shipped" in out
