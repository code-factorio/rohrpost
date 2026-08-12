"""Tests for :mod:`rohrpost.stats` — the §13.1 decision instrumentation.

``compute_stats`` derives every signal from the log, so the tests assert it reads
the real log correctly: the body-size distribution, the over-``PIPE_BUF`` count,
the lock-share percentage, and that a cold fold is timed without error. The
percentile/distribution helpers get a focused shape test rather than asserting
exact banker's-rounded values (the decision thresholds are coarse enough that the
interpolation method is immaterial).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from rohrpost import api
from rohrpost.stats import _distribution, compute_stats, pipe_buf


def test_pipe_buf_is_the_posix_floor(tmp_repo: Path) -> None:
    # POSIX guarantees a 512-byte floor; Linux reports 4096. Resolved against the
    # log's own filesystem so the value the stats use matches the append path.
    assert pipe_buf(tmp_repo) >= 512
    assert pipe_buf() >= 512  # default (cwd) path also resolves


def test_stats_on_empty_repo(tmp_repo: Path) -> None:
    data = compute_stats(tmp_repo)
    assert data["tickets"] == 0
    assert data["events"] == 0
    assert cast(dict[str, int], data["body_bytes"])["count"] == 0
    line = cast(dict[str, float], data["event_line_bytes"])
    assert line["over_pipe_buf"] == 0
    assert line["lock_share_pct"] == 0.0
    assert cast(float, data["fold_ms"]) >= 0.0


def test_stats_derive_body_sizes_and_pipe_buf_share(tmp_repo: Path) -> None:
    small = "x" * 2
    big = "y" * 6000  # line length well over the 4096 PIPE_BUF on Linux
    api.create_ticket(tmp_repo, "small", body=small, actor="user/test")
    api.create_ticket(tmp_repo, "big", body=big, actor="user/test")

    data = compute_stats(tmp_repo)
    body = cast(dict[str, int], data["body_bytes"])
    line = cast(dict[str, float], data["event_line_bytes"])

    assert data["tickets"] == 2
    assert data["events"] == 2
    assert body["count"] == 2
    assert body["max"] == 6000
    # The 6000-byte body produces a JSONL line exceeding PIPE_BUF -> one append
    # that relies on the lock; two create events total -> 50%.
    assert line["over_pipe_buf"] == 1
    assert line["lock_share_pct"] == 50.0


def test_whitespace_only_body_is_not_counted_as_a_body(tmp_repo: Path) -> None:
    # create_ticket drops a body whose .strip() is empty, so it never reaches the
    # log as a body field and must not appear in the body-size distribution.
    api.create_ticket(tmp_repo, "ws", body="   ", actor="user/test")
    data = compute_stats(tmp_repo)
    assert cast(dict[str, int], data["body_bytes"])["count"] == 0


def test_distribution_shape_is_monotonic_and_counts_samples() -> None:
    empty = cast(dict[str, int], _distribution([]))
    assert empty == {"p50": 0, "p90": 0, "p95": 0, "p99": 0, "max": 0, "count": 0}

    single = cast(dict[str, int], _distribution([42]))
    assert single["count"] == 1
    assert single["max"] == 42

    dist = cast(dict[str, int], _distribution(list(range(1, 101))))  # 1..100
    assert dist["count"] == 100
    assert dist["max"] == 100
    assert dist["p50"] <= dist["p90"] <= dist["p95"] <= dist["p99"] <= dist["max"]
    assert dist["p50"] >= 1
