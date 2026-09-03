# Rohrpost — Specification

**Status:** Draft v0.2 · **Date:** 2026-09-03 (v0.1: 2026-08-11) · **Author:** Vinzenz Feenstra

A git-native ticket system for agentic coding workflows. Local files are canonical and
the repository is the only tracker.

> **v0.2 (2026-09-03).** The implementation is a standard-library-only Rust binary; the
> sync layer of v0.1 (§8, the `link`/`unlink`/`synced` ops, the `remotes` field, shadow
> snapshots and providers) and the `tickets.jsonl` snapshot cache were removed. See
> `docs/adr/0001-rust-std-only-rewrite.md`. Sections below are edited in place; the
> removed material is summarised where it used to stand.

---

## 1. What it is

Rohrpost keeps work items as files in the repository they belong to. Coding agents
create, claim, update and close them without leaving the repo. There is no server, no
daemon and no remote tracker: the repository is the source of truth and the whole
system.

The differentiator is not the storage format — it is a validated single write path over
an append-only log that merges through git without coordination.

**Design assumption:** ~95% of all reads and writes come from agents, not humans.
Every trade-off below resolves in favour of machine ergonomics.

---

## 2. Naming

**Project:** Rohrpost — German pneumatic tube mail. Berlin ran ~400 km of it;
Prague's is the last citywide system ever built and has been mothballed since the
2002 floods. Work capsules shot through tubes to stations is the mental image.

**Binary:** `rp` · **ID prefix:** `RP-`

The metaphor lives on the label only: project name, binary, README voice, logo, and
`rp doctor` output. Everything in the schema, CLI and code uses plain software
vocabulary. This is the Flask/Jinja/Werkzeug pattern — whimsical name, boring API.

| Concept | Term |
|---|---|
| Unit of work | `ticket` |
| Grouping / decomposition | `plan` |
| Reusable ticket skeleton | `template` |
| Set dispatched together | `batch` |
| The agent doing work | `runner` |
| Its git worktree | `workspace` |

### 2.1 The one naming rule

> **`ticket` is ours. `issue` is theirs.**

Never use "issue" for a local object — not in code, not in docs, not in CLI output.
This removes all ambiguity when someone says "close the ticket" vs "close the issue",
and it costs nothing to enforce from day one.

---

## 3. Design principles

1. **The log is truth.** Everything else is derived and disposable.
2. **Any file `rp` can regenerate must be deletable.** Any file it cannot must be
   committed and must survive a merge. No file may sit between these categories.
3. **One write path.** All mutations go through `rp`. Hand-editing is not supported.
4. **Git is the backup.** There is no separate backup tier.
5. **Conflicts that agents can resolve should resolve silently. Conflicts that need
   judgement must be loud.**

### 3.1 Architecture and boundaries

Rohrpost is one component of a larger system and knows about none of the others.

```
    bus  ──┐                    reactive: webhooks, comments, CI
 planner ──┼──→ rp (CLI) ──→ .rohrpost/
 runners ──┘                    tickets only
```

**The dependency runs one way.** Everything above drives Rohrpost through `rp --json`;
Rohrpost never calls out, never listens, never runs as a daemon. This is what keeps a
single `rp` binary useful standalone in any repository — the original requirement, and
worth more than any feature it excludes.

Reading through the CLI is not coupling: a planner calling `rp show --json` for a body
has the same access a runner does. Coupling is when a consumer needs *fields Rohrpost
would not otherwise have*. Planner state, bus state, and runner state live with those
components, keyed by ticket id.

---

## 4. On-disk layout

```
.rohrpost/
├── config.toml              # committed — display prefix, compaction branch
├── log.jsonl                # committed — append-only event log. TRUTH.
├── archive/
│   └── log-2026-Q2.jsonl    # committed — compacted historical events
├── templates/
│   └── bug.toml             # committed — hand-authored
└── .lock                    # the advisory lock file (empty)
```

`.gitattributes`:

