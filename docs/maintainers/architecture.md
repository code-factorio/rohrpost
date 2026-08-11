# Rohrpost — Maintainer's Guide

This document is for people changing the code. It maps the spec
(`docs/spec/ROHRPOST-SPEC.md`) to the implementation and records the
load-bearing decisions. The spec is the design authority; this guide is the
code-level companion.

---

## Module map

```
src/rohrpost/
  ids.py        ticket ids (6-char base32, ~2**30 space) + ULIDs (26-char, time-ordered)
  events.py     the Event envelope (frozen msgspec Struct) + JSONL codec
  exceptions.py RohrpostError hierarchy
  util.py       now_ts() (monotonic ms clock) + resolve_actor()
  paths.py      the .rohrpost/ layout, repo discovery, .gitattributes/.gitignore rules
  config.py     load/validate config.toml (project prefix; remotes passed through)
  store.py      append_event() under fcntl.flock + O_APPEND; read_events(_lenient)
  fold.py       fold(): dedupe -> sort -> replay -> Ticket; derived status, readiness, cycles
                + the tickets.jsonl snapshot cache
  api.py        the ONE write path: create/set/claim/close/drop/comment/link/...
                idempotent; returns WriteResult(ticket, wrote)
  doctor.py     rp doctor: isolated, degrading checks
  compact.py    rp compact: archive + truncate under the lock, clean-main guard
  cli.py        argparse adapter + NO_COLOR-aware rendering; --json everywhere
```

The dependency direction is strictly downward: `cli → api → {store, fold, config}`;
`fold → store → events → ids`. Nothing imports `cli`.

---

## The one load-bearing decision: the event log

Spec §13.1: the event log is the only thing that accumulates data. Everything
else is code that gets rewritten. So the **envelope** (`id`, `ts`, `op`, `actor`,
plus op-dependent payloads in `events.py`) is sacred; field names, status values
and CLI shape are cheap to change.

Rules that follow:
- **Never mutate a written event.** Events are frozen value objects. The only
  rewrite is `rp compact`, which moves whole events to the archive.
- **One write path.** All mutations go through `api.py` → `store.append_event`.
  Do not append events from anywhere else; do not hand-edit `log.jsonl`.
- **Write bare ids.** The `ticket`/`parent`/`blocked_by` ids in the log are bare
  (`a1b2c3`); the display prefix never enters the log (it is `config.toml`
  only). `fold._bare_id` is defensively tolerant of rendered ids in case a log
  was hand-authored.

---

## The fold (`fold.py`)

`fold(events) -> dict[bare_id, Ticket]` is the derivation. Algorithm (spec §6):

1. **Deduplicate by event `id`** (`_dedup_sort`). A `merge=union` of `log.jsonl`
   can produce duplicate lines; every event id is unique, so dedupe is exact.
2. **Sort by `(ts, id)`** — total and deterministic. The ULID tiebreak matters
   only when two events share a millisecond.
3. **Replay** (`_apply_event` → `_apply_set`/`_apply_scalar`): apply each op's
   payload field-by-field, recording `fieldts[field] = ts`.
4. **Per-field last-write-wins.** Two runners updating `status` and `priority`
   concurrently both win; whole-record LWW would silently discard one.

Set fields (`labels`, `blocked_by`) use `labels+`/`labels-` add/remove ops that
fold as set union/difference — concurrent labelling composes. `blocked_by`
values are normalised to bare ids; `labels` values are free-form.

### Derived state (never stored)

- `ready` — `is_ready()`: `open`, non-epic, every `blocked_by` done.
- epic status — `derive_status()`: `done` when all children done, else `open`.
- `last_close_reason` — the most recent terminal-status event's `reason`.
- `created`/`updated` — first / last event ts for the ticket.

These are computed at query time, so closing a dependency unblocks its
dependents with **zero** extra writes (no write amplification, no merge conflicts).

### Snapshot cache (`tickets.jsonl`)

`load_tickets()` reuses the snapshot when its mtime is **strictly newer** than
the log's (`st_mtime_ns`, strict `>`); otherwise it re-folds and overwrites. The
strict comparison trades a rare redundant fold for never serving data that
predates an append. The snapshot is gitignored and disposable; `doctor` checks it
matches a fresh fold.

### Monotonic clock

`util.now_ts()` is strictly increasing per process (bumps the millisecond on
collision). This keeps insertion order deterministic within a process — without
it, two events in the same millisecond would tie on `ts` and fall back to the
ULID's random suffix, reordering append-only things like comments. Cross-process
ordering still relies on the ULID tiebreak (correct for LWW field semantics).

---

## Concurrency (`store.py`)

| Hazard | Mitigation |
|---|---|
| Two processes writing the log | `fcntl.flock` (LOCK_EX on `.rohrpost/.lock`) + `O_APPEND` |
| Two branches writing the log | `.gitattributes` `merge=union` + dedupe on read |
| Same ticket, same field, two branches | per-field LWW by `ts` |
| Same ticket, different fields | both survive (common case) |
| Duplicate events after merge | dropped by `id` during fold |
| Stale `tickets.jsonl` | regenerated when mtime older than log |

Each append is one `write()` of one line under `O_APPEND` inside the lock.

---

## Adding things (the cheap changes)

**A new field.** Add it to `SCALAR_FIELDS`/`SET_FIELDS` in `fold.py`, to the
allowed set in `api.parse_assignment`, and to `ticket_to_mapping`/
`_mapping_to_ticket` (round-trip!). Old logs without it keep folding (the fold
ignores unknown keys; `priority`/scalar default via `_Builder`).

**A new status/type.** Add to `STATUSES`/`TYPES`. Decide terminal-ness
(`TERMINAL`) and whether it affects `is_ready`/`derive_status`.

**A new op.** Add to the `Op` literal in `events.py`, handle it in
`fold._apply_event`, and add an `api` function + `cli` command.

**A sync provider.** `merge.py` (three-way per-field merge + `git merge-file`
for bodies), `shadow.py` (the merge base), `sync.py` (the round), and
`providers/github.py` (gh-preferred, httpx fallback) implement spec §8.
`config.py` passes `[remotes.*]` tables through. To add Jira/Linear/GitLab,
implement the `Provider` protocol (`fetch`/`push` returning local-vocab field
maps) and register it in `cli._build_provider`. First-cut sync is scalar fields
only; a set-wise three-way merge for `labels` is the follow-on.

---

## Quality gate

`make check` runs ruff (format+lint), ty + mypy + pyright, bandit, pyscn, radon,
xenon, and pytest with coverage (`fail_under = 85`, `filterwarnings = ["error"]`).
Complexity is held to grade B (`xenon --max-absolute B`, pyscn max-complexity 10)
— refactor by extracting helpers rather than raising thresholds.

**Environment note:** the local `ruff format` is wrapped and strips parens from
`except (A, B):`, producing a SyntaxError. Avoid that pattern — use
`contextlib.suppress(A, B)` or separate `except` clauses.

---

## Testing conventions

Tests are focused, not exhaustive smoke. Each module has its own `test_*.py`:
`fold` proves the algorithm (dedupe, sort, per-field LWW, set ops, derived state,
cycles, snapshot round-trip); `api` proves the write path and idempotency;
`store` proves append/read/lock; `doctor`/`compact` prove detection and the
guards. The `deterministic_clock`/`deterministic_ulid` fixtures (in `conftest`)
make event ordering deterministic; the real `now_ts` is monotonic so tests that
don't inject a clock still get stable ordering.
