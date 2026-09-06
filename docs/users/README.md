# Rohrpost — User Guide

Rohrpost is a ticket system that lives in your git repository. The binary is
**`rp`**. Tickets are files under `.rohrpost/`; the append-only event log
(`log.jsonl`) is the source of truth, and a ticket is a *fold* over that log.

This guide is for humans and coding agents who drive `rp`. It is **agent-first**:
`--json` is available on every command and is the preferred interface for
automation; the default human-readable output is for terminals.

---

## Install

`rp` is a single static binary with no runtime requirements. Download the build
for your platform from the
[releases page](https://github.com/code-factorio/rohrpost/releases) — Linux
(x86_64, aarch64), macOS (Apple silicon, Intel), Windows (x86_64) — and put it
on your `PATH`. Or build it yourself with a Rust toolchain (1.89+):

```bash
cargo install --git https://github.com/code-factorio/rohrpost --locked
```

`rp` discovers the nearest `.rohrpost/` by walking up from the current directory,
so it works from anywhere inside a repo.

---

## First run: `rp init`

```bash
rp init                     # scaffold .rohrpost/ at the repo root
rp init --prefix FAC        # set the display prefix (2-5 uppercase letters)
```

`init` proposes a prefix from the directory name and writes:

- `.rohrpost/config.toml` — the display prefix and the compaction branch
- `.rohrpost/log.jsonl` — the append-only event log (committed; **truth**)
- `.rohrpost/archive/`, `.rohrpost/templates/`
- `.gitattributes` merge and line-ending rules for the log

`init` is idempotent — re-running fills in anything missing without clobbering
an existing config.

**The prefix is display-only.** It never enters the log, so renaming it in
`config.toml` re-renders every ticket id with no migration. `rp` accepts either
the bare id (`a1b2c3`) or the rendered form (`FAC-a1b2c3`); agents usually drop
the prefix.

---

## The ticket lifecycle

```
open → in_progress → review → done
                       ↘ waiting        (stalled on human input)
open → dropped                          (terminal)
```

`ready` is **derived, never stored**: a ticket is ready when it is `open` and
every `blocked_by` is `done`. Closing a dependency therefore unblocks its
dependents with no extra writes.

### Create

```bash
rp new "Fix token refresh race"
rp new "Spike: auth options" --type spike -p 1
rp new "Child task" --parent <epic-id> --label auth --blocked-by <id> --body "..."
rp new "Design notes" --body-file notes.md
rp new "Handle auth failures" --template bug
```

Flags: `--type task|bug|spike|epic|saga`, `-p/--priority 0..4` (0 highest), `--label`
(repeatable), `--blocked-by` (repeatable), `--parent`, `--assignee`, `--body`.
`--template NAME` loads defaults from `.rohrpost/templates/NAME.toml`; command-line
values override template defaults. A template may use top-level fields or a
`[defaults]`, `[fields]`, or `[ticket]` table:

```toml
[defaults]
type = "bug"
priority = 1
labels = ["needs-triage"]
body = """
## Steps to reproduce

"""
```

### Multi-line bodies: `--body-file`

`--body-file <path|->` reads text from a file, or from stdin with `-`, always
decoded as strict UTF-8 — no locale guessing. It exists on `new`, `comment`, and
`set` so multi-line prose never needs a shell heredoc (which PowerShell and cmd
do not have):

```bash
rp new "Design notes" --body-file notes.md
rp set <id> status=review --body-file review.md     # composes with other assignments
rp comment <id> --body-file findings.md             # replaces the note text argument
printf '%s\n' "piped prose" | rp new "t" --body-file -
```

On `new` it is mutually exclusive with `--body`, on `comment` with the positional
note text, and on `set` with a `body=` assignment — giving both is a usage error
(exit 2), as is a missing file or bytes that are not valid UTF-8. An explicit
`--body-file` (like `--body`) beats a template's `body` default; an empty file
yields an empty body.

### Find work

```bash
rp ready                  # the actionable queue: open, unblocked leaves (no epics, no sagas)
rp ready --limit 5
rp ready --json           # machine-readable
```

`rp ready --json` is the single most important call for a runner — it is how an
agent finds work. It is fast and small: the list shapes never carry bodies.

### Work a ticket

```bash
rp claim <id>             # -> in_progress, stamps the actor as assignee
rp set <id> status=review priority=1
rp set <id> labels+=auth,bug labels-=ui
rp comment <id> "retried with backoff, still 429s"
rp close <id> --reason "implemented with exponential backoff"
rp drop <id> --reason "wontfix"
```

`set` is the generic field update. Set fields (`labels`, `blocked_by`) use `+=`
(add) and `-=` (remove) so concurrent edits from two runners compose instead of
clobbering each other. An empty value clears a nullable scalar: `rp set <id>
parent=` detaches a ticket from its epic or saga, and `assignee=` and `body=`
work the same way.

All mutations are **idempotent**: re-running `rp close <id>` on an already-done
ticket is a no-op (it appends nothing), not an error.

### Read

```bash
rp show <id>                          # summary + body
rp show <id> --include body,deps,notes,fieldts
rp comments <id>                      # all local notes
rp tree <epic-or-saga-id>             # the whole subtree, every line with its derived status
rp list --status open --label auth    # query
rp list --status ready                # derived statuses are queryable
rp list --match "token refresh"       # case-insensitive substring of the title
rp log [<id>]                         # raw event history
```

---

## Statuses and types

| Status        | Meaning                                          |
| ------------- | ------------------------------------------------ |
| `open`        | exists, not yet actionable                       |
| `ready`       | **derived**: `open` and all `blocked_by` done    |
| `in_progress` | a runner holds it                                |
| `review`      | work pushed, awaiting a gate                     |
| `waiting`     | stalled on human input                           |
| `done`        | terminal — completed                             |
| `dropped`     | terminal — abandoned                             |

Types: `task`, `bug`, `spike`, `epic`, `saga`. The first three are **leaves**: they
carry work and parent nothing. An **epic** and a **saga** own children through the
children's `parent` field, and a parent with children shows a status derived from
them instead of its own — see [Epics and sagas](#epics-and-sagas).

---

## Epics and sagas

Open an epic when a deliverable needs more than one leaf. Open a saga only when a
second epic appears for the same outcome: create the saga, then set both epics'
parent to it. Whoever creates the second epic opens the saga, agent or human. Never
open a saga for one epic, or for a concern that cuts across epics owned elsewhere;
that is a label. A leaf goes under the epic it belongs to. A leaf that belongs to
the outcome and to no epic, such as a spike that clears fog or the ADR that records
the decision, goes under the saga. A saga whose children are all leaves is an epic
with the wrong type.

A worked example. Day one is the normal case: one deliverable, one epic.

```bash
rp new "Magic-link sign-in" --type epic --label auth                                  # → RP-3f8xa1
rp new "Send the sign-in mail" --parent RP-3f8xa1 --label auth                        # → RP-c1q7we
rp new "Verify the link token" --parent RP-3f8xa1 --label auth --blocked-by RP-c1q7we # → RP-x9r2ht
```

Weeks later a second epic for the same outcome appears. Passwordless sign-in is not
done when magic links ship, and not done when passkeys ship. That is the moment a
saga exists, and the actor creating the second epic opens it:

```bash
rp new "Passkey sign-in" --type epic --label auth                                     # → RP-b6nd4z
rp new "Passwordless sign-in" --type saga                                             # → RP-7k2m9q
rp set RP-3f8xa1 parent=RP-7k2m9q
rp set RP-b6nd4z parent=RP-7k2m9q
```

Neither epic is re-typed or touched otherwise. Work that belongs to the outcome but
to neither epic goes directly under the saga:

```bash
rp new "Spike: one session record for both flows" --type spike --parent RP-7k2m9q     # → RP-2wjy6e
```

A concern that cuts across the epics is a label, never a third tier. Audit logging
touches both flows, so the leaves that carry it get `audit`:

```bash
rp new "Log every passkey enrolment" --parent RP-b6nd4z --label auth --label audit    # → RP-m5t8vk
rp set RP-c1q7we labels+=audit
```

`rp tree` on the saga renders the whole subtree, two levels deep, every line carrying
the derived status:

```
RP-7k2m9q  [open]  saga  p2  Passwordless sign-in
  RP-3f8xa1  [open]  epic  p2  Magic-link sign-in
    RP-c1q7we  [done]  task  p2  Send the sign-in mail
    RP-x9r2ht  [in_progress]  task  p2  Verify the link token
  RP-b6nd4z  [open]  epic  p2  Passkey sign-in
    RP-m5t8vk  [open]  task  p2  Log every passkey enrolment
  RP-2wjy6e  [open]  spike  p2  Spike: one session record for both flows
```

The status of the saga and of both epics is **derived, never written**: `dropped`
when every child is dropped, `done` when every child is settled (`done` or
`dropped`) and at least one is `done`, `open` otherwise, and an epic counts toward
its saga by its own derived status. Closing the last leaf turns every line above it
`done` with no further command, and a status write on a parent with open children
is refused (the no-op that equals the derived status is accepted, as every no-op is):

```
$ rp close RP-7k2m9q
rp: cannot close RP-7k2m9q: it has 3 open children (RP-3f8xa1, RP-b6nd4z, RP-2wjy6e)
```

The shape is bounded: `saga → epic → leaf`, and stop. A saga sits under nothing, an
epic under a saga or nothing, a leaf under a saga, an epic, or nothing. Every write
that sets `type` or `parent` is checked against the shape it would leave behind and
refused whole when that breaks the rule (`cannot set type=task on RP-x: it has 3
children (...)`); to demote an epic, move or drop its children first. `rp` keeps no
ancestry: a leaf's epic is its `parent`, the saga is the epic's `parent`, so two
`rp show` calls reach it.

Three things that are **not** a saga:

- One epic, however large. It stays a standalone epic.
- "Windows support" across three epics owned elsewhere. That is the label `windows`.
- A saga whose children are all leaves. That is an epic with the wrong type.

---

## Actors

Every event records who did it under one of two namespaces:

- `user/<git config user.email>` — a human (the default)
- `runner/<agent>@<batch>` — a coding agent. Set via the `ROHRPOST_RUNNER` and
  `ROHRPOST_BATCH` env vars, or `--actor`.

`--actor` overrides everything; `ROHRPOST_ACTOR` overrides the env-derived form.

---

## Integrity and maintenance

```bash
rp doctor        # log parses; no dup ids; refs resolve; tier rule; no cycles; gitattributes rules
rp compact       # archive tickets terminal for >90d; main branch only
rp stats         # body/line size distributions, cold fold timing
```

`doctor` is the one place the pneumatic metaphor is allowed out: it reports
whether anything is "stuck in the tube", and exits non-zero when something needs
attention. `compact` is the only operation that rewrites the log, so it refuses
unless the tree is clean and `HEAD` is on the default branch (`main`, or
`default_branch` in `config.toml`; `--force` overrides). It moves the events of
long-terminal tickets from `log.jsonl` into `archive/log-<YYYY>-Q<N>.jsonl`;
both stay committed and both are read on every fold.

---

## JSON output

Every command takes `--json` and returns structured output. Tickets render as:

```jsonc
{
  "id": "FAC-a1b2c3", "title": "...", "type": "task", "status": "open",
  "priority": 2, "parent": null, "blocked_by": [], "labels": ["auth"],
  "assignee": null, "body": null, "last_close_reason": null,
  "created": "2026-08-11T09:14:02.000Z", "updated": "2026-08-11T11:02:38.000Z",
  "comments": [...],
  "_fieldts": { "status": "2026-08-11T11:02:38.000Z", ... }   // last-write ts per field
}
```

`rp list`/`rp ready`/`rp tree`'s children use a short shape without `body`,
`comments` and `_fieldts`; in `rp tree` an epic or saga entry carries one extra
key, `children`, holding its own children in the same shape (`[]` when it has
none), and a leaf entry carries no such key. Exit codes: `0` success, `1` a domain failure (no such
ticket, bad status, …), `2` a usage error. `NO_COLOR` and `CLICOLOR=0` are
respected, and colour is off whenever stdout is not a terminal.

---

## What Rohrpost does not do

Rohrpost stores **tickets** and **notes**. It deliberately does not mirror
tickets into GitHub, Jira or any other tracker (that layer existed once and was
removed as overkill — see ADR 0001), ingest remote comments, run webhooks,
decide *when* something is `waiting`, or track CI results. Those belong to the
surrounding system, which drives `rp --json`; `rp` never reaches the network.
See the [spec](../spec/ROHRPOST-SPEC.md) §9.1 for the full boundary.