```gitattributes
.rohrpost/log.jsonl          merge=union text eol=lf
.rohrpost/archive/*.jsonl    merge=union text eol=lf
```

`merge=union` keeps both sides' appended lines instead of writing conflict markers.
It is safe here **only because the log is strictly append-only** and every event
carries a unique id, so duplicates are removed on read. `text eol=lf` on the JSONL
event store normalises every checkin to an LF blob and checks the file out with the
blob's exact bytes on any platform, regardless of `core.autocrlf`: a Windows clone and
a Linux clone hold byte-identical working trees, and the union driver always compares
LF lines.

Every file under `.rohrpost/` is committed. There is no regenerable cache: the fold is
cheap enough to run on every invocation (§11), so nothing sits between "committed and
must survive a merge" and "deletable".

---

## 5. Data model

### 5.1 Identifiers

**Stored form:** 6 lowercase base32 characters from random bytes, e.g. `a1b2c3`.
**Rendered form:** `<prefix>-a1b2c3`, where the prefix is per-project configuration,
not per-tool.

```toml
[project]
prefix = "FAC"        # → FAC-a1b2c3
```

`rp init` proposes a prefix derived from the directory name and accepts an override.
Two to five uppercase letters, matching Jira's project-key convention so the two can
be aligned where that is useful.

The prefix is **display-only** — it never enters the log. Changing it later is a config
edit, not a migration through the event history. `rp` accepts either form as input, so
`rp show a1b2c3` and `rp show FAC-a1b2c3` are equivalent; agents will drop the prefix.

The collision domain is one repository, so ~1 billion values is comfortable. For a
cross-repo index the key is the pair `(prefix, id)` — which is why a hardcoded `RP-`
would have been a defect: three repos referenced from one external index would produce
indistinguishable references.

Sequential numbers are rejected deliberately: parallel runners on separate branches
cannot allocate them without coordination, and coordination is the thing this design
is built to avoid. Random suffixes collide with negligible probability and need no
central authority.

Event ids are ULIDs — lexicographically sortable by creation time, which gives a
stable tiebreak during folding.

### 5.2 Events (`log.jsonl`)

One JSON object per line. Append-only. Never rewritten except by `rp compact`.

```jsonc
{
  "id":     "01K2X8P4RQ7YFZ3M9NVB6TDHWC",  // ULID
  "ts":     "2026-08-11T09:20:14.221Z",     // RFC 3339, UTC, ms precision
  "ticket": "a1b2c3",                       // bare id — the display prefix never enters the log (§5.1)
  "op":     "set",                          // see below
  "actor":  "runner/claude-code@b-3",       // or "user/<git email>"
  "set":    { "status": "in_progress" }     // op-dependent payload
}
```

`actor` is load-bearing: it distinguishes a human decision from a runner write. Two
namespaces — `user/*` resolved from `git config user.email`, and
`runner/<agent>@<batch>`. Never hardcode a name.

**Operations:**

| `op` | Payload | Meaning |
|---|---|---|
| `create` | `set: {...}` | Ticket comes into existence with initial fields |
| `set` | `set: {field: value}` | Field-level update. The workhorse |
| `set` | `set: {"labels+": [...]}` / `{"labels-": [...]}` | Set add/remove — see below |
| `comment` | `text: str` | Append-only discussion entry |

There is no `close` or `claim` op — both are `set` on `status`. Fewer ops means a
simpler fold and fewer schema decisions to regret.

Three further ops — `link`, `unlink` and `synced` — were written by the v0.1 sync layer.
They still **decode** (a log is forever) and count as activity on their ticket for
`updated`, but they apply no state and `rp` never writes them. `rp doctor` reports how
many such legacy events a log carries. Keys the reader does not know are preserved
verbatim, so `rp log` and `rp compact` round-trip any log byte-for-byte.

**Close reasons ride on the event, not the ticket:**

