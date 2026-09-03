"""Replay the repository's own event log: read-only output must be byte-identical.

The log at the repo root holds real tickets written by the reference over
months (epics, dependencies, comments, close reasons, unicode). Both
implementations fold the same bytes, so no normalisation is needed.
"""

from __future__ import annotations

import json

import pytest

from conformance.conftest import ROOT, Pair, Result, copy_fixture_repo

FIXTURE_LOG = ROOT / ".rohrpost" / "log.jsonl"
FIXTURE_CONFIG = '[project]\nprefix = "RP"\n'


@pytest.fixture
def replay(pair: Pair) -> Pair:
    for impl in pair.both:
        copy_fixture_repo(impl, FIXTURE_LOG, FIXTURE_CONFIG)
    return pair


def _exact(pair: Pair, *args: str) -> Result:
    ref, nat = pair.run(*args)
    assert nat.code == ref.code, f"exit differs for {args}: {nat.err}"
    assert nat.stdout == ref.stdout, f"stdout differs for {args}"
    assert nat.stderr == ref.stderr, f"stderr differs for {args}"
    return nat


def test_list_and_ready_exact(replay: Pair) -> None:
    _exact(replay, "list")
    _exact(replay, "list", "--json")
    _exact(replay, "ready")
    _exact(replay, "ready", "--json")
    _exact(replay, "ready", "--limit", "3", "--json")
    for status in ("open", "in_progress", "review", "waiting", "done", "dropped", "ready"):
        _exact(replay, "list", "--status", status, "--json")
    _exact(replay, "list", "--type", "epic", "--json")
    _exact(replay, "list", "--label", "wayfinder:map", "--json")
    _exact(replay, "list", "--match", "[win", "--json")
    _exact(replay, "conflicts", "--json")
    _exact(replay, "log", "--json")
    _exact(replay, "log")


def test_every_ticket_exact(replay: Pair) -> None:
    tickets = json.loads(replay.reference.run("list", "--json").stdout)
    assert tickets
    for t in tickets:
        tid = t["id"]
        _exact(replay, "show", tid)
        _exact(replay, "show", tid, "--include", "body,deps,notes,fieldts")
        _exact(replay, "show", tid, "--json")
        _exact(replay, "comments", tid, "--json")
        _exact(replay, "log", tid, "--json")
        if t["type"] == "epic":
            _exact(replay, "tree", tid)
            _exact(replay, "tree", tid, "--json")
        parent = t["parent"]
        if parent:
            _exact(replay, "list", "--parent", parent, "--json")


def test_doctor_and_stats_shape(replay: Pair) -> None:
    _exact(replay, "doctor", "--json")
    _exact(replay, "doctor")
    ref, nat = replay.run("stats", "--json")
    assert nat.code == ref.code == 0
    ref_stats, nat_stats = ref.json(), nat.json()
    assert list(nat_stats) == list(ref_stats)
    for key in ("tickets", "events", "pipe_buf", "body_bytes"):
        assert nat_stats[key] == ref_stats[key], key
    ref_line, nat_line = ref_stats["event_line_bytes"], nat_stats["event_line_bytes"]
    assert nat_line == ref_line
    assert isinstance(nat_stats["fold_ms"], float)


def test_snapshot_written_by_one_is_read_by_the_other(replay: Pair) -> None:
    """The tickets.jsonl cache is an interchange format: both must read either's."""
    ref, nat = replay.both
    assert ref.run("list", "--json").code == 0
    ref_snapshot = ref.rohrpost_dir / "tickets.jsonl"
    assert nat.run("list", "--json").code == 0
    nat_snapshot = nat.rohrpost_dir / "tickets.jsonl"
    assert ref_snapshot.read_bytes() == nat_snapshot.read_bytes()
    # Swap the caches and make sure doctor still agrees they are fresh.
    ref_bytes = ref_snapshot.read_bytes()
    ref_snapshot.write_bytes(nat_snapshot.read_bytes())
    nat_snapshot.write_bytes(ref_bytes)
    _exact(replay, "doctor", "--json")
    _exact(replay, "list", "--json")


def test_compact_rejects_the_sync_watermark_identically(replay: Pair) -> None:
    """The reference's compactor normalises every event's ticket id, including the
    `__sync__` watermark, and fails on it (an inherited limitation, tracked as a
    ticket). Both implementations must refuse the same way."""
    _exact(replay, "compact", "--force", "--archive-after", "0", "--json")
    _exact(replay, "compact", "--force")


def test_compacted_archive_reads_back(replay: Pair) -> None:
    for impl in replay.both:
        log = impl.rohrpost_dir / "log.jsonl"
        kept = [
            line
            for line in log.read_bytes().splitlines(keepends=True)
            if b'"op":"synced"' not in line
        ]
        log.write_bytes(b"".join(kept))
    _exact(replay, "compact", "--force", "--archive-after", "0", "--json")
    ref_archive = replay.reference.rohrpost_dir / "archive"
    nat_archive = replay.native.rohrpost_dir / "archive"
    names = sorted(p.name for p in ref_archive.iterdir())
    assert names, "the replayed log holds terminal tickets, so compaction must archive"
    assert sorted(p.name for p in nat_archive.iterdir()) == names
    assert (replay.native.rohrpost_dir / "log.jsonl").read_bytes() == (
        replay.reference.rohrpost_dir / "log.jsonl"
    ).read_bytes()
    for name in names:
        assert (nat_archive / name).read_bytes() == (ref_archive / name).read_bytes()
    _exact(replay, "list", "--json")
    _exact(replay, "doctor", "--json")
    _exact(replay, "log", "--json")
    _exact(replay, "compact", "--force", "--json")  # nothing left to archive
