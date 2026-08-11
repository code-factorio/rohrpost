"""Focused tests for :mod:`rohrpost.merge` — the three-way merge engine."""

from __future__ import annotations

from rohrpost.merge import merge_text, three_way


def test_three_way_no_change_when_local_equals_remote() -> None:
    result = three_way({"title": "x"}, {"title": "x"}, {"title": "x"})
    assert result.remote_won == {}
    assert result.local_won == {}
    assert result.conflicts == []


def test_remote_won_when_only_remote_changed() -> None:
    result = three_way({"title": "x"}, {"title": "x"}, {"title": "remote"})
    assert result.remote_won == {"title": "remote"}
    assert result.local_won == {}


def test_local_won_when_only_local_changed() -> None:
    result = three_way({"title": "x"}, {"title": "local"}, {"title": "x"})
    assert result.local_won == {"title": "local"}
    assert result.remote_won == {}


def test_conflict_flagged_when_all_differ() -> None:
    result = three_way({"title": "x"}, {"title": "local"}, {"title": "remote"})
    assert result.local_won == {}
    assert result.remote_won == {}
    assert len(result.conflicts) == 1
    assert result.conflicts[0].field == "title"


def test_policy_local_resolves_conflict() -> None:
    result = three_way({"title": "x"}, {"title": "local"}, {"title": "remote"}, policy="local")
    assert result.local_won == {"title": "local"}
    assert result.conflicts == []


def test_policy_remote_resolves_conflict() -> None:
    result = three_way({"title": "x"}, {"title": "local"}, {"title": "remote"}, policy="remote")
    assert result.remote_won == {"title": "remote"}
    assert result.conflicts == []


def test_body_clean_merge_combines_disjoint_edits() -> None:
    # Edits on well-separated lines (1 and 4) merge cleanly; adjacent edits conflict.
    base = "line1\nline2\nline3\nline4"
    local = "LOCAL\nline2\nline3\nline4"  # edited line 1
    remote = "line1\nline2\nline3\nREMOTE"  # edited line 4
    result = three_way({"body": base}, {"body": local}, {"body": remote})
    merged = str(result.remote_won["body"])
    assert "REMOTE" in merged
    assert "LOCAL" in merged
    assert result.conflicts == []


def test_body_conflict_flagged_on_overlapping_edit() -> None:
    base = "same"
    local = "local version"
    remote = "remote version"
    result = three_way({"body": base}, {"body": local}, {"body": remote})
    assert any(c.field == "body" for c in result.conflicts)


def test_merge_text_returns_local_when_git_unavailable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _no_git(*_a: object, **_kw: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("rohrpost.merge.subprocess.run", _no_git)
    merged, conflict = merge_text("base", "local", "remote")
    assert merged == "local"
    assert conflict
