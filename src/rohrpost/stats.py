"""On-demand repository statistics — the instrumentation for spec §13.1.

The open question is whether ticket bodies stay inline in the JSONL event line or
move to per-ticket sidecar files. The decision rule (see the decision note) turns
on measurable signals: the body/line byte distributions, the share of appends
whose single JSONL line exceeds ``PIPE_BUF`` (the ones the advisory lock exists
to protect), and the cost of a cold fold at the observed sizes.

Every signal here is **derived straight from the event log**. There are no
hot-path counters in :mod:`rohrpost.store` to keep in step with the append path:
line length is ``len(encode(event)) + 1``, a body's size is the UTF-8 length of
its string, and "over ``PIPE_BUF``" is a comparison against ``os.pathconf``'s
``PC_PIPE_BUF``. That makes the instrumentation free at append time *and*
retroactive — a log written before this command existed is measured exactly as
well as a fresh one. Only the fold timing (``fold_ms``) is a live measurement,
because wall time cannot be reconstructed from the log.

The output shape matches the decision note's ``rp stats --json`` contract so the
D1-D4 thresholds can be read off it directly.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

from rohrpost import store
from rohrpost.events import encode
from rohrpost.fold import fold

#: Percentile points reported for every byte distribution.
_PERCENTILE_POINTS: tuple[int, ...] = (50, 90, 95, 99)


def pipe_buf(path: Path | None = None) -> int:
    """The kernel's guaranteed-atomic pipe-write size for ``path``'s filesystem.

    A single ``write()`` of one JSONL line at or below this size is atomic at the
    OS level; a longer line (a long inline body) is what the advisory lock in
    :func:`rohrpost.store.append_event` exists to protect. Sidecar bodies would
    remove that hazard entirely, which is the whole point of the §13.1 question.

    POSIX guarantees at least 512; Linux reports 4096 everywhere. Windows has no
    ``PC_PIPE_BUF`` at all, so the same 4096 default stands in there as the
    heuristic the decision thresholds are calibrated against. ``path``
    defaults to the current directory. Tests should pass their temp repo so the
    value is resolved against the same filesystem the log lives on.
    """
    target = str(path) if path is not None else "."
    if sys.platform == "win32":
        return 4096  # no os.pathconf on Windows; see docstring
    try:
        return os.pathconf(target, "PC_PIPE_BUF")
    except OSError, ValueError:
        return 4096  # Linux default; the POSIX floor is 512.


def _percentile(sorted_samples: list[int], point: float) -> int:
    """Linear-interpolation percentile of an already-sorted, non-empty sample list.

    The decision thresholds (1 000 and 4 000 bytes) are coarse enough that the
    exact percentile method never changes the verdict; interpolation is used only
    so adjacent samples do not jump in confusing steps.
    """
    if not sorted_samples:
        return 0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    rank = point / 100.0 * (len(sorted_samples) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_samples) - 1)
    frac = rank - lo
    return round(sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac)


def _distribution(samples: list[int]) -> dict[str, object]:
    """Summarise a byte-sample list as ``{p50, p90, p95, p99, max, count}``."""
    if not samples:
        return {"p50": 0, "p90": 0, "p95": 0, "p99": 0, "max": 0, "count": 0}
    ordered = sorted(samples)
    dist: dict[str, object] = {
        f"p{point}": _percentile(ordered, point) for point in _PERCENTILE_POINTS
    }
    dist["max"] = ordered[-1]
    dist["count"] = len(ordered)
    return dist


def _median_cold_fold_ms(rohrpost_dir: Path, runs: int) -> float:
    """Median wall-clock milliseconds for a cold read + fold of the whole log.

    "Cold" means the snapshot cache is bypassed — we time :func:`store.read_events`
    plus :func:`fold` directly, which is the work a real ``rp`` invocation does
    when the snapshot is stale. A handful of runs are averaged because a single
    timing is dominated by scheduling noise.
    """
    timings_ms: list[float] = []
    for _ in range(max(1, runs)):
        start = time.perf_counter()
        fold(store.read_events(rohrpost_dir))
        timings_ms.append((time.perf_counter() - start) * 1000.0)
    return round(statistics.median(timings_ms), 3)


def compute_stats(rohrpost_dir: Path, *, fold_runs: int = 5) -> dict[str, object]:
    """Compute the §13.1 decision signals from the live log.

    Returns a mapping with the keys the decision note's ``rp stats --json``
    contract names: ``tickets``, ``events``, ``pipe_buf``, ``body_bytes`` (a
    distribution), ``event_line_bytes`` (a distribution plus the over-``PIPE_BUF``
    counters that feed signal D3), and ``fold_ms`` (signal D4).
    """
    events = store.read_events(rohrpost_dir)
    buf = pipe_buf(rohrpost_dir)
    line_bytes: list[int] = []
    body_bytes: list[int] = []
    over_pipe_buf = 0
    set_events = 0
    for ev in events:
        line_len = len(encode(ev)) + 1  # +1 trailing newline written by append
        line_bytes.append(line_len)
        if line_len > buf:
            over_pipe_buf += 1
        if ev.set and isinstance(ev.set.get("body"), str):
            body_bytes.append(len(ev.set["body"].encode("utf-8")))
        if ev.op in ("create", "set"):
            set_events += 1
    lock_share_pct = round(100.0 * over_pipe_buf / set_events, 2) if set_events else 0.0
    line_dist = _distribution(line_bytes)
    line_dist["over_pipe_buf"] = over_pipe_buf
    line_dist["lock_share_pct"] = lock_share_pct
    return {
        "tickets": len(fold(events)),
        "events": len(events),
        "pipe_buf": buf,
        "body_bytes": _distribution(body_bytes),
        "event_line_bytes": line_dist,
        "fold_ms": _median_cold_fold_ms(rohrpost_dir, runs=fold_runs),
    }


__all__ = ["compute_stats", "pipe_buf"]
