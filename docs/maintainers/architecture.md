# Rohrpost — Maintainer's Guide

This document is for people changing the code. It maps the spec
(`docs/spec/ROHRPOST-SPEC.md`) to the implementation and records the
load-bearing decisions. The spec is the design authority; this guide is the
code-level companion. The rewrite decisions themselves are in
[ADR 0001](../adr/0001-rust-std-only-rewrite.md).

---

## Module map

```
src/
  ids.rs      ticket ids (6-char base32, ~2**30 space) + ULIDs (26-char, time-ordered);
              entropy from std's OS-seeded RandomState
  events.rs   the Event envelope + JSONL codec; unknown keys preserved in `extra`
  error.rs    one Error enum; Usage -> exit 2, everything else -> exit 1
  time.rs     now_ts() (RFC 3339 ms, monotonic per process), format/parse, civil dates
  util.rs     resolve_actor() + git_output()
  paths.rs    the .rohrpost/ layout, repo discovery, the .gitattributes rules
  config.rs   load/validate config.toml (prefix, default_branch)
  toml.rs     the TOML subset config.toml and templates use
  json.rs     Json value (ordered objects, interned keys), parser, compact + pretty writers
  store.rs    append_event() under File::lock + append mode; read_events(_lenient)
  fold.rs     fold(): dedupe -> sort -> replay -> Ticket; derived status, readiness, cycles;
              the --json shape (full / short)
  api.rs      the ONE write path: create/set/claim/close/drop/comment; templates; queries
  doctor.rs   rp doctor: isolated, degrading checks
  compact.rs  rp compact: archive + truncate under the lock, clean-main guard
  stats.rs    rp stats: byte distributions + cold-fold timing
  cli.rs      argv parsing driven by a command table, dispatch, NO_COLOR-aware rendering
  main.rs     std::process::exit(cli::main(argv))
tests/cli.rs  end-to-end tests that drive the built binary in temp git repos
```

The dependency direction is strictly downward: `cli → api → {store, fold, config, paths}`;
`fold → store → events → ids`. Nothing imports `cli`. The crate is a library plus a thin
binary so the integration tests can reuse the JSON parser.

**No dependencies.** `Cargo.toml` has an empty `[dependencies]` table and that is a design
constraint, not an accident: `rp` runs in bare agent containers on three platforms. What
would normally be crates (`serde_json`, `toml`, `clap`, `chrono`, `rand`, `fs2`) is a few
hundred lines each in-tree, scoped to exactly what Rohrpost needs.

---

## The one load-bearing decision: the event log

Spec §13.1: the event log is the only thing that accumulates data. Everything
else is code that gets rewritten — this repository is the proof. So the
**envelope** (`id`, `ts`, `ticket`, `op`, `actor`, plus op-dependent payloads in
`events.rs`) is sacred; field names, status values and CLI shape are cheap to change.

Rules that follow:
- **Never mutate a written event.** The only rewrite is `rp compact`, which moves
  whole events from the live log to the archive.
- **One write path.** All mutations go through `api.rs` → `store::append_event`.
  Do not append events from anywhere else; do not hand-edit `log.jsonl`.
- **Write bare ids.** The `ticket`/`parent`/`blocked_by` ids in the log are bare
  (`a1b2c3`); the display prefix never enters the log. `fold::bare_id` is tolerant of
  rendered ids in case a log was hand-authored.
- **Tolerate what you do not know.** Unknown payload keys are ignored by the fold and
  round-tripped by `rp log`/`compact`. The legacy sync ops (`link`, `unlink`, `synced`)
  decode, count as ticket activity for `updated`, and apply no state.

---

## The fold (`fold.rs`)

`fold(&[Event]) -> BTreeMap<bare_id, Ticket>` is the derivation. Algorithm (spec §6):

1. **Deduplicate by event `id`** (`dedup_sort`). A `merge=union` of `log.jsonl`
   can produce duplicate lines; every event id is unique, so dedupe is exact.
2. **Sort by `(ts, id)`** — total and deterministic. The ULID tiebreak matters
   only when two events share a millisecond.
3. **Replay** (`apply_event` → `apply_set`/`apply_scalar`): apply each op's
   payload field-by-field, recording `fieldts[field] = ts`.
4. **Per-field last-write-wins.** Two runners updating `status` and `priority`
   concurrently both win; whole-record LWW would silently discard one.

