"""Focused tests for :mod:`rohrpost.shadow` — the sync merge base."""

from __future__ import annotations

from pathlib import Path

from rohrpost import shadow


def test_read_shadow_none_when_absent(tmp_repo: Path) -> None:
    assert shadow.read_shadow(tmp_repo, "github", "42") is None


def test_write_then_read_round_trips(tmp_repo: Path) -> None:
    shadow.write_shadow(tmp_repo, "github", "42", {"title": "x", "status": "open"})
    assert shadow.read_shadow(tmp_repo, "github", "42") == {"title": "x", "status": "open"}


def test_shadow_path_sanitises_slashes_in_ref(tmp_repo: Path) -> None:
    # A ref like "owner/repo#5" must not escape the shadow dir.
    path = shadow.shadow_path(tmp_repo, "jira", "PROJ/123")
    assert path.parent.name == "jira"
    assert "/" not in path.name


def test_all_shadowed_lists_pairs(tmp_repo: Path) -> None:
    shadow.write_shadow(tmp_repo, "github", "42", {"title": "a"})
    shadow.write_shadow(tmp_repo, "jira", "PROJ-1", {"title": "b"})
    pairs = shadow.all_shadowed(tmp_repo)
    assert ("github", "42") in pairs
    assert ("jira", "PROJ-1") in pairs


def test_read_shadow_tolerates_corrupt_file(tmp_repo: Path) -> None:
    path = shadow.shadow_path(tmp_repo, "github", "42")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert shadow.read_shadow(tmp_repo, "github", "42") is None
