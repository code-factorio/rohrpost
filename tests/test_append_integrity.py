"""Spec §7 append-path concurrency guarantees across the ``PIPE_BUF`` boundary.

These are decision experiments **E1** and **E2** from the §13.1 inline-vs-sidecar
decision note. Spec §7 promises that the advisory ``fcntl`` lock in
:func:`rohrpost.store.append_event`, combined with ``O_APPEND``, makes concurrent
appends non-interleaving: one JSONL line per event, every line decodable, every
event id distinct — at any body size.

* **E1** (always-on regression) proves that promise by hammering the live log from
  several *processes* (true cross-process concurrency, mirroring separate ``rp``
  invocations) with bodies sized from empty to 64 KB — straddling ``PIPE_BUF`` —
  then reading the whole log back. This holds regardless of whether bodies stay
  inline or move to sidecars, so the file is worth keeping either way.

* **E2** (opt-in ``@pytest.mark.experiment``) removes the lock to test whether it
  is load-bearing. It runs in *threads* so the in-process monkeypatch reaches the
  writers. Per the doc the prediction is: below ``PIPE_BUF`` the single
  ``write()`` is kernel-atomic so the log stays clean even without the lock;
  above ``PIPE_BUF`` the buffered writer can interleave, which is the race the
  lock closes. That above-``PIPE_BUF`` race is scheduler- and filesystem-
  dependent, so E2 only runs under ``ROHRPOST_RUN_EXPERIMENTS=1`` — run it
  manually to read the verdict on this kernel/filesystem (the observed finding is
  recorded in the test body).
"""

from __future__ import annotations

import multiprocessing
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

from rohrpost import store
from rohrpost.events import Event, encode
from rohrpost.stats import pipe_buf

_TS: Final[str] = "2026-08-11T09:00:00.000Z"
_TICKET: Final[str] = "a1b2c3"

# E1 workload: a few processes, each appending many events.
_E1_WRITERS: Final[int] = 4
_E1_EACH: Final[int] = 16

# True cross-process isolation per platform. POSIX uses "fork": 3.14 defaults to
# forkserver, which re-imports modules in a spawn helper that cannot see this
# test module, while fork inherits the worker as-is. Windows has no fork at all;
# "spawn" re-imports the module in the child, which pytest's sys.path insertion
# makes importable — and spawned children exercise the real Windows locking seam.
_MP_CONTEXT: Final[str] = "spawn" if sys.platform == "win32" else "fork"

# E2 workload: more threads for stronger race pressure on the large body.
_E2_WRITERS: Final[int] = 8
_E2_EACH: Final[int] = 20


def _append_worker(repo_dir: str, writer_id: int, count: int, body: str) -> int:
    """Append ``count`` distinct ``set`` events from one writer; returns ``count``.

    Top-level (not a closure) so it pickles cleanly for ``ProcessPoolExecutor``.
    The id embeds the writer id and index, so every id is globally distinct
    without leaning on ULID randomness. It calls the real
    :func:`rohrpost.store.append_event` — the lock + ``O_APPEND`` path under test.
    """
    rohrpost_dir = Path(repo_dir)
    for i in range(count):
        # 3-digit writer prefix + 23-digit index -> a 26-char, globally-unique id.
        eid = f"{writer_id:03d}{i:023d}"
        event = Event(
            id=eid,
            ts=_TS,
            ticket=_TICKET,
            op="set",
            actor=f"writer/{writer_id}",
            set={"body": body},
        )
        store.append_event(rohrpost_dir, event)
    return count


def _resolve_body_size(category: str, buf: int) -> int:
    """Map a symbolic size category to a byte length resolved against ``buf``.

    Resolving at call time (rather than baking in 4096) keeps the parametrized
    case names accurate on any filesystem. ``empty`` is the empty-body case; the
    rest straddle ``PIPE_BUF`` from well below to well above.
    """
    table: dict[str, int] = {
        "empty": 0,
        "tiny": 512,
        "below_buf": buf - 96,
        "at_buf": buf,
        "above_buf": buf + 1,
        "large": 16_384,
        "huge": 65_536,
    }
    return table[category]


# ---------------------------------------------------------------------------
# E1 — concurrent appends (true processes) preserve integrity at every size.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "category",
    ["empty", "tiny", "below_buf", "at_buf", "above_buf", "large", "huge"],
)
def test_e1_concurrent_appends_preserve_integrity_across_pipe_buf(
    tmp_repo: Path,
    category: str,
) -> None:
    """Lock-protected concurrent appends never interleave, at any body size."""
    buf = pipe_buf(tmp_repo)
    body_size = _resolve_body_size(category, buf)
    if body_size < 0:  # defensive: the POSIX floor (512) means this never triggers
        pytest.skip(f"negative body size for category={category}, buf={buf}")
    body = "x" * body_size
    expected = _E1_WRITERS * _E1_EACH

    with ProcessPoolExecutor(
        max_workers=_E1_WRITERS, mp_context=multiprocessing.get_context(_MP_CONTEXT)
    ) as pool:
        futures = [
            pool.submit(_append_worker, str(tmp_repo), w, _E1_EACH, body)
            for w in range(_E1_WRITERS)
        ]
        appended = [f.result() for f in futures]
    assert sum(appended) == expected

    events = store.read_events(tmp_repo)  # raises StoreError on any malformed line
    assert len(events) == expected  # (a) no lines lost or doubled
    assert len({e.id for e in events}) == expected  # (c) every id distinct
    assert all(e.set == {"body": body} for e in events)  # bodies survive byte-exact


