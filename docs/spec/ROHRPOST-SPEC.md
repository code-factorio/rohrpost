# Rohrpost — Specification

**Status:** Draft v0.1 · **Date:** 2026-08-11 · **Author:** Vinzenz Feenstra

A git-native ticket system for agentic coding workflows. Local files are canonical;
GitHub, Jira, GitLab and Linear are projections that get synced.

---

## 1. What it is

Rohrpost keeps work items as files in the repository they belong to. Coding agents
create, claim, update and close them without leaving the repo. A sync layer mirrors
them bidirectionally into whatever SaaS tracker the humans and stakeholders use.

The differentiator is not the storage format — it is being the abstraction *over*
four issue trackers, with the repo as the source of truth.

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
| A linked SaaS tracker | `remote` |
| Cross-reference to a remote | `remote ref` |
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
Rohrpost never calls out, never listens, never runs as a daemon. This is what keeps
`uvx rohrpost` useful standalone in any repository — the original requirement, and
worth more than any feature it excludes.

Reading through the CLI is not coupling: a planner calling `rp show --json` for a body
has the same access a runner does. Coupling is when a consumer needs *fields Rohrpost
would not otherwise have*. Planner state, bus state, and runner state live with those
components, keyed by ticket id.

---

## 4. On-disk layout

```
.rohrpost/
├── config.toml              # committed — remotes, field mappings, policy
├── log.jsonl                # committed — append-only event log. TRUTH.
├── archive/
│   └── log-2026-Q2.jsonl    # committed — compacted historical events
├── shadow/
│   └── jira/PROJ-123.json   # committed — last-synced remote state (merge base)
├── templates/
│   └── bug.toml             # committed — hand-authored
├── bodies/                  # committed — phase 2, optional
│   └── RP-a1b2.md
└── tickets.jsonl            # GITIGNORED — folded snapshot, regenerable
```

`.gitattributes`:

```gitattributes
.rohrpost/log.jsonl          merge=union
.rohrpost/archive/*.jsonl    merge=union
.rohrpost/shadow/**/*.json   merge=ours
.rohrpost/tickets.jsonl      linguist-generated
```

