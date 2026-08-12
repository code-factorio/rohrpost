#!/usr/bin/env python3
"""E7: verify the ``rp ready`` hot path never carries body prose.

Decision experiment feeding the inline-vs-sidecar body question (spec §13.1).
The everyday agent query is ``rp ready --json``: it lists the actionable work
queue. If that output stays roughly flat no matter how large the ticket bodies
grow, then the agent's *context cost* is independent of where bodies are stored
— the inline-vs-sidecar decision only affects storage and merge, not the query
path that runs on every turn. If the output grows with body size, prose is
leaking into the hot path, which is a bug: every ``rp ready`` would then charge
the agent for every byte of every body in the queue.

The swept variable is therefore body size. For each size the script builds a
fresh ``.rohrpost`` log of ``--tickets`` ready tickets (status open, unblocked,
non-epic), each carrying a body of that many bytes, then measures the byte size
of the ``rp ready --json`` output at ``--ready-limit``. Events are built directly
with ``Event(...)`` + ``rohrpost.events.encode`` (NOT via ``api.create_ticket`` —
its per-call fold would make a 50 KB-body run impractically slow) and the log is
written in one shot.

The in-process measurement mirrors ``rp ready --json`` exactly:
``api.ready_tickets(dir, limit=N)`` then
``ticket_to_mapping(t, prefix=..., include_fieldts=False, include_comments=False)``
per ticket (the ``_short`` shape ``cli.cmd_ready`` emits for ``--json``), joined
as ``json.dumps(list, indent=2, ensure_ascii=False) + "\\n"`` (what
``_Out.emit_json`` writes). An optional subprocess cross-check shells out to the
real ``rp ready --json --limit N`` and ``wc -c`` to confirm the in-process byte
count matches the live CLI. This is a standalone measurement script, not a
pytest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rohrpost import api, paths
from rohrpost.events import Event, encode
from rohrpost.fold import ticket_to_mapping
from rohrpost.ids import new_ticket_id, new_ulid

#: Fixed epoch base (2026-01-01 UTC) so timestamps are deterministic and strictly
#: increasing across the whole synthesis — the fold's ``(ts, id)`` order is then
#: total, making the chosen ``ready`` slice deterministic. Mirrors conftest.py.
_EPOCH_MS: int = 1_767_225_600_000

#: A representative actor; the namespace is load-bearing per spec §5.2 but its
#: exact value does not affect the ready path, only the bytes per line by a
#: constant.
_ACTOR: str = "user/bench@example.com"


# ---------------------------------------------------------------------------
# Result rows
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Row:
    """One reported table row: a single body size's measurement."""

    body_size: int
    total_body_bytes: int
    ready_count: int
    ready_json_bytes: int
    cli_bytes: int | None


@dataclass(frozen=True, slots=True)
class _Verdict:
    """The flat/leak verdict across all body sizes."""

    spread: int
    tolerance: int
    flat: bool
    label: str