# ---------------------------------------------------------------------------
# E2 — the lock is load-bearing (opt-in: genuine race, scheduler-dependent).
# ---------------------------------------------------------------------------
@contextmanager
def _noop_lock(_rohrpost_dir: Path) -> Iterator[None]:
    """A no-op stand-in for :func:`store.file_lock` that holds no lock at all."""
    yield


@pytest.mark.experiment
def test_e2_lock_removed_below_pipe_buf_stays_clean(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sub-``PIPE_BUF`` writes stay atomic without the lock (kernel guarantee).

    A single ``write()`` at or below ``PIPE_BUF`` is atomic by POSIX, so removing
    the advisory lock must not corrupt a log whose lines all fit under it. This is
    the deterministic half of the doc's prediction and the reason the hazard lives
    only at large sizes.
    """
    buf = pipe_buf(tmp_repo)
    # Leave comfortable envelope headroom so the whole JSONL line stays under buf.
    body = "y" * max(1, buf - 512)
    sample_line = (
        len(
            encode(
                Event(
                    id="0" * 26,
                    ts=_TS,
                    ticket=_TICKET,
                    op="set",
                    actor="writer/0",
                    set={"body": body},
                )
            )
        )
        + 1
    )
    assert sample_line <= buf  # machine-check the premise this test rests on

    monkeypatch.setattr(store, "file_lock", _noop_lock)
    expected = _E2_WRITERS * _E2_EACH
    threads = [
        threading.Thread(target=_append_worker, args=(str(tmp_repo), w, _E2_EACH, body))
        for w in range(_E2_WRITERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = store.read_events(tmp_repo)  # raises StoreError on any malformed line
    assert len(events) == expected
    assert len({e.id for e in events}) == expected
    assert all(e.set == {"body": body} for e in events)


@pytest.mark.experiment
def test_e2_lock_removed_above_pipe_buf_exposes_race(
    tmp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above ``PIPE_BUF`` with the lock removed: probe for the interleaving race.

    The doc predicts corruption (a bad line or a vanished id) once a line exceeds
    ``PIPE_BUF`` without the advisory lock. This is a genuine race, so it is opt-in;
    the always-true invariant (no fabricated ``'\\n'`` -> at most ``expected`` lines)
    is asserted unconditionally, and the ``corrupted`` flag records the observed
    verdict for this kernel/filesystem.

    FINDING on Linux 7.x / tmpfs (``PIPE_BUF`` = 4096), confirmed across repeated
    runs: the lock is *not* load-bearing for integrity here. Removing it and having
    eight threads append 64 KB lines still yields every line decodable, every id
    distinct, no losses. The mechanism: ``store.append_event`` writes each whole
    JSONL line with a single ``os.write`` (Python's ``BufferedWriter`` issues one
    syscall per flush), and ``O_APPEND`` reserves the file offset atomically per
    ``write()`` on a local regular file — so each line lands contiguously at EOF
    regardless of size. ``PIPE_BUF``'s atomicity ceiling governs pipes/FIFOs, not
    ``O_APPEND`` regular-file writes. The lock remains worthwhile as portability
    cover (filesystems where ``O_APPEND`` atomicity is weaker) and for any future
    append path that splits a line across several ``write`` calls; this canary will
    flag the latter should it ever appear.
    """
    body = "z" * 65_536
    expected = _E2_WRITERS * _E2_EACH
    monkeypatch.setattr(store, "file_lock", _noop_lock)

    threads = [
        threading.Thread(target=_append_worker, args=(str(tmp_repo), w, _E2_EACH, body))
        for w in range(_E2_WRITERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events, errors = store.read_events_lenient(tmp_repo)
    ids = {e.id for e in events}
    # Invariant: interleaving never fabricates new '\n' bytes, so the line count
    # can only fall short of (or equal) the number written — never exceed it.
    assert len(events) + len(errors) <= expected
    corrupted = bool(errors) or len(ids) < len(events) or (len(events) + len(errors)) < expected
    # OBSERVED on this kernel/filesystem: clean (see the finding above). Should a
    # platform or append-path change ever make the race bite, this assertion flips;
    # do not weaken it to pass — investigate and record the new verdict instead.
    assert not corrupted
    assert errors == []
    assert len(events) == expected
    assert len(ids) == expected
    assert all(e.set == {"body": body} for e in events)
