"""Spec §7 merge=union + dedupe-on-read story, exercised on long-body log lines.

This is decision experiment E4. It proves that ``.rohrpost/log.jsonl merge=union``
together with the fold's by-``id`` dedup is a correct concurrency story even when
individual JSONL lines far exceed ``PIPE_BUF`` (the regime the advisory lock exists
for), and that the classic no-trailing-newline footgun cannot glue two events into
one malformed line.

Git's union merge driver is deterministic, so these are always-on regression tests,
not ``@pytest.mark.experiment`` timing/race experiments. Each test drives a real git
repo via ``subprocess``; the ``tmp_repo`` fixture has already run ``git init`` and
``rp init``, the latter of which writes the ``merge=union`` rule into
``.gitattributes`` for ``log.jsonl``.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

from rohrpost import paths, store
from rohrpost.events import Event, encode
from rohrpost.fold import fold
from rohrpost.ids import new_ulid
from rohrpost.stats import pipe_buf

# 2026-01-01 00:00:00 UTC as ms — a stable base for strictly-increasing timestamps.
_BASE_MS: int = 1_767_225_600_000
# Payload size that guarantees an encoded JSONL line exceeds PIPE_BUF (4096 on Linux).
_EIGHT_K: int = 8192
_ACTOR: str = "user/merger@example.com"
# Bare 6-char ticket ids (the display prefix never enters the log). Two distinct ids
# so one branch can edit the same ticket the other touches while a second ticket
# isolates a branch-unique line.
_SHARED: str = "a1b2c3"
_OTHER: str = "b2c3d4"


def _ts(index: int) -> str:
    """Render a strictly-increasing RFC3339 UTC ms timestamp, ordered by ``index``."""
    return (
        dt.datetime.fromtimestamp((_BASE_MS + index) / 1000, tz=dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _eid(index: int) -> str:
    """A ULID whose embedded ms is unique per ``index`` so ids never collide."""
    return new_ulid(timestamp_ms=_BASE_MS + index)


def _set_event(eid: str, ts: str, ticket: str, payload: dict[str, object]) -> Event:
    """Build a ``set`` event carrying ``payload`` — the workhorse field-update op."""
    return Event(id=eid, ts=ts, ticket=ticket, op="set", actor=_ACTOR, set=payload)


def _run(repo_root: Path, *args: str) -> None:
    """Run ``git <args>`` in ``repo_root``; raise ``CalledProcessError`` on non-zero exit."""
    subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True, text=True)


def _run_out(repo_root: Path, *args: str) -> str:
    """Run ``git <args>`` and return its stripped stdout."""
    result = subprocess.run(
        ["git", *args], cwd=repo_root, check=True, capture_output=True, text=True
    )
    return (result.stdout or "").strip()


def _initial_commit(repo_root: Path) -> str:
    """Commit the ``rp init`` scaffold as the shared merge base; return its SHA."""
    _run(repo_root, "add", "-A")
    _run(repo_root, "commit", "-qm", "base")
    return _run_out(repo_root, "rev-parse", "HEAD")


def _commit_log(repo_root: Path, message: str) -> None:
    """Stage only the event log and commit (leave ``.lock`` and friends untracked)."""
    _run(repo_root, "add", ".rohrpost/log.jsonl")
    _run(repo_root, "commit", "-qm", message)


# ---------------------------------------------------------------------------
# Case 1: long-body lines on both branches, one ticket edited by both.
# ---------------------------------------------------------------------------
def test_union_merge_keeps_all_long_body_lines_and_folds_deterministically(
    tmp_repo: Path,
) -> None:
    repo_root = tmp_repo.parent
    base_sha = _initial_commit(repo_root)

    body_a = "A" * _EIGHT_K
    body_b = "B" * _EIGHT_K
    # Branch A touches the shared ticket twice (an 8K body, then a status edit).
    e1 = _set_event(_eid(1), _ts(1), _SHARED, {"body": body_a})
    e2 = _set_event(_eid(2), _ts(2), _SHARED, {"status": "in_progress"})
    # Branch B touches the SAME shared ticket with a second 8K body (later ts) and
    # also edits a different ticket so the merge carries a branch-unique line too.
    e3 = _set_event(_eid(3), _ts(3), _SHARED, {"body": body_b})
    e4 = _set_event(_eid(4), _ts(4), _OTHER, {"priority": 0})

    _run(repo_root, "checkout", "-b", "topic-a")
    store.append_event(tmp_repo, e1)
    store.append_event(tmp_repo, e2)
    _commit_log(repo_root, "topic-a events")

    _run(repo_root, "checkout", "-b", "topic-b", base_sha)
    store.append_event(tmp_repo, e3)
    store.append_event(tmp_repo, e4)
    _commit_log(repo_root, "topic-b events")

    _run(repo_root, "checkout", "topic-a")
    _run(repo_root, "merge", "--no-edit", "topic-b")

    # Every line decodes (read_events raises on the first malformed line) and all
    # four distinct event ids survive the union merge.
    merged = store.read_events(tmp_repo)
    assert {ev.id for ev in merged} == {e1.id, e2.id, e3.id, e4.id}
    assert len(merged) == 4

    # The two long-body events really do exceed PIPE_BUF: this is the regime the
    # advisory lock protects within a process, and union merge must keep intact
    # across branches.
    buf = pipe_buf(tmp_repo)
    assert len(encode(e1)) > buf
    assert len(encode(e3)) > buf

    # Both 8K bodies round-tripped byte-exactly through the merge — the loser's
    # line is still in the log; the fold picks the later-ts body by per-field LWW.
    by_id = {ev.id: ev for ev in merged}
    assert by_id[e1.id].set == {"body": body_a}
    assert by_id[e3.id].set == {"body": body_b}

    # The fold dedupes by id and is independent of the order the merge emitted the
    # lines: folding the merged log equals folding either interleaving directly.
    folded_merged = fold(merged)
    assert folded_merged == fold([e1, e2, e3, e4])
    assert folded_merged == fold([e3, e4, e1, e2])

    # Per-ticket outcomes: different fields both survive on the shared ticket, its
    # body resolves to the later-ts writer, and the other ticket gets its own edit.
    assert set(folded_merged) == {_SHARED, _OTHER}
    assert folded_merged[_SHARED].body == body_b
    assert folded_merged[_SHARED].status == "in_progress"
    assert folded_merged[_OTHER].priority == 0


# ---------------------------------------------------------------------------
# Case 2: the no-trailing-newline footgun (spec §7 long-line hazard).
# ---------------------------------------------------------------------------
def test_union_merge_does_not_glue_a_missing_final_newline(tmp_repo: Path) -> None:
    """A hand-edited or compacted log can drop the trailing newline.

    The union driver must still keep the two sides' events on separate lines rather
    than concatenating the last line of one side onto the first line of the other
    (which would produce one undecodable line). ``store.append_event`` always writes
    a trailing newline, so a normally-appended log never enters this state; this is
    the safety net for a log produced outside the one write path (spec §3 principle 3).
    """
    repo_root = tmp_repo.parent
    base_sha = _initial_commit(repo_root)

    e_a = _set_event(_eid(10), _ts(10), _SHARED, {"status": "in_progress"})
    e_b = _set_event(_eid(11), _ts(11), _SHARED, {"status": "review"})

    # Branch A: hand-write the log with NO trailing newline (append_event never does
    # this — it always appends encode(ev) + b"\n").
    _run(repo_root, "checkout", "-b", "topic-a")
    paths.log_path(tmp_repo).write_bytes(encode(e_a))
    _commit_log(repo_root, "topic-a no trailing newline")

    # Branch B from base: append normally, with a trailing newline.
    _run(repo_root, "checkout", "-b", "topic-b", base_sha)
    store.append_event(tmp_repo, e_b)
    _commit_log(repo_root, "topic-b newline")

    _run(repo_root, "checkout", "topic-a")
    _run(repo_root, "merge", "--no-edit", "topic-b")

    # read_events raises on the first malformed line, so succeeding with two events
    # is itself the proof that no two lines were glued together; the newline count
    # pins the byte-level reality explicitly.
    merged = store.read_events(tmp_repo)
    assert len(merged) == 2
    assert {ev.id for ev in merged} == {e_a.id, e_b.id}

    raw = paths.log_path(tmp_repo).read_bytes()
    assert raw.count(b"\n") == 2

    # Folding is unaffected: per-field LWW picks the later-ts status.
    folded = fold(merged)
    assert set(folded) == {_SHARED}
    assert folded[_SHARED].status == "review"


# ---------------------------------------------------------------------------
# Case 3: sanity — an identical event on both branches collapses to one line.
# ---------------------------------------------------------------------------
def test_union_merge_collapses_identical_event_to_one_line(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    base_sha = _initial_commit(repo_root)

    # One event, appended byte-identically on both branches (same id, same payload).
    same = _set_event(_eid(20), _ts(20), _SHARED, {"status": "open"})

    _run(repo_root, "checkout", "-b", "topic-a")
    store.append_event(tmp_repo, same)
    _commit_log(repo_root, "topic-a identical")

    _run(repo_root, "checkout", "-b", "topic-b", base_sha)
    store.append_event(tmp_repo, same)
    _commit_log(repo_root, "topic-b identical")

    _run(repo_root, "checkout", "topic-a")
    _run(repo_root, "merge", "--no-edit", "topic-b")

    # Git's 3-way merge sees both sides made the identical addition and emits one
    # line; the fold therefore sees a single event (no duplicate to dedup).
    merged = store.read_events(tmp_repo)
    assert len(merged) == 1
    assert merged[0].id == same.id

    raw = paths.log_path(tmp_repo).read_bytes()
    assert raw.count(b"\n") == 1

    folded = fold(merged)
    assert set(folded) == {_SHARED}
    assert folded[_SHARED].status == "open"
