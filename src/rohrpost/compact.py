"""``rp compact`` — archive terminal tickets' events and truncate the live log.

Spec §6.1. Compaction is the *one* operation that rewrites a union-merged file
(``log.jsonl``) rather than appending to it, so it is the one operation that can
lose data if run carelessly. The guard is strict: it refuses unless the working
tree is clean and ``HEAD`` is the configured default branch, unless ``--force``
is given.

Algorithm:

1. Fold everything.
2. Move every event of a ticket terminal (``done``/``dropped``) for more than
   ``archive_after`` days into ``archive/log-<YYYY>-Q<N>.jsonl`` (bucketed by
   the event's own timestamp).
3. Rewrite ``log.jsonl`` with the remainder.

Runs under the advisory lock so no appender races the rewrite. Outside a git
repo the branch/dirty guard is skipped (there is nothing to protect).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rohrpost import events, paths, store
from rohrpost.exceptions import StoreError
from rohrpost.fold import TERMINAL, Ticket, fold_all

#: Default retention before terminal-ticket events are archived.
DEFAULT_ARCHIVE_AFTER_DAYS: int = 90


@dataclass(frozen=True, slots=True)
class CompactResult:
    """What compaction did, for both human and ``--json`` reporting."""

    archived: int
    remaining: int
    archive_files: list[str]

    def to_mapping(self) -> dict[str, object]:
        return {
            "archived": self.archived,
            "remaining": self.remaining,
            "archive_files": self.archive_files,
        }


def _parse_ts(ts: str) -> datetime:
    """Parse an RFC 3339 timestamp (with trailing ``Z``) into an aware UTC datetime."""
    cleaned = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)


def _quarter_bucket(ts: str) -> str:
    """Return the ``log-<YYYY>-Q<N>`` bucket name for an event timestamp."""
    dt = _parse_ts(ts)
    quarter = (dt.month - 1) // 3 + 1
    return f"log-{dt.year}-Q{quarter}.jsonl"


def _default_branch() -> str:
    return "main"


def _git_state(repo_root: Path) -> tuple[bool, str]:
    """Return ``(is_dirty, current_branch)``. Best-effort; ``(False, "")`` without git."""
    try:
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(repo_root), "status", "--porcelain"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        )
        branch = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except FileNotFoundError:
        return False, ""
    except subprocess.TimeoutExpired:
        return False, ""
    return dirty, branch


def _guard(repo_root: Path, force: bool) -> str | None:
    """Return a refusal reason if compaction must not proceed, else ``None``."""
    if force:
        return None
    dirty, branch = _git_state(repo_root)
    if not branch:  # outside git — nothing to protect
        return None
    if dirty:
        return "refusing to compact: working tree is dirty (use --force to override)"
    if branch != _default_branch():
        return (
            f"refusing to compact: HEAD is on {branch!r}, not {_default_branch()!r} "
            f"(use --force to override)"
        )
    return None


def _terminal_events_for(events_all: list[events.Event], tid: str) -> list[events.Event]:
    """The terminal-status (``set status=done|dropped``) events for one ticket."""
    return [
        e
        for e in events_all
        if e.op == "set" and e.set and e.set.get("status") in TERMINAL and _ticket_of(e) == tid
    ]


def _archivable_ids(
    events_all: list[events.Event], by_id: dict[str, Ticket], cutoff: datetime
) -> set[str]:
    """Ticket ids terminal (done/dropped) since before ``cutoff``."""
    archivable: set[str] = set()
    for tid, ticket in by_id.items():
        if ticket.status not in TERMINAL:
            continue
        terminal = _terminal_events_for(events_all, tid)
        if terminal and max(_parse_ts(e.ts) for e in terminal) < cutoff:
            archivable.add(tid)
    return archivable


def _partition(
    events_all: list[events.Event], archivable: set[str]
) -> tuple[list[events.Event], dict[str, list[events.Event]]]:
    """Split events into (keep, {archive_bucket: events}) by ticket archivability."""
    keep: list[events.Event] = []
    archive_buckets: dict[str, list[events.Event]] = {}
    for ev in events_all:
        if _ticket_of(ev) in archivable:
            archive_buckets.setdefault(_quarter_bucket(ev.ts), []).append(ev)
        else:
            keep.append(ev)
    return keep, archive_buckets


def run(
    rohrpost_dir: Path,
    *,
    archive_after_days: int = DEFAULT_ARCHIVE_AFTER_DAYS,
    force: bool = False,
    json_output: bool = False,
    now: datetime | None = None,
) -> int:
    """Run compaction. Returns process exit code (0 success, 1 refused/error)."""
    refusal = _guard(rohrpost_dir.parent, force)
    if refusal is not None:
        _fail(refusal, json_output)
        return 1

    cutoff = (now or datetime.now(UTC)) - timedelta(days=archive_after_days)
    events_all = store.read_events(rohrpost_dir)
    by_id = fold_all(rohrpost_dir)
    archivable = _archivable_ids(events_all, by_id, cutoff)
    keep, archive_buckets = _partition(events_all, archivable)

    with store.file_lock(rohrpost_dir):
        _rewrite_log(rohrpost_dir, keep)
        for bucket, evs in archive_buckets.items():
            _append_archive(rohrpost_dir, bucket, evs)

    _invalidate_snapshot(rohrpost_dir)

    result = CompactResult(
        archived=sum(len(evs) for evs in archive_buckets.values()),
        remaining=len(keep),
        archive_files=sorted(archive_buckets),
    )
    _report(result, json_output)
    return 0


def _invalidate_snapshot(rohrpost_dir: Path) -> None:
    """Drop the stale snapshot so the next read regenerates it (best-effort)."""
    snap = paths.snapshot_path(rohrpost_dir)
    if snap.is_file():
        with contextlib.suppress(OSError):
            snap.unlink()


def _report(result: CompactResult, json_output: bool) -> None:
    if json_output:
        json.dump(result.to_mapping(), sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)
        return
    print(f"Compacted: archived {result.archived} event(s), kept {result.remaining}.")
    if result.archive_files:
        print("  archive files: " + ", ".join(result.archive_files))


def _ticket_of(ev: events.Event) -> str:
    from rohrpost.ids import normalize_id

    try:
        return normalize_id(ev.ticket)
    except StoreError:
        return ev.ticket


def _rewrite_log(rohrpost_dir: Path, keep: list[events.Event]) -> None:
    """Atomically replace ``log.jsonl`` with the kept events, preserving (ts,id) order."""
    keep.sort(key=lambda e: (e.ts, e.id))
    log = paths.log_path(rohrpost_dir)
    payload = b"".join(events.encode(e) + b"\n" for e in keep)
    tmp = log.with_suffix(".jsonl.tmp")
    tmp.write_bytes(payload)
    tmp.replace(log)


def _append_archive(rohrpost_dir: Path, bucket: str, evs: list[events.Event]) -> None:
    """Append archived events to their quarter bucket (dedup-safe; sorted on read)."""
    adir = paths.archive_dir(rohrpost_dir)
    adir.mkdir(parents=True, exist_ok=True)
    target = adir / bucket
    evs.sort(key=lambda e: (e.ts, e.id))
    with target.open("ab") as fh:
        for ev in evs:
            fh.write(events.encode(ev) + b"\n")


def _fail(message: str, json_output: bool) -> None:
    if json_output:
        json.dump({"error": message}, sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)
    else:
        print(f"rp compact: {message}", file=sys.stderr)


__all__ = ["DEFAULT_ARCHIVE_AFTER_DAYS", "CompactResult", "run"]
