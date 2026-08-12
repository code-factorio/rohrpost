#!/usr/bin/env python3
"""E3: measure how a cold full-fold's wall time scales with inline body size.

Decision experiment feeding signal D4 ("fold wall time > 50ms"). Spec §11 claims
a full fold is single-digit milliseconds; this script is where that claim is
checked under a controlled, synthetic load. The repo is the source of truth, so
the "fold" here is the real one: ``store.read_events`` (decode the JSONL log)
followed by ``fold.fold`` (dedupe, sort, replay) — the same work
``rohrpost.stats._median_cold_fold_ms`` times, hence the numbers are directly
comparable to ``rp stats``.

Why body size is the swept variable: with bodies inline in the JSONL line, the
JSON decode in ``read_events`` is linear in total body bytes — a fat-tailed body
size makes the fold expensive. A sidecar body scheme would have the fold carry
only a path, making fold cost independent of body size. A total-fold time that
grows with body size at the fat tail is therefore evidence *for* sidecars.

The script synthesizes ``--tickets`` tickets x ``--events-per-ticket`` events
for each body size, building events directly with ``Event(...)`` +
``rohrpost.events.encode`` (NOT via ``api.create_ticket`` — its per-call fold
would make 30 000 events impractically slow), writes the encoded lines to a
fresh ``.rohrpost/log.jsonl`` once, then times a cold read+fold ``--runs`` times
and reports the median. It is a standalone measurement script, not a pytest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rohrpost import paths, store
from rohrpost.events import Event, encode
from rohrpost.fold import fold
from rohrpost.ids import new_ticket_id, new_ulid

#: Fixed epoch base (2026-01-01 UTC) so timestamps are deterministic and strictly
#: increasing across the whole synthesis — the fold's ``(ts, id)`` order is then
#: total without ever consulting the ULID tiebreak. Mirrors tests/conftest.py.
_EPOCH_MS: int = 1_767_225_600_000

#: A representative actor; the namespace is load-bearing per spec §5.2 but its
#: exact value does not affect fold cost, only the bytes per line by a constant.
_ACTOR: str = "user/bench@example.com"


# ---------------------------------------------------------------------------
# Timing and row result types
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _FoldTiming:
    """Median cold-fold timings (ms) plus the sanity counts from the last run."""

    read_ms: float
    apply_ms: float
    total_ms: float
    events: int
    tickets: int


@dataclass(frozen=True, slots=True)
class _Row:
    """One reported table row: a single body size's measurement."""

    body_size: int
    tickets: int
    events: int
    log_bytes: int
    read_ms: float
    apply_ms: float
    total_ms: float
    target_ms: float | None
    target_label: str
    verdict: str


