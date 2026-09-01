"""Focused tests for :mod:`rohrpost.store` — append, read, lock, dedup-readiness."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from rohrpost import paths, store
from rohrpost.events import Event
from rohrpost.exceptions import StoreError


def _ev(eid: str = "01K2X8P4RQ7YFZ3M9NVB6TDHWC", ticket: str = "a1b2c3") -> Event:
    return Event(
        id=eid,
        ts="2026-08-11T09:00:00.000Z",
        ticket=ticket,
        op="set",
        actor="user/x",
        set={"status": "open"},
    )


def test_append_then_read_round_trips(tmp_repo: Path) -> None:
    store.append_event(tmp_repo, _ev())
    events = store.read_events(tmp_repo)
    assert len(events) == 1
    assert events[0].ticket == "a1b2c3"


def test_read_on_empty_log_returns_nothing(tmp_repo: Path) -> None:
    assert store.read_events(tmp_repo) == []


def test_read_raises_on_malformed_line(tmp_repo: Path) -> None:
    paths.log_path(tmp_repo).write_text("not json\n")
    with pytest.raises(StoreError):
        store.read_events(tmp_repo)


def test_read_lenient_collects_errors(tmp_repo: Path) -> None:
    log = paths.log_path(tmp_repo)
    log.write_text(
        '{"id":"x","ts":"t","ticket":"a1b2c3","op":"set","actor":"a","set":{"status":"open"}}\n'
        "garbage line\n"
    )
    events, errors = store.read_events_lenient(tmp_repo)
    assert len(events) == 1
    assert len(errors) == 1


def test_blank_lines_ignored(tmp_repo: Path) -> None:
    log = paths.log_path(tmp_repo)
    log.write_text("\n   \n")
    assert store.read_events(tmp_repo) == []


def test_archive_read_before_live_log(tmp_repo: Path) -> None:
    from rohrpost.events import encode

    archived = _ev("01K2X8P4RQ7YFZ3M9NVB6TDHW" + "A", ticket="aaaaaa")
    live = _ev("01K2X8P4RQ7YFZ3M9NVB6TDHW" + "B", ticket="bbbbbb")
    paths.archive_dir(tmp_repo).mkdir(parents=True, exist_ok=True)
    (paths.archive_dir(tmp_repo) / "log-2025-Q4.jsonl").write_text(encode(archived).decode() + "\n")
    store.append_event(tmp_repo, live)
    events = store.read_events(tmp_repo)
    assert [e.ticket for e in events] == ["aaaaaa", "bbbbbb"]


def test_concurrent_appends_do_not_lose_lines(tmp_repo: Path) -> None:
    """Many threads appending distinct events under the lock: all survive."""
    from rohrpost.events import encode

    n_threads, n_each = 8, 25

    def writer(thread_id: int) -> None:
        for i in range(n_each):
            ev = Event(
                id=f"01K2X8P4RQ7YFZ3M9NVB6TDH{thread_id:02d}{i:022d}"[:26],
                ts="2026-08-11T09:00:00.000Z",
                ticket="a1b2c3",
                op="comment",
                actor="user/x",
                text=f"t{thread_id}-{i}",
            )
            store.append_event(tmp_repo, ev)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = store.read_events(tmp_repo)
    assert len(events) == n_threads * n_each
    _ = encode  # keep the import meaningful for the codec used implicitly above


def test_file_lock_releases_on_exception(tmp_repo: Path) -> None:
    """The lock is released when the body raises — a leak would deadlock the
    next acquisition (POSIX blocks forever, Windows errors after ~10 s)."""
    with pytest.raises(RuntimeError, match="boom"), store.file_lock(tmp_repo):
        raise RuntimeError("boom")
    # The released lock can be acquired again immediately.
    with store.file_lock(tmp_repo):
        pass


def test_lock_file_created_on_first_mutation_and_kept(tmp_repo: Path) -> None:
    """``.lock`` appears on first mutation and is never deleted: file existence
    is not lock state, so nothing may remove or write to the file."""
    lock = paths.lock_path(tmp_repo)
    assert not lock.exists()
    store.append_event(tmp_repo, _ev())
    assert lock.exists()
    assert lock.read_bytes() == b""
    store.append_event(tmp_repo, _ev("01K2X8P4RQ7YFZ3M9NVB6TDHWX"))
    assert lock.exists()


def test_event_count_counts_non_blank_lines(tmp_repo: Path) -> None:
    store.append_event(tmp_repo, _ev())
    store.append_event(tmp_repo, _ev("01K2X8P4RQ7YFZ3M9NVB6TDHWX"))
    assert store.event_count(tmp_repo) == 2


def test_append_raises_on_short_write_and_rolls_back(
    tmp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short ``write()`` is fatal, not resumed; the partial line is rolled back.

    Resuming a short write would append the remainder in a *second* ``write()`` whose
    offset can interleave with another writer — collapsing the single-write atomicity
    spec §7 relies on. The rolled-back tail keeps the log decodable (§3 principle 5).
    """
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:1])  # persist one byte, report a short count

    monkeypatch.setattr(os, "write", short_write)

    with pytest.raises(StoreError):
        store.append_event(tmp_repo, _ev())

    # The single partial byte was rolled back: the log is empty and still decodable.
    assert store.read_events(tmp_repo) == []