```jsonc
{"op":"set","set":{"status":"done"},"reason":"implemented with exponential backoff"}
```

A field would be overwritten when a ticket is reopened and closed again; on the event,
both reasons survive. The fold exposes `last_close_reason` for convenience so `rp show`
need not walk the log.

**Array fields use add/remove ops, not whole-array writes.** Arrays are the hostile
case for per-field LWW: two runners each adding a different label produce two
whole-array writes and one silently loses. `labels+` and `labels-` fold as set union
and difference, so concurrent labelling composes. Applies to `labels` and any future
array field.

### 5.3 Ticket (folded shape)

```jsonc
{
  "id":         "RP-a1b2c3",
  "title":      "Fix token refresh race",
  "type":       "task",          // task | bug | spike | epic
  "status":     "open",          // see 5.4
  "priority":   2,               // 0 highest .. 4 lowest
  "parent":     "RP-9f8e7d",     // epic ownership. No children[] — see 5.5
  "blocked_by": ["RP-4702aa"],
  "labels":     ["auth"],
  "assignee":   "runner/claude-code",
  "body":       "…",             // or {"path": "bodies/RP-a1b2c3.md"} in phase 2
  "last_close_reason": null,     // derived from the most recent close event
  "created":    "2026-08-11T09:14:02Z",
  "updated":    "2026-08-11T11:02:38Z",
  "_fieldts":   { "status": "2026-08-11T11:02:38Z", "priority": "…" }
}
```

`_fieldts` carries the last-write timestamp per field. It is what makes field-level
merge possible, and it is why the fold is not simply "last event wins".

### 5.4 Status

`open → ready → in_progress → review → done`, plus `waiting` and terminal `dropped`.

- `open` — exists, not yet actionable
- `ready` — **derived, never stored**: `open` AND every `blocked_by` is `done`
- `in_progress` — a runner holds it
- `review` — work pushed, awaiting a gate
- `waiting` — stalled on human input. Rohrpost records it; something outside decides
  when it is set and cleared. Distinct from `blocked_by`, which means another *ticket*
  is in the way
- `done` / `dropped` — terminal

`rp ready` excludes both `waiting` and anything with an unfinished `blocked_by`, and
computes readiness at query time from the dependency graph. Storing it
would mean every close-event has to cascade writes into unrelated tickets, which is
exactly the kind of write amplification that produces merge conflicts.

### 5.5 Epics, plans, templates, batches

**An epic is a ticket** with `type: epic`. Children point at it via `parent`. One
entity, one fold, one write path.

Three constraints, cheap now and expensive later:

- **One level of nesting.** `epic → task`, and stop. No tracker maps deeper cleanly,
  and unbounded trees bring cycle detection and recursive rollups for little benefit.
- **Epic status is derived, not stored.** An epic is `done` when its children are.
  Storing it means closing a child cascades a write into the parent — the write
  amplification pattern from 5.4.
- **`parent` is the only structural field.** No `children[]`, and no `blocks[]` beside
  `blocked_by`. Both are denormalization: every edge edit would write to two tickets,
  and two branches adding dependencies would conflict on a ticket neither is working
  on. Invert the graph at query time — microseconds at this scale.

**A plan is not a Rohrpost object at all.** Epics are durable structure; plans are the
transient reasoning that produced them — an ordering, a rationale, rejected
alternatives. A plan has no status and no assignee.
Planning lives in a separate tool that reads through `rp --json` and creates tickets
through `rp new --parent`. Nothing flows back; Rohrpost does not know a plan existed.

This also keeps proposed work out of accepted work: a rejected decomposition leaves a
discarded document rather than orphaned tickets to clean up.

**A template is a file**, `templates/<name>.toml`: default field values plus a body
skeleton. Hand-authored, committed, never in the log. Defaults may be at the top
level or under `[defaults]`, `[fields]`, or `[ticket]`; command-line values win.
`rp new "title" --template bug`.

