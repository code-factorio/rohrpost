"""Golden cases + plumbing-cost measurement for the body three-way text merge (spec §8.3).

Bodies are the one field that earns a real ``git merge-file`` three-way merge rather
than per-field last-writer-wins: LWW would silently discard human prose. The first
half of this file pins the merge SEMANTICS with small markdown golden cases — disjoint
section edits combine cleanly, overlapping edits conflict, reorders and wholesale
replacements are flagged rather than auto-resolved, and identical edits deduplicate.

The second half quantifies the inline body-merge path's PLUMBING cost: every merge
writes exactly three temp files (base/local/remote) and shells out to one
``git merge-file`` subprocess. That per-merge glue bill is the concrete sidecar
argument tracked by decision experiment E6, and these tests regression-proof it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from rohrpost.merge import merge_text, three_way


def _sections(*sections: str) -> str:
    """Join markdown sections with blank-line separators and a trailing newline."""
    return "\n\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# Golden cases: merge semantics over markdown bodies (spec §8.3)
# ---------------------------------------------------------------------------
def test_disjoint_section_edits_merge_cleanly() -> None:
    """Local edits Context, remote edits AC: both edits survive, no conflict."""
    base = _sections("## Context\nold context", "## AC\nold ac")
    local = _sections("## Context\nnew context", "## AC\nold ac")
    remote = _sections("## Context\nold context", "## AC\nnew ac")

    merged, had_conflict = merge_text(base, local, remote)

    assert had_conflict is False
    assert merged == _sections("## Context\nnew context", "## AC\nnew ac")


def test_overlapping_paragraph_edit_is_a_conflict() -> None:
    """Both sides edit the same paragraph differently: conflict, neither edit dropped."""
    base = _sections("## Context\nshared para", "## AC\nthe ac")
    local = _sections("## Context\nlocal para", "## AC\nthe ac")
    remote = _sections("## Context\nremote para", "## AC\nthe ac")

    merged, had_conflict = merge_text(base, local, remote)

    assert had_conflict is True
    assert "<<<<<<<" in merged
    assert ">>>>>>>" in merged
    assert "local para" in merged
    assert "remote para" in merged


def test_new_sections_at_distinct_insertion_points_both_survive() -> None:
    """Two new sections land at different line positions: both survive, no conflict.

    Two additions at the SAME line conflict in git's line-based merge, so the
    precondition for a clean union is structurally distinct insertion points —
    here a mid-body insertion (## Risks) versus an end-of-body append (## Notes).
    """
    base = _sections("## Context\nctx", "## AC\nac")
    local = _sections("## Context\nctx", "## AC\nac", "## Notes\nlocal notes")
    remote = _sections("## Context\nctx", "## Risks\nremote risks", "## AC\nac")

    merged, had_conflict = merge_text(base, local, remote)

    assert had_conflict is False
    assert merged == _sections(
        "## Context\nctx",
        "## Risks\nremote risks",
        "## AC\nac",
        "## Notes\nlocal notes",
    )


def test_reorder_against_an_edit_is_a_conflict() -> None:
    """Local reorders sections while remote edits one: conflict, no clever auto-merge."""
    base = _sections("## Context\nctx", "## AC\nac")
    local = _sections("## AC\nac", "## Context\nctx")
    remote = _sections("## Context\nctx", "## AC\nedited ac")

    merged, had_conflict = merge_text(base, local, remote)

    assert had_conflict is True
    assert "<<<<<<<" in merged


def test_wholesale_remote_replace_does_not_silently_clobber_local() -> None:
    """Remote replaces the whole body while local edits a line: conflict, not a clobber."""
    base = _sections("## Context\nthe context", "## AC\nthe ac", "## Notes\nthe notes")
    local = _sections("## Context\nthe context", "## AC\nedited ac", "## Notes\nthe notes")
    remote = _sections("## Totally Different\nfresh body", "new structure entirely")

    merged, had_conflict = merge_text(base, local, remote)

    assert had_conflict is True
    assert "<<<<<<<" in merged
    assert "edited ac" in merged
    assert "fresh body" in merged


def test_identical_edit_on_both_sides_merges_without_duplication() -> None:
    """Same edit on both sides: clean merge, single copy, no duplicated text."""
    base = _sections("## Context\nold context", "## AC\nold ac")
    both = _sections("## Context\nnew context", "## AC\nold ac")

    merged, had_conflict = merge_text(base, both, both)

    assert had_conflict is False
    assert merged == both
    assert merged.count("new context") == 1


# ---------------------------------------------------------------------------
# Field-level routing of the body through three_way (the body plumbing)
# ---------------------------------------------------------------------------
def test_three_way_routes_overlapping_body_into_conflicts() -> None:
    """A conflicting body surfaces in MergeResult.conflicts under the flag policy."""
    base = _sections("## Context\nshared para", "## AC\nthe ac")
    local = _sections("## Context\nlocal para", "## AC\nthe ac")
    remote = _sections("## Context\nremote para", "## AC\nthe ac")

    result = three_way({"body": base}, {"body": local}, {"body": remote})

    assert not result.clean
    assert len(result.conflicts) == 1
    assert result.conflicts[0].field == "body"
    assert result.conflicts[0].local == local
    assert result.conflicts[0].remote == remote
    assert result.remote_won == {}
    assert result.local_won == {}


def test_three_way_clean_body_lands_in_both_buckets() -> None:
    """A clean body merge is adopted on both sides (apply locally and push remotely)."""
    base = _sections("## Context\nold context", "## AC\nold ac")
    local = _sections("## Context\nnew context", "## AC\nold ac")
    remote = _sections("## Context\nold context", "## AC\nnew ac")
    expected = _sections("## Context\nnew context", "## AC\nnew ac")

    result = three_way({"body": base}, {"body": local}, {"body": remote})

    assert result.clean
    assert result.remote_won == {"body": expected}
    assert result.local_won == {"body": expected}


def test_three_way_skips_body_when_local_equals_remote() -> None:
    """When local == remote the body is a no-op even if both differ from base."""
    base = _sections("## Context\nold")
    same = _sections("## Context\nnew")

    result = three_way({"body": base}, {"body": same}, {"body": same})

    assert result.clean
    assert result.remote_won == {}
    assert result.local_won == {}
    assert result.conflicts == []


# ---------------------------------------------------------------------------
# Plumbing cost: the concrete sidecar argument (decision experiment E6)
# ---------------------------------------------------------------------------
def test_merge_text_writes_exactly_three_temp_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each merge writes exactly the base/local/remote temp files — count the glue."""
    written: list[str] = []
    real_write_text: Callable[..., int] = Path.write_text

    def spy(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        written.append(self.name)
        return real_write_text(self, data, encoding, errors, newline)

    monkeypatch.setattr(Path, "write_text", spy)
    merge_text("base body\n", "local body\n", "remote body\n")

    assert len(written) == 3
    assert set(written) == {"base", "local", "remote"}


def test_merge_text_invokes_git_merge_file_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each merge shells out to ``git merge-file`` exactly once — capture the argv."""
    captured: list[list[str]] = []

    def spy(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        head = args[0] if args else None
        argv: list[str] = []
        if isinstance(head, list):
            argv = [str(part) for part in head]
            captured.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"merged\n", stderr=b"")

    monkeypatch.setattr("rohrpost.merge.subprocess.run", spy)
    merge_text("base\n", "local\n", "remote\n")

    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "git"
    assert "merge-file" in argv
