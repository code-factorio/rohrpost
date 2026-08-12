"""Focused tests for :mod:`rohrpost.doctor`."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rohrpost import api, doctor, paths
from rohrpost.events import Event, encode


def _new(repo: Path, title: str = "t") -> str:
    return api.create_ticket(repo, title, actor="user/x").ticket.id


def test_doctor_passes_on_healthy_repo(tmp_repo: Path) -> None:
    _new(tmp_repo)
    assert doctor.run(tmp_repo) == 0


def test_doctor_requires_remote_credentials(
    tmp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.config_path(tmp_repo).write_text(
        '[project]\nprefix = "TST"\n\n[remotes.github]\nrepo = "owner/name"\n'
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ROHRPOST_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert doctor.run(tmp_repo) == 1


def test_doctor_accepts_github_token(tmp_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.config_path(tmp_repo).write_text(
        '[project]\nprefix = "TST"\n\n[remotes.github]\nrepo = "owner/name"\n'
    )
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    assert doctor.run(tmp_repo) == 0


def test_doctor_detects_malformed_log(tmp_repo: Path) -> None:
    paths.log_path(tmp_repo).write_text("garbage\n")
    assert doctor.run(tmp_repo) == 1


def test_doctor_detects_dangling_blocked_by(tmp_repo: Path) -> None:
    _new(tmp_repo, "a")
    # Hand-append a set with a blocked_by that points nowhere.
    ev = Event(
        id="01K2X8P4RQ7YFZ3M9NVB6TDHW" + "Z",
        ts="2026-08-11T09:00:00.005Z",
        ticket="aaaaaa",
        op="set",
        actor="user/x",
        set={"blocked_by+": ["zzzzzz"]},
    )
    paths.log_path(tmp_repo).write_text(encode(ev).decode() + "\n")
    assert doctor.run(tmp_repo) == 1


def test_doctor_detects_cycle(tmp_repo: Path) -> None:
    a = _new(tmp_repo, "a")
    b = _new(tmp_repo, "b")
    api.set_fields(tmp_repo, a, [api.parse_assignment(f"blocked_by+={b}")], actor="user/x")
    api.set_fields(tmp_repo, b, [api.parse_assignment(f"blocked_by+={a}")], actor="user/x")
    assert doctor.run(tmp_repo) == 1


def test_doctor_flags_missing_gitattributes(tmp_repo: Path) -> None:
    (tmp_repo.parent / ".gitattributes").unlink()
    assert doctor.run(tmp_repo) == 1


def test_doctor_json_returns_findings(tmp_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _new(tmp_repo)
    code = doctor.run(tmp_repo, json_output=True)
    out = capsys.readouterr().out
    import json

    findings = json.loads(out)
    assert isinstance(findings, list)
    assert all("check" in f and "ok" in f for f in findings)
    assert code == 0


def test_doctor_flags_stale_snapshot_after_external_log_edit(tmp_repo: Path) -> None:
    # Creating tickets writes a snapshot that matches the log.
    _new(tmp_repo, "first")
    # Append a raw event the on-disk snapshot does not reflect.
    ev = Event(
        id="01K2X8P4RQ7YFZ3M9NVB6TDHW" + "Q",
        ts="2026-08-11T09:00:00.009Z",
        ticket="cccccc",
        op="set",
        actor="user/x",
        set={"status": "open"},
    )
    with paths.log_path(tmp_repo).open("a") as fh:
        fh.write(encode(ev).decode() + "\n")
    # doctor compares the stale snapshot to a fresh fold and flags the mismatch.
    assert doctor.run(tmp_repo) == 1