`merge=union` keeps both sides' appended lines instead of writing conflict markers.
It is safe here **only because the log is strictly append-only** and every event
carries a unique id, so duplicates are removed on read. Shadow files use `merge=ours`
because a stale merge base is harmless — the next sync overwrites it — while a
conflicted one blocks the sync entirely.

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
would have been a defect: three repos syncing into one Jira project would produce
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
  "ticket": "RP-a1b2c3",
  "op":     "set",                          // see below
  "actor":  "runner/claude-code@b-3",       // or "user/<git email>", "remote/jira"
  "set":    { "status": "in_progress" }     // op-dependent payload
}
```

`actor` is load-bearing: it distinguishes a human decision from a runner write from a
change that arrived through sync. Three namespaces — `user/*` resolved from
`git config user.email`, `runner/<agent>@<batch>`, and `remote/<name>` for anything
sync appends. Never hardcode a name.

**Operations:**

| `op` | Payload | Meaning |
|---|---|---|
| `create` | `set: {...}` | Ticket comes into existence with initial fields |
| `set` | `set: {field: value}` | Field-level update. The workhorse |
| `set` | `set: {"labels+": [...]}` / `{"labels-": [...]}` | Set add/remove — see below |
| `comment` | `text: str` | Append-only discussion entry |
| `link` | `remote: str, ref: str` | Bind ticket to a remote tracker item |
| `unlink` | `remote: str` | Remove that binding |
| `synced` | `remote: str, at: str` | Records a completed sync round |

There is no `close` or `claim` op — both are `set` on `status`. Fewer ops means a
simpler fold and fewer schema decisions to regret.

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

### 5.3 Ticket (folded shape, `tickets.jsonl`)

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
  "remotes":    { "jira": "PROJ-123", "github": "88" },
  "last_close_reason": null,     // derived from the most recent close event
  "created":    "2026-08-11T09:14:02Z",
  "updated":    "2026-08-11T11:02:38Z",
  "_fieldts":   { "status": "2026-08-11T11:02:38Z", "priority": "…" }
}
```

`_fieldts` carries the last-write timestamp per field. It is what makes field-level
merge and incremental sync possible, and it is why the fold is not simply
"last event wins".

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

**An epic is a ticket** with `type: epic`. Children point at it via `parent`. Epics
mirror to Jira Epics and Linear Projects, so making them a separate entity would mean
implementing sync twice — the expensive half of the system — for no gain.

Three constraints, cheap now and expensive later:

- **One level of nesting.** `epic → task`, and stop. No tracker maps deeper cleanly,
  and unbounded trees bring cycle detection and recursive rollups for little benefit.
- **Epic status is derived, not stored.** An epic is `done` when its children are.
  Storing it means closing a child cascades a write into the parent — the write
  amplification pattern from 5.4. Sync pushes the derived value outward; inbound epic
  status changes are advisory and do not overwrite the computation.
- **`parent` is the only structural field.** No `children[]`, and no `blocks[]` beside
  `blocked_by`. Both are denormalization: every edge edit would write to two tickets,
  and two branches adding dependencies would conflict on a ticket neither is working
  on. Invert the graph at query time — microseconds at this scale.

**A plan is not a Rohrpost object at all.** Epics are durable structure; plans are the
transient reasoning that produced them — an ordering, a rationale, rejected
alternatives. A plan has no status, no assignee, and must never appear in Jira.
Planning lives in a separate tool that reads through `rp --json` and creates tickets
through `rp new --parent`. Nothing flows back; Rohrpost does not know a plan existed.

This also keeps proposed work out of accepted work: a rejected decomposition leaves a
discarded document rather than orphaned tickets to clean up.

**A template is a file**, `templates/<name>.toml`: default field values plus a body
skeleton. Hand-authored, committed, never in the log. `rp new --template bug`.

**A batch is a label on claim events**, not a stored object: `actor:
"runner/claude-code@b-3"`. Batches are ephemeral dispatch units and do not need to
outlive the run. Promote to a first-class entity only if batch-level status is
genuinely needed.

---

## 6. The fold

`tickets.jsonl` is produced from `log.jsonl` by:

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

1. Fold everything.
2. Move all events belonging to tickets terminal for more than `archive_after`
   (default 90d) into `archive/log-<YYYY>-Q<N>.jsonl`.
3. Rewrite `log.jsonl` with the remainder.

**Compaction rewrites a union-merged file and therefore must only ever run on a clean
`main` with no open branches carrying unmerged events.** `rp compact` refuses if the
working tree is dirty or `HEAD` is not the configured default branch. This is the one
operation in the system that can lose data if run carelessly.

---

## 7. Concurrency

| Hazard | Mitigation |
|---|---|
| Two processes writing the log | Advisory lock (`fcntl.flock`) + `O_APPEND` |
| Two branches writing the log | `merge=union` + dedupe on read |
| Same ticket, same field, two branches | Per-field LWW by `ts` |
| Same ticket, different fields | Both survive — this is the common case |
| Duplicate events after merge | Dropped by `id` during fold |
| Stale `tickets.jsonl` | Regenerated when mtime is older than `log.jsonl` |

Each append is a single `write()` of one line under `O_APPEND`, which is atomic on
local filesystems for writes below `PIPE_BUF`. Lines exceeding that (long bodies) go
through the lock. Phase 2's sidecar bodies remove the problem entirely.

---

## 8. Sync

The hard part, and the part neither beads nor seeds has to solve: **the remote can
change behind your back.** Every sync is therefore a three-way merge, and a three-way
merge needs a base.

### 8.1 Shadow snapshots

`shadow/<remote>/<ref>.json` holds the remote's field values **as of the last
successful sync**. It is the merge base. Without it you cannot distinguish "local
changed" from "remote changed" from "both changed", and you will either clobber
people's Jira edits or refuse to sync anything.

### 8.2 Per-field resolution

For each mapped field, with `base` = shadow, `local` = folded ticket, `remote` = live:

| Condition | Action |
|---|---|
| `local == remote` | nothing |
| `local == base`, `remote != base` | take remote → append `set` event, actor `remote/<name>` |
| `remote == base`, `local != base` | push local to remote |
| all three differ | **conflict** → apply policy |

Conflict policy per remote in config: `flag` (default), `local`, `remote`.
`flag` writes a `set` event moving the ticket to `status: review`, adds a
`conflict:<remote>` label, and records both values in a comment. `rp conflicts` lists
them; `rp resolve <id> --take local|remote` clears them.

### 8.3 Body merge

Prose is the one field where per-field LWW is unacceptable — it throws away real
human writing. Merge bodies with a genuine three-way text merge by shelling out to
`git merge-file --stdin` (present wherever Rohrpost runs, well-tested, no dependency).
On conflict, keep the markers in the body and flag as above.

### 8.4 Sync round

```
rp sync [remote] [--dry-run]
  1. pull remote items changed since shadow watermark (updated_at / ETag)
  2. for each linked ticket: three-way merge per field (8.2), body via 8.3
  3. append resulting set-events with actor "remote/<name>"
  4. push locally-won fields to the remote
  5. rewrite shadow from post-sync remote state
  6. append one `synced` event per remote
```

Steps 3 and 5 must be ordered so a crash between them leaves a stale shadow rather
than a lost update: a stale shadow causes a redundant merge next round, which is
idempotent. The reverse ordering loses data.

`--dry-run` prints the plan and touches nothing. It is the default in CI.

### 8.5 Providers

| Remote | Access | Notes |
|---|---|---|
| GitHub | REST via `httpx` | **Build first** — simplest auth, fastest feedback |
| GitLab | `python-gitlab` | Mature |
| Jira | `jira` or REST | Field mapping is the work; custom fields vary per install |
| Linear | GraphQL via `httpx` | No Python SDK. Direct GraphQL is an afternoon |

Mapping lives in `config.toml`, per remote, and is explicit — no field is synced
unless it is listed. Unmapped remote fields are preserved untouched on push.

```toml
[project]
prefix = "FAC"

[remotes.jira]
url     = "https://acme.atlassian.net"
project = "PROJ"
policy  = "flag"

[remotes.jira.fields]
title    = "summary"
body     = "description"
status   = { open = "To Do", in_progress = "In Progress", done = "Done" }
priority = "priority"
```

---

## 9. Comments and boundaries

Rohrpost stores **notes**: short, ticket-scoped, locally authored, never synced.
A runner records why it retried, a human records a caveat. That is all.

```
rp comment RP-a1b2 "retried with backoff, still 429s"
```

Appended as a `comment` event, folded into a `comments` list on the ticket, returned
by `rp show` (last 10) and `rp comments <id>` (all). No threading, no anchors, no
resolution state, no sync in either direction.

### 9.1 What Rohrpost does not do

Everything reactive lives one level up, in the surrounding system, and Rohrpost has no
knowledge of it. Explicitly out of scope, permanently:

| Not in Rohrpost | Why |
|---|---|
| Ingesting remote comments | Not ticket-shaped. A review comment may concern a ticket, a policy, a runner, or nothing |
| PR review threads, anchors, resolution | Belongs with whatever watches PRs |
| Asking humans questions and awaiting answers | Requires a listener; Rohrpost has no daemon |
| Webhooks, event subscriptions, long-running processes | Same |
| Deciding *when* a ticket is waiting on input | Rohrpost records the status; something else decides it |
| CI results, build status, deployment state | Not ticket state |

**The dependency runs one way.** The higher-level system knows about Rohrpost and
drives it through the CLI. Rohrpost does not know the higher-level system exists. This
is what keeps `uvx rohrpost` useful standalone in any repo, which was the original
requirement and is worth more than any feature listed above.

Section 8 sync stays in Rohrpost because field sync needs a merge base and a merge
base needs to live beside the data. Reactive ingestion does not.

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
blocking question in Jira results in one `rp set` and one `rp comment` recording the
answer, not a mirrored thread.

## 10. CLI

```
rp init                                  scaffold .rohrpost/
rp new "title" [--template bug] [-p 1]   create ticket
rp ready [--limit N]                     unblocked, actionable work
rp show <id> [--include body,deps,notes] ticket; defaults to summary + body
rp tree <id>                             epic and its children
rp list [--status] [--label] [--parent]  query
rp claim <id>                            → in_progress, stamps actor
rp set <id> field=value ...              generic update
rp close <id> [--reason "..."]           → done
rp drop <id> [--reason "..."]            → dropped
rp comment <id> "..."                    append local note
rp comments <id>                         all notes on a ticket
rp link <id> <remote> <ref>              bind to remote item
rp unlink <id> <remote>                   remove a remote binding
rp sync [remote] [--dry-run]             three-way sync
rp conflicts                             list flagged tickets
rp resolve <id> --take local|remote      clear a conflict
rp log [<id>]                            raw event history
rp compact                               archive + truncate (main only)
rp doctor                                integrity + config checks
```

`--json` on every command. Non-zero exit on error. `NO_COLOR` respected.
`rp ready --json` is the single most important call in the system — it is what
runners invoke to find work, and it must be fast and small.

### 10.1 `rp doctor`

Checks: log parses; no duplicate event ids after dedupe; every `blocked_by` and
`parent` resolves; no dependency cycles; every `remotes` entry has a shadow file;
`.gitattributes` contains the union-merge rules; remote credentials present and
authenticating; `tickets.jsonl` matches a fresh fold.

This is the one place the pneumatic metaphor is allowed out: *"3 tickets stuck in the
tube for >14d"* is more memorable than "3 stale tickets", and nobody has to type it.

---

## 11. Implementation

- **Python ≥ 3.12**, distributed via `uv` — `uvx rohrpost` runs with no prerequisite
  toolchain, which matters because agents invoke this from bare containers.
- **`msgspec`** for JSONL encode/decode with schema validation. Substantially faster
  than pydantic for line-oriented parsing and gives struct types for free.
- **`httpx`** for all providers. One client, sync API, explicit timeouts.
- **`fcntl.flock`** for the advisory lock. No dependency, correct on Linux and macOS.
- Stdlib `secrets` + `base64` for ids; a small ULID helper rather than a dependency.

Performance envelope: 3 000 tickets ≈ 30 000 events ≈ 6 MB. A full fold with msgspec
is single-digit milliseconds. **No index, no cache invalidation, no staleness
protocol** — `tickets.jsonl` exists only to avoid re-folding on every CLI invocation
and can be deleted at any moment.

---

## 12. Build order

| Phase | Contents | Done when |
|---|---|---|
| **0** | Event log, fold, lock, ids, `new`/`ready`/`show`/`claim`/`set`/`close` | A runner can work a ticket end to end |
| **0** | — a weekend of work. Build it, run it on something real, then re-read this spec | |
| **1** | Shadow store, three-way merge, **GitHub provider**, `sync`, `conflicts` | A GitHub issue and a ticket stay in step through edits on both sides |
| **2** | Templates, plans, sidecar bodies, `doctor` | Usable by someone who is not you |
| **2.5** | Nothing. Resist adding a listener here | — |
| **3** | Jira, Linear, GitLab | |
| **4** | Compaction, archive, batches as first-class | Only when volume demands it |

Do not build phase 2+ features before feeling their absence. The sync layer is an
order of magnitude more work than the store; spend the effort there.

---

## 13. Open questions

Deliberately unresolved. Nothing is built yet, so most of these are unanswerable until
phase 0 has run against real work.

1. **Bodies inline or sidecar?** Inline is simpler; sidecar merges better and is the
   field that syncs with Jira descriptions. Decide after observing real ticket sizes.
2. **Is `tickets.jsonl` ever committed?** Currently no. Committing enables `git grep`
   and cheap CI reads at the cost of churn on every commit.
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

**Plans as a Rohrpost entity** (either `type: plan` or a separate `plans.jsonl`).
Epics are durable structure and stay; plans are transient reasoning and moved out. The
distinction that settled it: an epic mirrors to a Jira Epic and therefore wants the
ticket sync path, while a plan must never reach Jira at all.

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
