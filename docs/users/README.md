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

Flags: `--type task|bug|spike|epic`, `-p/--priority 0..4` (0 highest), `--label`
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
rp ready                  # the actionable queue: open, unblocked, non-epic
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
clobbering each other.

All mutations are **idempotent**: re-running `rp close <id>` on an already-done
ticket is a no-op (it appends nothing), not an error.

### Read

```bash
rp show <id>                          # summary + body
rp show <id> --include body,deps,notes,fieldts
rp comments <id>                      # all local notes
rp tree <epic-id>                     # an epic and its direct children
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

Types: `task`, `bug`, `spike`, `epic`. An **epic** is a ticket with `type: epic`;
children point at it via `parent` (one level of nesting). Epic status is derived
— an epic is `done` when its children are.

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
rp doctor        # log parses; no dup ids; refs resolve; no cycles; gitattributes rules
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
`comments` and `_fieldts`. Exit codes: `0` success, `1` a domain failure (no such
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