Set fields (`labels`, `blocked_by`) use `labels+`/`labels-` add/remove ops that
fold as set union/difference — concurrent labelling composes. `blocked_by`
values are normalised to bare ids; `labels` values are free-form.

The builder borrows timestamps from the events and allocates only when a ticket is
frozen; `fieldts` keys are `&'static str` from the field tables. On a slow aarch64 box a
30 000-event log parses and folds in ~90 ms; on a laptop in ~25 ms. That is why there is
**no snapshot cache**: `rp ready` folds cold every time, and `rp stats` reports `fold_ms`
so the decision can be revisited with data.

### Derived state (never stored)

- `ready` — `is_ready()`: `open`, non-epic, every `blocked_by` done.
- epic status — `derive_status()`: `done` when all children done, else `open`.
- `last_close_reason` — the most recent terminal-status event's `reason`.
- `created`/`updated` — first / last event ts for the ticket.

These are computed at query time, so closing a dependency unblocks its
dependents with **zero** extra writes (no write amplification, no merge conflicts).

### Monotonic clock

`time::now_ts()` is strictly increasing per process (bumps the millisecond on
collision). This keeps insertion order deterministic within a process — without
it, two events in the same millisecond would tie on `ts` and fall back to the
ULID's random suffix, reordering append-only things like comments. Cross-process
ordering still relies on the ULID tiebreak (correct for LWW field semantics).

---

## Concurrency (`store.rs`)

| Hazard | Mitigation |
|---|---|
| Two processes writing the log | `File::lock` on `.rohrpost/.lock` (`flock` / `LockFileEx`) + append mode |
| Two branches writing the log | `.gitattributes` `merge=union` + dedupe on read |
| Same ticket, same field, two branches | per-field LWW by `ts` |
| Same ticket, different fields | both survive (common case) |
| Duplicate events after merge | dropped by `id` during fold |
| A write that fails part-way | the file is truncated back to its previous length |

Each append is one `write_all` of one line in append mode inside the lock. Readers never
lock; a torn tail line fails to decode and the lenient reader (used by `doctor`) reports it
while the strict reader (everything else) fails loudly. The same lock guards compaction's
read-partition-rewrite sequence, and compaction appends to the archive **before** rewriting
the log so an interruption duplicates (harmless) rather than loses.

Platform notes: append mode is `O_APPEND` on Unix and `FILE_APPEND_DATA` on Windows;
`std::fs::rename` replaces the target on every platform, which is what `write_atomic`
relies on; the lock is advisory on Unix and mandatory on Windows, which is fine because
only `.lock` is ever locked, never the log.

---

## Adding things (the cheap changes)

**A new field.** Add it to `SCALAR_FIELDS`/`SET_FIELDS` in `fold.rs` (and to
`json::KNOWN_KEYS` if it is hot), to the allowed set in `api::parse_assignment`, to
`Ticket`/`Builder`/`ticket_to_json`, and to the template loader if templates may set it.
Old logs without it keep folding (the fold ignores unknown keys and defaults the rest).

**A new status/type.** Add to `STATUSES`/`TYPES`. Decide terminal-ness (`TERMINAL`) and
whether it affects `is_ready`/`derive_status`.

**A new op.** Add it to `events::KNOWN_OPS`, handle it in `fold::apply_event`, and add
an `api` function plus a `cli::SPECS` entry and handler.

**A new command.** One `Spec` row in `cli::SPECS` (positionals, options, help) plus a
`cmd_*` handler in `dispatch`. `--help`, validation and usage errors come from the table.

---

## Quality gate

`make check` = `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`,
`cargo test`. CI runs the same on Linux, macOS and Windows, checks the 1.89 MSRV, and
runs the release binary's `rp doctor` against this repository. Tags `v*` build and
publish the release binaries (`.github/workflows/release.yml`).

---

## Testing conventions

Tests are focused, not exhaustive smoke. Algorithmic modules carry unit tests next to the
code (`json`, `toml`, `ids`, `time`, `events`, `fold`, `api::parse_assignment`, `cli`
argv parsing). `tests/cli.rs` drives the built binary in temporary git repositories and
pins the agent-facing contract: exit codes, `--json` shapes, text renderings, `--body-file`
on every platform, the compaction guard, doctor findings, legacy-log tolerance and a
twelve-process concurrent-append race. Fixture ticket ids must be valid Crockford base32
(no `i`, `l`, `o`, `u`).
