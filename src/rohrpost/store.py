"""The append-only event log store: read, append, and lock ``log.jsonl``.

Spec §3 principle 1 — *the log is truth* — and principle 3 — *one write path* —
make this module the chokepoint for every mutation. All appends are serialised
under an exclusive lock on ``.rohrpost/.lock`` (:manpage:`fcntl` on POSIX, an
enforced ``msvcrt`` byte-range lock on Windows) and written with ``O_APPEND`` so
two processes (or two runners on separate branches after a union merge) can
never interleave half-lines. Spec §7 spells out the concurrency guarantees.

Reads are deliberately lock-free: the log is strictly append-only and every
event carries a unique id, so a reader may momentarily see a partial tail line
on a truly concurrent writer, which is handled by line-level decode (a partial
JSON line fails to decode and is skipped as a transient). Deduplication and
ordering belong to the fold (:mod:`rohrpost.fold`), not here.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

import msgspec

from rohrpost import paths
from rohrpost.events import Event, decode_line, encode
from rohrpost.exceptions import StoreError

# The platform seam: msvcrt only exists on Windows, fcntl only on POSIX.
if sys.platform == "win32":
    import msvcrt

    # Lock and unlock must name the identical byte range, so both helpers share it.
    _LOCK_BYTES = 1

    def _acquire(rohrpost_dir: Path, fh: TextIO) -> None:
        """Lock byte range [0, 1) exclusively; the CRT retries every 1 s, ~10 attempts."""
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, _LOCK_BYTES)
        except OSError as exc:
            # LK_LOCK exhaustion (EDEADLOCK) after ~10 s, or a lock not yet
            # released by a dead process: fail loudly instead of half-acquiring.
            raise StoreError(
                f"could not lock {rohrpost_dir} within the ~10s wait budget "
                f"(is another rp process holding it?): {exc}"
            ) from exc

    def _release(fh: TextIO) -> None:
        # Unlock must name the exact locked range, so seek back to its start.
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
else:
    import fcntl

    def _acquire(rohrpost_dir: Path, fh: TextIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _release(fh: TextIO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(rohrpost_dir: Path) -> Iterator[None]:
    """Exclusive lock on ``.rohrpost/.lock`` (held for the duration of the block).

    Used by appends and by the log-rewriting operations (compaction). Blocking
    waits for the current holder to release: unconditionally on POSIX, for the
    CRT's ~10 s retry budget on Windows (then :class:`StoreError`). Callers must
    not nest two ``file_lock`` calls on the same dir — nesting deadlocks on POSIX
    (each ``open()`` gets an independent open file description) and fails after
    the same ~10 s on Windows (an exclusive range lock cannot overlap itself
    through a second handle). The POSIX lock is advisory; the Windows byte-range
    lock is enforced — even plain I/O through a second handle fails inside the
    locked range.
    """
    lock = paths.lock_path(rohrpost_dir)
    # Open read+write so the file is created if missing, without truncating.
    fh = lock.open("a+", encoding="utf-8")
    locked = False
    try:
        # The Windows lock covers a byte range from the current position; pin it
        # to [0, 1), which may sit past EOF of the empty file (documented as
        # lockable). flock ignores the position, so this is a no-op there.
        fh.seek(0)
        _acquire(rohrpost_dir, fh)
        locked = True
        yield
    finally:
        # Unlocking a never-locked range raises on Windows and would mask the
        # StoreError from _acquire, so release only what was acquired.
        if locked:
            _release(fh)
        fh.close()


def append_event(rohrpost_dir: Path, event: Event) -> None:
    """Append one event as a single JSONL line under the advisory lock.

    The whole append happens inside :func:`file_lock` with the log opened
    ``O_APPEND``. On Linux a single ``O_APPEND`` write to a regular file is atomic
    at any size (spec §7), so the correctness argument rests on the append being
    *one* ``write()`` — hence ``os.write`` is used directly, never the buffered
    file object, which silently loops over short writes and could split the line.

    A short ``write()`` is fatal, not resumed: appending the remainder would be a
    second ``write()`` whose offset can interleave with another writer, collapsing
    the single-write atomicity §7 relies on. The partial bytes are rolled back so
    the log keeps no corrupt half-line, then :class:`StoreError` is raised.
    """
    line = encode(event) + b"\n"
    with file_lock(rohrpost_dir):
        log = paths.log_path(rohrpost_dir)
        # 0o644 matches the mode ``open("ab")`` would use; without it ``os.open``
        # defaults to 0o777 and (under a typical umask) creates an executable file.
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            written = os.write(fd, line)
            if written != len(line):
                # Roll back the partial bytes: a trailing half-line would otherwise
                # fail to decode on every future read (§3 principle 5).
                os.ftruncate(fd, os.fstat(fd).st_size - written)
                raise StoreError(
                    f"short write to {log}: wrote {written} of {len(line)} bytes; "
                    "appending the remainder would break single-write atomicity (§7)"
                )
            os_sync(fd)
        finally:
            os.close(fd)


def os_sync(fd: int) -> None:
    """Best-effort ``fsync`` of an open file descriptor.

    Factored out so durability can be dialed (fsync per append is expensive; the
    lock plus append-mode is what guarantees correctness, fsync only durability).
    Currently a no-op: the spec treats git as the backup tier, and committing is
    the caller's job, so per-append fsync is not worth the latency yet.
    """
    _ = fd  # reserved for os.fsync(fd) when we want durability


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