# ---------------------------------------------------------------------------
# Synthesis helpers
# ---------------------------------------------------------------------------
def _ms_to_ts(ms: int) -> str:
    """Render ms-since-epoch as an RFC 3339 UTC ms string (mirrors rohrpost.util)."""
    return (
        dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _make_body(size: int) -> str:
    """Return an ASCII body of exactly ``size`` bytes (``""`` if ``size <= 0``).

    Only digits are used so ``json.dumps`` never escapes anything: the serialized
    byte count is then a clean linear function of ``size``.
    """
    if size <= 0:
        return ""
    unit = "0123456789"
    return (unit * (size // len(unit) + 1))[:size]


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


def _build_repo(repo_root: Path, tickets: int, body_size: int) -> Path:
    """Scaffold a ``.rohrpost`` dir and write ``tickets`` ready tickets in one shot.

    Every ticket is a ``task``, ``open``, with no parent and no blockers — i.e.
    ready by ``rohrpost.fold.is_ready`` — and each carries a body of ``body_size``
    bytes in its ``create`` event (omitted entirely when ``body_size == 0`` so the
    folded ``body`` is ``None``, matching a real no-body ticket). Returns the
    ``.rohrpost`` directory path.
    """
    rohrpost_dir = repo_root / paths.ROHRPOST_DIR_NAME
    paths.ensure_layout(rohrpost_dir)

    body = _make_body(body_size)
    ticket_ids = _make_ticket_ids(tickets)
    chunks: list[bytes] = []
    for index, tid in enumerate(ticket_ids):
        payload: dict[str, object] = {
            "title": f"ticket-{index}",
            "type": "task",
            "status": "open",
            "priority": 2,
        }
        if body_size > 0:
            payload["body"] = body
        event = Event(
            id=new_ulid(),
            ts=_ms_to_ts(_EPOCH_MS + index),
            ticket=tid,
            op="create",
            actor=_ACTOR,
            set=payload,
        )
        chunks.append(encode(event))
        chunks.append(b"\n")
    paths.log_path(rohrpost_dir).write_bytes(b"".join(chunks))
    return rohrpost_dir


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------
def _ready_json_bytes(rohrpost_dir: Path, limit: int) -> tuple[int, int]:
    """Return ``(ready_count, ready_json_bytes)`` mirroring ``rp ready --json``.

    Calls the real ``api.ready_tickets`` and renders each ticket with the same
    ``_short`` shape the CLI emits for ``ready --json`` (``include_fieldts=False``,
    ``include_comments=False``, ``include_body=False`` — fieldts, comments and the
    body dropped, so the queue view carries no prose). The prefix is read from the
    repo config so the byte count matches ``rp ready --json | wc -c``, which
    resolves the same way through ``api.load_repo_config``.
    """
    tickets = api.ready_tickets(rohrpost_dir, limit=limit)
    prefix = api.load_repo_config(rohrpost_dir).prefix
    mappings = [
        ticket_to_mapping(
            t,
            prefix=prefix,
            include_fieldts=False,
            include_comments=False,
            include_body=False,
        )
        for t in tickets
    ]
    # Mirrors rohrpost.cli._Out.emit_json: json.dump(list, indent=2, ensure_ascii=False)
    # immediately followed by a trailing newline from print().
    blob = json.dumps(mappings, indent=2, ensure_ascii=False) + "\n"
    return len(tickets), len(blob.encode("utf-8"))


def _cli_ready_bytes(repo_root: Path, limit: int) -> int | None:
    """Byte size of ``rp ready --json --limit N`` stdout, or ``None`` if unavailable.

    Best-effort cross-check against the real CLI. Returns ``None`` when ``rp`` is
    not on PATH (run via ``uv run`` to put it there) or the invocation fails, so
    the in-process measurement stays primary.
    """
    rp = shutil.which("rp")
    if rp is None:
        return None
    try:
        result = subprocess.run(
            [rp, "ready", "--json", "--limit", str(limit)],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError, OSError:
        return None
    return len(result.stdout)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def _verdict(ready_bytes: list[int]) -> _Verdict:
    """Classify the sweep as FLAT (hot path independent of body size) or LEAK.

    A flat hot path produces an identical ready-json payload at every body size,
    so the spread (max - min) should be ~0. The tolerance is 2 % of the smallest
    payload with a 128 B floor: large enough to absorb the ``"body": null`` vs.
    omitted-field structural difference (a few bytes per ticket), far smaller
    than any real leak (``ready_limit * max_body`` bytes — e.g. 500 KB here).
    """
    spread = max(ready_bytes) - min(ready_bytes)
    baseline = min(ready_bytes)
    tolerance = max(128, baseline // 50)
    flat = spread <= tolerance
    return _Verdict(spread=spread, tolerance=tolerance, flat=flat, label="FLAT" if flat else "LEAK")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _human_bytes(num: int) -> str:
    """Render a byte count with a binary unit suffix (B / KiB / MiB / GiB)."""
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def _print_table(rows: list[_Row]) -> None:
    """Print the aligned fixed-width results table."""
    header = f"{'body(B)':>9}  {'body-bytes':>12}  {'ready':>6}  {'ready-json':>12}  {'rp CLI':>12}"
    print(header)
    print("-" * len(header))
    for r in rows:
        cli = "-" if r.cli_bytes is None else _human_bytes(r.cli_bytes)
        print(
            f"{r.body_size:>9}  {_human_bytes(r.total_body_bytes):>12}  {r.ready_count:>6}  "
            f"{_human_bytes(r.ready_json_bytes):>12}  {cli:>12}"
        )


def _print_verdict(verdict: _Verdict, rows: list[_Row]) -> None:
    """Print the key assertion: the ready-json bytes should be ~constant."""
    print()
    if verdict.flat:
        print(
            f"VERDICT: FLAT  (spread {_human_bytes(verdict.spread)} across body sizes, "
            f"tolerance {_human_bytes(verdict.tolerance)})"
        )
        print("  The 'rp ready' hot path is independent of body size.")
        print(
            "  Agent context cost is unaffected by the inline-vs-sidecar decision; "
            "that decision is about storage/merge, not the everyday query path."
        )
    else:
        largest = max(rows, key=lambda r: r.body_size)
        smallest = min(rows, key=lambda r: r.body_size)
        per_ticket = 0
        if largest.body_size > smallest.body_size and largest.ready_count > 0:
            per_ticket = (
                largest.ready_json_bytes - smallest.ready_json_bytes
            ) // largest.ready_count
        print(
            f"VERDICT: LEAK  (spread {_human_bytes(verdict.spread)} across body sizes, "
            f"tolerance {_human_bytes(verdict.tolerance)})"
        )
        print("  The 'rp ready' hot path grows with body size -> bug.")
        print(
            f"  Bodies are reaching the everyday query path (~{_human_bytes(per_ticket)}/ticket "
            f"from {smallest.body_size} B to {largest.body_size} B bodies): every "
            "`rp ready` charges the agent for the prose in the queue."
        )


def _print_cli_check(rows: list[_Row]) -> None:
    """Report whether the subprocess ``rp`` cross-check matches the in-process bytes."""
    # The ``if r.cli_bytes is not None`` guard narrows ``cli_bytes`` to ``int`` in
    # the expression, so the subtraction type-checks without a cast.
    deltas = [abs(r.cli_bytes - r.ready_json_bytes) for r in rows if r.cli_bytes is not None]
    print()
    if not deltas:
        print(
            "CLI cross-check: skipped ('rp' not on PATH; run via 'uv run' to validate "
            "against the real CLI)."
        )
        return
    max_delta = max(deltas)
    if max_delta == 0:
        print(
            f"CLI cross-check: 'rp ready --json | wc -c' matched the in-process bytes "
            f"at all {len(deltas)} body sizes (max delta 0 B)."
        )
    else:
        print(
            f"CLI cross-check: max delta {_human_bytes(max_delta)} between in-process and "
            f"'rp ready --json | wc -c' across {len(deltas)} body sizes."
        )


# ---------------------------------------------------------------------------
# argparse helpers
# ---------------------------------------------------------------------------
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
    """Parse args, run the sweep, print the table and verdict. Returns the exit code."""
    parser = argparse.ArgumentParser(
        description=(
            "E7: verify the 'rp ready' hot path is independent of inline body size "
            "(decision signal for the inline-vs-sidecar body question)."
        )
    )
    parser.add_argument(
        "--tickets", type=int, default=200, help="open ready tickets per body size (default 200)"
    )
    parser.add_argument(
        "--ready-limit",
        type=int,
        default=10,
        help="tickets returned by 'rp ready' / --limit (default 10)",
    )
    parser.add_argument(
        "--bodies",
        type=_parse_bodies,
        default=[0, 500, 5000, 50000],
        help="comma-separated body byte sizes (default 0,500,5000,50000)",
    )
    parser.add_argument(
        "--tmpdir",
        type=str,
        default=None,
        help="working dir (default: a fresh mkdtemp, left in place with its path printed)",
    )
    parser.add_argument(
        "--no-cli",
        action="store_true",
        help="skip the subprocess 'rp ready --json' cross-check",
    )
    args = parser.parse_args(argv)

    if args.tickets < 1:
        parser.error("--tickets must be >= 1")
    if args.ready_limit < 0:
        parser.error("--ready-limit must be >= 0")
    if len(args.bodies) < 2:
        parser.error("--bodies must list at least 2 sizes to compare for a flat/leak verdict")

    tmpdir = Path(args.tmpdir) if args.tmpdir else Path(tempfile.mkdtemp(prefix="rp-bench-ready-"))
    tmpdir.mkdir(parents=True, exist_ok=True)

    print("E7 bench: 'rp ready --json' output size vs inline body size")
    print(f"  tickets={args.tickets}  ready-limit={args.ready_limit}  bodies={args.bodies}")
    print(f"  tmpdir={tmpdir}")
    print(
        "  expects FLAT: 'rp ready --json' omits bodies, so output should be ~constant "
        "across body sizes."
    )
    print()

    rows: list[_Row] = []
    expected_ready = min(args.ready_limit, args.tickets)
    for body_size in args.bodies:
        repo_root = tmpdir / f"body-{body_size}b"
        rohrpost_dir = _build_repo(repo_root, args.tickets, body_size)
        ready_count, ready_bytes = _ready_json_bytes(rohrpost_dir, args.ready_limit)
        if ready_count < expected_ready:
            print(
                f"  ! warning: body={body_size} only {ready_count} ready tickets "
                f"(expected {expected_ready}); the measurement may undercount.",
                file=sys.stderr,
            )
        cli_bytes: int | None = None
        if not args.no_cli:
            cli_bytes = _cli_ready_bytes(repo_root, args.ready_limit)
        rows.append(
            _Row(
                body_size=body_size,
                total_body_bytes=args.tickets * body_size,
                ready_count=ready_count,
                ready_json_bytes=ready_bytes,
                cli_bytes=cli_bytes,
            )
        )

    _print_table(rows)
    _print_verdict(_verdict([r.ready_json_bytes for r in rows]), rows)
    _print_cli_check(rows)

    print(f"\ntmpdir left in place at: {tmpdir}")
    print(
        "E7 signal: FLAT means the inline-vs-sidecar decision is about storage/merge "
        "only; LEAK means bodies must be kept out of 'rp ready' regardless."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