**A batch is a label on claim events**, not a stored object: `actor:
"runner/claude-code@b-3"`. Batches are ephemeral dispatch units and do not need to
outlive the run. Promote to a first-class entity only if batch-level status is
genuinely needed.

---

## 6. The fold

Tickets are produced from the log, on every invocation, by:

1. Read all events from `archive/*.jsonl` then `log.jsonl`.
2. **Deduplicate by event `id`** (union merge can produce duplicates).
3. **Sort by `(ts, id)`** — ULID tiebreak makes this total and deterministic.
4. For each event in order, apply its payload field-by-field onto the ticket,
   recording `_fieldts[field] = ts`.
5. **Last write wins, per field** — not per record.

Per-field LWW is the whole point. Two runners updating `status` and `priority` on the
same ticket concurrently both win. Whole-record LWW would silently discard one of
them, and that class of bug is close to undebuggable after the fact.

Clock skew across machines is accepted. Timestamps come from the writing host and
are not corrected; a wrong clock means a wrong winner on one field. Vector clocks
would fix it and are not worth the complexity at this scale.

### 6.1 Compaction

`rp compact`:

1. Fold everything (archive and live log).
2. For every ticket terminal for more than `archive_after` (default 90d), append its
   events **from the live log** to `archive/log-<YYYY>-Q<N>.jsonl` (bucketed by the
   event's own timestamp). Events already in the archive stay where they are.
3. Rewrite `log.jsonl` with the remainder.

The archive append happens **before** the log rewrite: an interruption between the two
leaves duplicated events, which the fold removes on read, never lost ones.

**Compaction rewrites a union-merged file and therefore must only ever run on a clean
`main` with no open branches carrying unmerged events.** `rp compact` refuses if the
working tree is dirty or `HEAD` is not the configured default branch (`--force`
overrides). This is the one operation in the system that can lose data if run
carelessly.

---

## 7. Concurrency

**The guarantee.** Every append happens inside an exclusive lock on `.rohrpost/.lock`
and is one write of one line in append mode. Two writers therefore never interleave
half-lines: the lock serialises them, and a short write is rolled back, never resumed
(§3 principle 5). Readers stay lock-free and tolerate a torn tail — a partial final line
fails to decode and is skipped; the fold deduplicates, so nothing a reader briefly sees
wrong becomes state.

| Hazard | Mitigation |
|---|---|
| Two processes writing the log | Exclusive lock on `.lock` + append-mode writes |
| Two branches writing the log | `merge=union` + dedupe on read |
| Same ticket, same field, two branches | Per-field LWW by `ts` |
| Same ticket, different fields | Both survive — this is the common case |
| Duplicate events after merge | Dropped by `id` during fold |

**Platform mechanisms** (implementation notes, not contract):

- The lock is the Rust standard library's `File::lock` on `.rohrpost/.lock`: `flock` on
  Linux and macOS (advisory, per open file description), `LockFileEx` on Windows
  (mandatory, per handle). Both block until acquired and release when the holding
  process dies. Only `.lock` is ever locked — never the log — so the Windows
  enforcement cannot interfere with readers.
- Appends open the log in append mode: `O_APPEND` on POSIX (offset update and write
  are one atomic step on Linux) and `FILE_APPEND_DATA` on Windows. Each event is one
  `write_all` of one line inside the lock; a write that fails part-way is rolled back by
  truncating the file to its previous length rather than resumed.
- The lock is retained even where `O_APPEND` alone would suffice, for portability
  (NFS, Windows), for compaction's multi-step rewrite, and as the short-write backstop.

The contract is the guarantee above; either mechanism satisfies it. Tests map onto the
guarantees, not the syscalls — including a twelve-process concurrent-append race that
runs on all three platforms in CI.

---

## 8. Sync (removed)

v0.1 specified a bidirectional sync layer: shadow snapshots as a three-way merge base,
per-field resolution with conflict policies, `git merge-file` for prose bodies, and
per-tracker providers starting with GitHub. It was implemented, then **removed in v0.2**
as overkill: an order of magnitude more code than the store, with no workflow that
needed it. Anything that wants a remote tracker lives one level up (§3.1) and drives
`rp --json`, as every other consumer does. The decision is recorded in ADR 0001.

---

## 9. Comments and boundaries

Rohrpost stores **notes**: short, ticket-scoped, locally authored. A runner records
why it retried, a human records a caveat. That is all.

```
rp comment RP-a1b2 "retried with backoff, still 429s"
```

Appended as a `comment` event, folded into a `comments` list on the ticket. Text output
surfaces them via `rp show --include notes` (last 10); `rp show --json` returns the full
list, as does `rp comments <id>`. The default `rp show` (summary + body) omits notes. No
threading, no anchors, no resolution state.

### 9.1 What Rohrpost does not do

Everything reactive lives one level up, in the surrounding system, and Rohrpost has no
knowledge of it. Explicitly out of scope, permanently:

| Not in Rohrpost | Why |
|---|---|
| Mirroring tickets into GitHub, Jira, GitLab or Linear | Removed in v0.2 (§8): a merge base beside the data was elegant and unused |
| Ingesting remote comments | Not ticket-shaped. A review comment may concern a ticket, a policy, a runner, or nothing |
| PR review threads, anchors, resolution | Belongs with whatever watches PRs |
| Asking humans questions and awaiting answers | Requires a listener; Rohrpost has no daemon |
| Webhooks, event subscriptions, long-running processes | Same |
| Deciding *when* a ticket is waiting on input | Rohrpost records the status; something else decides it |
| CI results, build status, deployment state | Not ticket state |

**The dependency runs one way.** The higher-level system knows about Rohrpost and
drives it through the CLI. Rohrpost does not know the higher-level system exists. This
is what keeps a single `rp` binary useful standalone in any repo, which was the original
requirement and is worth more than any feature listed above.

### 9.2 The interface contract

When an outer system does drive Rohrpost, it does so through `rp` — never by writing
the log directly, however tempting.

- Every command takes `--json` and returns machine-readable output.
- Every write is idempotent where it can be: re-running `rp set <id> status=done` is a
  no-op event, not an error.
- Non-zero exit on failure, with the reason on stderr.
- The write surface stays small. If handling one external event needs six `rp` calls,
  that is a signal the boundary is wrong — not a reason to add a seventh command.

Decisions land in Rohrpost; discussion stays where it was written. A human answering a
blocking question elsewhere results in one `rp set` and one `rp comment` recording the
answer, not a mirrored thread.

## 10. CLI

```
rp init [--prefix ABC]                   scaffold .rohrpost/
rp new "title" [--template bug] [-p 1]   create ticket
rp ready [--limit N]                     unblocked, actionable work
rp show <id> [--include body,deps,notes] ticket; defaults to summary + body
rp tree <id>                             epic and its children
rp list [--status] [--label] [--parent] [--type] [--match]  query
rp claim <id>                            → in_progress, stamps actor
rp set <id> field=value ...              generic update (labels+=a,b / labels-=a)
rp close <id> [--reason "..."]           → done
rp drop <id> [--reason "..."]            → dropped
rp comment <id> "..."                    append local note
rp comments <id>                         all notes on a ticket
rp log [<id>]                            raw event history
rp compact [--force] [--archive-after N] archive + truncate (main only)
rp doctor                                integrity + config checks
rp stats                                 size distributions + fold timing
```

`--json` on every command. `--actor` on every mutation. `--body-file <path|->` on
`new`, `set` and `comment` for multi-line text without shell heredocs. Non-zero exit on
error: `1` for a domain failure, `2` for a usage error. `NO_COLOR` respected.
`rp ready --json` is the single most important call in the system — it is what
runners invoke to find work, and it must be fast and small.

### 10.1 `rp doctor`

Checks: log parses; no duplicate event ids after dedupe; every `blocked_by` and
`parent` resolves; no dependency cycles; `.gitattributes` contains the merge and
line-ending rules. Informational: how many legacy sync events (§5.2) the log carries.

This is the one place the pneumatic metaphor is allowed out: *"3 tickets stuck in the
tube for >14d"* is more memorable than "3 stale tickets", and nobody has to type it.

---

## 11. Implementation

- **Rust, standard library only.** One static binary per platform (Linux x86_64 and
  aarch64, macOS Apple silicon and Intel, Windows x86_64), built from a crate with an
  empty `[dependencies]` table. Agents invoke `rp` from bare containers, so the tool
  carries no runtime and no supply chain. Minimum Rust is 1.89, where `File::lock`
  stabilised.
- JSON (ordered objects, interned hot keys), the TOML subset used by `config.toml` and
  templates, argv parsing, RFC 3339 timestamps and id entropy (the OS-seeded
  `RandomState` hasher) are implemented in-tree, each a few hundred lines scoped to
  exactly what Rohrpost needs.
- `File::lock` / append mode for the exclusive lock and the atomic append (§7).

Performance envelope: 3 000 tickets ≈ 30 000 events ≈ 5 MB. A cold read + fold of that
log measures ~90 ms on a slow aarch64 development box and ~25 ms on a laptop — under
the 50 ms threshold the v0.1 decision rule (§13.2, D4) set. **There is therefore no
snapshot, no index and no staleness protocol**: every `rp` invocation folds the log.
`rp stats` reports `fold_ms` so this can be revisited with data.

---

## 12. Build order

| Phase | Contents | Done when |
|---|---|---|
| **0** | Event log, fold, lock, ids, `new`/`ready`/`show`/`claim`/`set`/`close` | A runner can work a ticket end to end — **done** |
| **1** | ~~Shadow store, three-way merge, GitHub provider, `sync`, `conflicts`~~ | Built, then removed in v0.2 (§8) |
| **2** | Templates, `doctor`, `compact`, `stats` — **done**; sidecar bodies pending | Usable by someone who is not you |
| **2.5** | Nothing. Resist adding a listener here | — |
| **3** | Batches as first-class, sidecar bodies | Only when volume demands it |

Do not build features before feeling their absence. The sync layer was an order of
magnitude more work than the store and was removed unused — the lesson of v0.1.

---

## 13. Open questions

Deliberately unresolved. Nothing is built yet, so most of these are unanswerable until
phase 0 has run against real work.

1. **Bodies inline or sidecar?** *Resolved (2026-08-12) — size-triggered spill.* See
   §13.2 for the evidence and the design: bodies stay inline; a body exceeding a
   configurable threshold (default 4096 bytes) spills to `bodies/<id>.md` on write, and
   `rp show` resolves either form transparently. §5.3 already permits both shapes, so
   this needs no schema change. Not implemented yet (phase 2).
2. **Is `tickets.jsonl` ever committed?** *Resolved (2026-09-03) — there is no
   `tickets.jsonl`.* The native fold made the cache unnecessary (§11).
3. **Do runners write events directly or always shell out to `rp`?** Direct writes are
   faster; shelling out keeps validation in one place. Start with shelling out.
4. **Does `rp` commit, or leave staging to the caller?** Committing is convenient and
   surprising. Lean toward `--commit` being opt-in.
5. **Does the planner need more than `rp show --json`?** If it wants fields Rohrpost
   would not otherwise carry, the boundary in 3.1 is wrong and planning belongs with
   the bus instead.
6. **Are `type` values right?** `task | bug | spike | epic` is a guess. Types are cheap
   to add and awkward to remove.

### 13.1 What is actually load-bearing

Only one decision here has real switching cost: **the event log**. It is the only
thing that accumulates data — everything else is code that gets rewritten anyway.
Well-formed append-only events with disciplined `id`, `ts`, `op` and `actor` can be
folded into whatever schema turns out to be right. Field names, status values, type
lists and CLI shape are all cheap to change later.

So: write events generously, keep the envelope strict, and do not over-invest in the
field set. Build phase 0, run it on something real, and re-read this document.

### 13.2 Bodies: inline or sidecar — decision (2026-08-12)

Open question §13 #1. The decision rule was pre-registered before measurement: go
sidecar if any of D1/D3/D4/D5 fires; stay inline if none do; D2 alone is too weak.
Measurement (table below) found none of D3/D4/D5 fire and D1/D2 unmeasurable without
real tickets — which the rule reads as "stay inline." That reading is overridden here by
a workload-structure argument the rule did not capture: the body-size distribution is
bimodal, not single, and that settles the *shape* without a corpus.

Instrumentation: `rp stats --json` derives every signal straight from the log
(there are no hot-path counters — line length, body size and the over-`PIPE_BUF`
count are all computable by decoding events, so instrumentation is free at append
time and retroactive). Only `fold_ms` is a live timing. The experiments live in
`tests/` (`test_append_integrity`, `test_union_merge_long_lines`,
`test_body_roundtrip`, `test_merge_body_fidelity`, `test_fold_invariants`) and
`scripts/` (`bench_fold_cost`, `bench_ready_context`); the concurrency races are
opt-in under `ROHRPOST_RUN_EXPERIMENTS=1`.

| Signal | Threshold | Observed (2026-08-12) | Fires? |
|---|---|---|---|
| D1 body p95 bytes | > 4000 | not measurable yet — no real tickets; `rp stats` will supply it | **DEFERRED** |
| D2 body p50 bytes | > 1000 | not measurable yet | DEFERRED (weak alone) |
| D3 lock-path share | > 5% | premise void on Linux: E2 shows **no corruption without the lock even at 64 KB** — `O_APPEND` makes each single-`write()` append atomic on regular files, and `PIPE_BUF` governs pipes/FIFOs, not the log file. The lock is defensive, not integrity-load-bearing here. | **NO** (hazard theoretical) |
| D4 fold wall time | > 50 ms | ~109 ms cold fold at 3000×10 events (realistic 200 B body), but **apply-bound** (~85 ms replaying 30 k events); body size adds only ~8 ms across 200 B→2 KB. The >50 ms is a fold-scale property, not a body property. | **NO** (not body-driven) |
| D5 semantic codec coupling | any | none — E5 round-trips byte-identically across the adversarial corpus (markdown fences, JSON escapes, emoji/CJK/RTL/ZWJ, `\u0000`/U+2028/U+2029, a body that is itself a JSONL event line, 50 KB). | **NO** |

**Decision: size-triggered spill.** Bodies stay inline; a body exceeding a
configurable threshold (default 4096 bytes) spills to `bodies/<id>.md` on write, and
`rp show` resolves either form transparently. §5.3 already permits both shapes, so this
needs no schema change.

The workload is not one distribution, so a real-ticket sample is not needed to decide
the *shape* — only to tune the threshold:

- Epic bodies are spec fragments: kilobytes, repeatedly edited.
- Task bodies are a paragraph written once.

A size threshold catches the large epic bodies regardless of sample size; the deferred
signals (D1/D2) would tune the 4096-byte default, not reverse the design. E5 settles the
strongest objection to spilling at all: bodies round-trip byte-identically across the
adversarial corpus, so moving one to a file and back is transparent to the codec.

**Re-evaluation trigger:** the design is firm; the 4096-byte default is what real data
would tune. Revisit it once `rp stats` runs over ≥100 real tickets and supplies D1/D2.

Not implemented yet (phase 2) — tracked as follow-up tickets.

**Side findings the experiments surfaced:**

- **E7 bug, fixed.** `rp ready --json` / `rp list --json` used to include every
  ticket's full body, so the work-queue view charged the agent for prose. The
  short shape (`fold::Shape::SHORT`) now omits the body, comments and `_fieldts`,
  pinned by an end-to-end test.

---

## Appendix A — Rejected alternatives

**TOON as the storage format.** It is a prompt-encoding format — the token savings
come from uniform arrays of objects collapsing to tabular form, which is precisely
what ticket bodies are not. The spec is a working draft that reached v4.1 in under a
year; fine for something regenerated per call, wrong for files that must parse in
2031. *Kept:* `rp ready --format toon` as an output flag, measured on real data. But
the token win comes from sending 8 tickets instead of 400, not from the encoding.

**One markdown file per ticket.** Better diffs, native prose, free `git log --follow`.
Rejected because agents given a file and an editor will eventually mangle YAML
frontmatter — unquoted colons, `no` parsing as false, indentation drift — and
file-per-ticket has no chokepoint to catch it. At 95% agent traffic, a single
validated write path is worth more than reviewable diffs. Also avoids building and
invalidating an index.

**Waiting for 100 real tickets before deciding inline vs sidecar bodies.** Rejected:
the epic/task size split is a known workflow property — epics carry spec fragments,
tasks carry a paragraph — not something a random sample would reveal, and a size
threshold captures it directly. Inline→sidecar migration also stays cheap if the call
is wrong: fold, write N files, rewrite the body field as a path. See §13.2.

**Dolt / SQLite backing store (the beads approach).** Best data model of the
alternatives, but it makes the tool git-adjacent rather than git-native, adds a
binary dependency, and introduces schema migrations. seeds exists specifically as its
replacement in that ecosystem, which is itself informative.

**Forking or wrapping seeds.** Wrapping means requiring both Bun and Python and
inheriting their whole-record LWW semantics; forking means tracking an actively
developed single-maintainer project forever. The store is ~500 lines. Take the
design, write the code.

**Comments as a first-class synced entity** (review threads, anchors, resolution
state, `rp ask` / `rp review`, echo prevention via shadow comment maps and marker
footers). Designed in full, then cut. Remote comments are not ticket-shaped and
ingesting them requires a listener, which breaks the no-daemon property and the
standalone-tool property together. Moved up a level. What survives is local notes.

**Bidirectional sync with SaaS trackers** (v0.1 §8: shadow merge bases, per-field
three-way merge, `git merge-file` for bodies, a GitHub provider). Built, then removed
in v0.2: the expensive half of the system served no workflow. See ADR 0001.

**A Python runtime** (v0.1: Python 3.14 + `msgspec` + `httpx` via `uv`). Replaced by a
dependency-free Rust binary in v0.2: the Windows pass showed that most of the effort
went into the runtime (interpreter, venv, locking shims, text-mode descriptors, per-shell
wrappers) rather than into tickets, and agents run `rp` from bare containers.

**Plans as a Rohrpost entity** (either `type: plan` or a separate `plans.jsonl`).
Epics are durable structure and stay; plans are transient reasoning and moved out. An
epic has status and children; a plan has neither.

**`blocks[]` stored alongside `blocked_by[]`, and `children[]` alongside `parent`**
(as seeds does). Denormalized reverse edges mean every structural edit writes two
tickets, and two branches adding dependencies conflict on a ticket neither is touching.
Invert the graph at query time instead.

**Whole-array writes for `labels`.** Silently loses one side when two runners each add
a label. Replaced by `labels+` / `labels-` set ops.

**A seeds importer.** No migration path exists for a personal project. Cut.

**German vocabulary throughout** (Zettel, Vordruck, Fahrplan, Stapel, Aktenzeichen,
Druck/Sog, Rückstau). Charming, coherent, and a tax on every future reader including
future you. Kept the name only.

---

## Appendix B — Vocabulary reference

For the README, should the pneumatic language ever be wanted in prose: capsules
travel *tubes* between *stations*, pushed by the *blower*; a *diverter* routes them;
work piles up as *backpressure*; a stuck capsule is a *clog*; undeliverable mail
returns to sender. None of this appears in the schema or the CLI.
