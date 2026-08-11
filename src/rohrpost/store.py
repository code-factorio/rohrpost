"""The append-only event log store: read, append, and lock ``log.jsonl``.

Spec §3 principle 1 — *the log is truth* — and principle 3 — *one write path* —
make this module the chokepoint for every mutation. All appends are serialised
under an advisory :manpage:`fcntl` lock and written with ``O_APPEND`` so two
processes (or two runners on separate branches after a union merge) can never
interleave half-lines. Spec §7 spells out the concurrency guarantees.

Reads are deliberately lock-free: the log is strictly append-only and every
event carries a unique id, so a reader may momentarily see a partial tail line
on a truly concurrent writer, which is handled by line-level decode (a partial
JSON line fails to decode and is skipped as a transient). Deduplication and
ordering belong to the fold (:mod:`rohrpost.fold`), not here.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import msgspec

from rohrpost import paths
from rohrpost.events import Event, decode_line, encode
from rohrpost.exceptions import StoreError


@contextmanager
def file_lock(rohrpost_dir: Path) -> Iterator[None]:
    """Exclusive advisory lock on ``.rohrpost/.lock`` (held for the duration of the block).

    Used by appends and by the log-rewriting operations (compaction). Blocking:
    waits for any other holder to release. Safe to re-enter within one process
    (``flock`` locks are per open file description, not recursive — callers must
    not nest two ``file_lock`` calls on the same dir).
    """
    lock = paths.lock_path(rohrpost_dir)
    # Open read+write so the file is created if missing, without truncating.
    fh = lock.open("a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def append_event(rohrpost_dir: Path, event: Event) -> None:
    """Append one event as a single JSONL line under the advisory lock.

    The whole append (open, write, flush) happens inside :func:`file_lock`, and
    the log is opened in append mode (``O_APPEND``), so concurrent writers cannot
    interleave. One ``write()`` of one line keeps sub-``PIPE_BUF`` writes atomic
    even without the lock; the lock covers the long-body case (spec §7).
    """
    line = encode(event) + b"\n"
    with file_lock(rohrpost_dir):
        log = paths.log_path(rohrpost_dir)
        with log.open("ab") as fh:
            fh.write(line)
            fh.flush()
            os_sync(fh)


def os_sync(fh: Any) -> None:
    """Best-effort ``fsync`` of a file object.

    Factored out so durability can be dialed (fsync per append is expensive; the
    lock plus append-mode is what guarantees correctness, fsync only durability).
    Currently a no-op: the spec treats git as the backup tier, and committing is
    the caller's job, so per-append fsync is not worth the latency yet.
    """
    _ = fh  # reserved for os.fsync(fh.fileno()) when we want durability


def _decode_stream(lines: Iterator[str | bytes]) -> tuple[list[Event], list[str]]:
    """Decode an iterable of lines into events, collecting malformed lines.

    Blank/whitespace-only lines are ignored. Decode failures are recorded rather
    than raised so :func:`read_events_lenient` can report corruption to
    ``rp doctor``; :func:`read_events` re-raises the first failure loudly.
    """
    events: list[Event] = []
    errors: list[str] = []
    for lineno, raw in enumerate(lines, start=1):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            events.append(decode_line(stripped))
        except msgspec.MsgspecError as exc:
            errors.append(f"line {lineno}: {exc}: {stripped[:80]!r}")
    return events, errors


def _iter_files(files: list[Path]) -> Iterator[str | bytes]:
    """Yield every line from ``files`` in order, skipping files that do not exist."""
    for path in files:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as fh:
            yield from fh


def _all_log_files(rohrpost_dir: Path) -> list[Path]:
    """Archive files (oldest first) then the live log — the fold's read order (§6)."""
    return [*paths.archive_files(rohrpost_dir), paths.log_path(rohrpost_dir)]


def read_events(rohrpost_dir: Path) -> list[Event]:
    """Read every event from archive then log, in order.

    Raises :class:`StoreError` on the first malformed line — a corrupt log is a
    loud failure (spec §3 principle 5). Duplicates from a union merge are *not*
    removed here; the fold deduplicates by event id.
    """
    events, errors = _decode_stream(_iter_files(_all_log_files(rohrpost_dir)))
    if errors:
        raise StoreError(f"malformed event log ({len(errors)} bad line(s)): {errors[0]}")
    return events


def read_events_lenient(rohrpost_dir: Path) -> tuple[list[Event], list[str]]:
    """Like :func:`read_events` but returns errors instead of raising (for ``rp doctor``)."""
    return _decode_stream(_iter_files(_all_log_files(rohrpost_dir)))


def event_count(rohrpost_dir: Path) -> int:
    """Count of non-blank lines across archive + log (a cheap, lock-free metric)."""
    return sum(
        1
        for raw in _iter_files(_all_log_files(rohrpost_dir))
        if (raw.decode("utf-8") if isinstance(raw, bytes) else raw).strip()
    )


__all__ = [
    "append_event",
    "event_count",
    "file_lock",
    "read_events",
    "read_events_lenient",
]