# ---------------------------------------------------------------------------
# Synthesis helpers (kept allocation-light; the encode/decode is the workload)
# ---------------------------------------------------------------------------
def _ms_to_ts(ms: int) -> str:
    """Render ms-since-epoch as an RFC 3339 UTC ms string (mirrors rohrpost.util)."""
    return (
        dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _make_body(size: int, seq: int) -> str:
    """Return a distinct ASCII body of exactly ``size`` bytes (``""`` if ``size <= 0``).

    A fixed-width counter prefix keeps every body a unique, exactly-sized string so
    the JSON decode in ``read_events`` cannot take any interning shortcut and the
    on-disk log size is a clean function of ``size``.
    """
    if size <= 0:
        return ""
    counter = f"{seq:010d}"  # 10 ASCII digits; seq < 1e10 for any realistic run
    if size <= len(counter):
        return "x" * size
    return counter + "x" * (size - len(counter))


def _make_ticket_ids(n: int) -> list[str]:
    """Return ``n`` distinct valid bare ticket ids (deduped; collision-proof)."""
    seen: set[str] = set()
    out: list[str] = []
    while len(out) < n:
        tid = new_ticket_id()
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _write_log(log: Path, ticket_ids: list[str], events_per_ticket: int, body_size: int) -> int:
    """Write one ``create`` + (``events_per_ticket`` - 1) ``set`` events per ticket.

    Every event carries a body of ``body_size`` bytes in its ``set`` payload, so the
    decode cost in ``read_events`` scales with body size — the signal isolated here.
    Returns the number of events written. Lines are streamed to the open file handle
    so peak memory is one event regardless of total log size.
    """
    written = 0
    seq = 0
    with log.open("wb") as fh:
        for t_index, tid in enumerate(ticket_ids):
            for i in range(events_per_ticket):
                ev = Event(
                    id=new_ulid(),
                    ts=_ms_to_ts(_EPOCH_MS + seq),
                    ticket=tid,
                    op="create" if i == 0 else "set",
                    actor=_ACTOR,
                    set={
                        "title": f"ticket-{t_index}",
                        "body": _make_body(body_size, seq),
                        "status": "open",
                    },
                )
                fh.write(encode(ev))
                fh.write(b"\n")
                seq += 1
                written += 1
    return written


def _time_cold_fold(rohrpost_dir: Path, runs: int) -> _FoldTiming:
    """Median read/apply/total ms over ``runs`` snapshot-bypassed read+fold passes.

    "Cold" means the snapshot cache is bypassed: this times ``read_events`` plus
    ``fold`` directly — the work a real ``rp`` invocation does when the snapshot is
    stale. Read and apply are timed separately so the body-size cost can be located
    (decode-dominated read vs. O(events) apply).
    """
    read_ms: list[float] = []
    apply_ms: list[float] = []
    total_ms: list[float] = []
    events = 0
    tickets = 0
    for _ in range(max(1, runs)):
        t0 = time.perf_counter()
        evs = store.read_events(rohrpost_dir)
        t1 = time.perf_counter()
        folded = fold(evs)
        t2 = time.perf_counter()
        read_ms.append((t1 - t0) * 1000.0)
        apply_ms.append((t2 - t1) * 1000.0)
        total_ms.append((t2 - t0) * 1000.0)
        events = len(evs)
        tickets = len(folded)
    return _FoldTiming(
        read_ms=statistics.median(read_ms),
        apply_ms=statistics.median(apply_ms),
        total_ms=statistics.median(total_ms),
        events=events,
        tickets=tickets,
    )


def _target_for(body_size: int) -> tuple[float | None, str]:
    """The spec §11 fold-time target for a body size, or ``None`` for the cliff probe.

    Buckets by size so the defaults map cleanly: 200 B -> the <10 ms target,
    2 KB -> the <50 ms target, 20 KB -> the unbounded "find the cliff" probe.
    """
    if body_size < 1000:
        return 10.0, "<10ms"
    if body_size < 10000:
        return 50.0, "<50ms"
    return None, "cliff"


def _human_bytes(num: int) -> str:
    """Render a byte count with a binary unit suffix (B / KiB / MiB / GiB / TiB)."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def _bench_one(
    tmpdir: Path, tickets: int, events_per_ticket: int, runs: int, body_size: int
) -> _Row:
    """Synthesize a log at one body size and time its cold fold."""
    rohrpost_dir = tmpdir / f"body-{body_size}b" / paths.ROHRPOST_DIR_NAME
    paths.ensure_layout(rohrpost_dir)
    log = paths.log_path(rohrpost_dir)

    ticket_ids = _make_ticket_ids(tickets)
    _write_log(log, ticket_ids, events_per_ticket, body_size)
    timing = _time_cold_fold(rohrpost_dir, runs)

    target_ms, target_label = _target_for(body_size)
    if target_ms is not None:
        verdict = "PASS" if timing.total_ms <= target_ms else "FAIL"
    else:
        verdict = "cliff"
    return _Row(
        body_size=body_size,
        tickets=timing.tickets,
        events=timing.events,
        log_bytes=log.stat().st_size,
        read_ms=timing.read_ms,
        apply_ms=timing.apply_ms,
        total_ms=timing.total_ms,
        target_ms=target_ms,
        target_label=target_label,
        verdict=verdict,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _print_table(rows: list[_Row]) -> None:
    """Print the aligned fixed-width results table."""
    header = (
        f"{'body(B)':>8}  {'tickets':>8}  {'events':>8}  {'log size':>10}  "
        f"{'read ms':>9}  {'apply ms':>9}  {'total ms':>9}  {'target':>7}  {'verdict':>7}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r.body_size:>8}  {r.tickets:>8}  {r.events:>8}  "
            f"{_human_bytes(r.log_bytes):>10}  {r.read_ms:>9.2f}  {r.apply_ms:>9.2f}  "
            f"{r.total_ms:>9.2f}  {r.target_label:>7}  {r.verdict:>7}"
        )


def _print_markdown(rows: list[_Row]) -> None:
    """Print a second, markdown-formatted copy of the table (for pasting into notes)."""
    print()
    print(
        "| body (B) | tickets | events | log size | read (ms) | apply (ms) "
        "| total (ms) | target | verdict |"
    )
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        print(
            f"| {r.body_size} | {r.tickets} | {r.events} | {_human_bytes(r.log_bytes)} "
            f"| {r.read_ms:.2f} | {r.apply_ms:.2f} | {r.total_ms:.2f} "
            f"| {r.target_label} | {r.verdict} |"
        )


def _parse_bodies(raw: str) -> list[int]:
    """Parse a comma-separated list of non-negative body byte sizes."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    try:
        values = [int(p) for p in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--bodies must be comma-separated ints: {raw!r}") from exc
    if not values or any(v < 0 for v in values):
        raise argparse.ArgumentTypeError(f"--bodies must be non-empty and non-negative: {raw!r}")
    return values


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Parse args, run the sweep, print the table. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description=(
            "E3: measure how a cold full-fold's wall time scales with inline body "
            "size (decision signal D4)."
        )
    )
    parser.add_argument(
        "--tickets", type=int, default=3000, help="tickets per body size (default 3000)"
    )
    parser.add_argument(
        "--events-per-ticket", type=int, default=10, help="events per ticket (default 10)"
    )
    parser.add_argument(
        "--runs", type=int, default=5, help="cold-fold samples to median over (default 5)"
    )
    parser.add_argument(
        "--bodies",
        type=_parse_bodies,
        default=[200, 2000, 20000],
        help="comma-separated body byte sizes (default 200,2000,20000)",
    )
    parser.add_argument(
        "--tmpdir",
        type=str,
        default=None,
        help="working dir (default: a fresh mkdtemp, left in place with its path printed)",
    )
    parser.add_argument("--markdown", action="store_true", help="also print a markdown table")
    args = parser.parse_args(argv)

    if args.tickets < 1 or args.events_per_ticket < 1 or args.runs < 1:
        parser.error("--tickets, --events-per-ticket and --runs must each be >= 1")

    tmpdir = Path(args.tmpdir) if args.tmpdir else Path(tempfile.mkdtemp(prefix="rp-bench-fold-"))
    tmpdir.mkdir(parents=True, exist_ok=True)

    print("E3 bench: cold full-fold cost vs inline body size")
    print(
        f"  tickets={args.tickets}  events/ticket={args.events_per_ticket}  "
        f"runs={args.runs}  bodies={args.bodies}"
    )
    print(f"  tmpdir={tmpdir}")
    print(
        "  each event carries a body of the row's size; total ms = read_events + fold"
        " (snapshot bypassed)."
    )
    print(
        "  targets are the spec §11 reference (<10 ms @ ~200 B, <50 ms @ ~2 KB); "
        "20 KB is an unbounded cliff probe."
    )
    print()

    rows: list[_Row] = []
    expected_events = args.tickets * args.events_per_ticket
    for body_size in args.bodies:
        row = _bench_one(tmpdir, args.tickets, args.events_per_ticket, args.runs, body_size)
        rows.append(row)
        # Loud warning if counts are off: the timings are meaningless if ids
        # collided or the write came up short.
        if row.events != expected_events or row.tickets != args.tickets:
            print(
                f"  ! warning: body={body_size} read {row.events} events / "
                f"{row.tickets} tickets (expected {expected_events} / {args.tickets})",
                file=sys.stderr,
            )

    _print_table(rows)
    if args.markdown:
        _print_markdown(rows)

    if len(rows) >= 2:
        small, large = rows[0], rows[-1]
        ratio = large.total_ms / small.total_ms if small.total_ms else float("inf")
        print(
            f"\nscaling: total fold grew {ratio:.1f}x from {small.body_size} B "
            f"to {large.body_size} B ({small.total_ms:.2f} -> {large.total_ms:.2f} ms)."
        )

    print(f"\nlog dirs left in place at: {tmpdir}")
    print(
        "D4 signal: compare 'total ms' against <10 ms / <50 ms; a body-size-driven "
        "rise is evidence for sidecar bodies."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
