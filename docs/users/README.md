# Rohrpost — User Guide

Rohrpost is a ticket system that lives in your git repository. The binary is
**`rp`**. Tickets are files under `.rohrpost/`; the append-only event log
(`log.jsonl`) is the source of truth, and a ticket is a *fold* over that log.

This guide is for humans and coding agents who drive `rp`. It is **agent-first**:
`--json` is available on every command and is the preferred interface for
automation; the default human-readable output is for terminals.

---

## Install

`rp` is a single native binary with no runtime dependencies. Download the build
for your platform from the
[releases page](https://github.com/code-factorio/rohrpost/releases) — Linux
(x86_64, aarch64), macOS (universal), Windows (x86_64, arm64) — verify it
against the published `SHA256SUMS`, rename it to `rp` (`rp.exe` on Windows)
and put it on `PATH`. To build from source you need a C++23 compiler, CMake
and Ninja:

```bash
cmake --preset linux && cmake --build --preset linux    # or macos / windows
```

The Python reference implementation is still available and produces the same
bytes, so either form can drive the same repository:

```bash
uvx rohrpost <command>      # the Python reference, no prerequisite toolchain
uv sync && uv run rp <command>
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

- `.rohrpost/config.toml` — the display prefix (and, later, remote mappings)
- `.rohrpost/log.jsonl` — the append-only event log (committed; **truth**)
- `.rohrpost/archive/`, `.rohrpost/shadow/`, `.rohrpost/templates/`
- `.gitattributes` merge and line-ending rules and a `.gitignore` for the regenerable
  `tickets.jsonl` snapshot

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
`[defaults]`, `[fields]`, or `[ticket]` table.

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
agent finds work. It is fast and small.

### Work a ticket

```bash
rp claim <id>             # -> in_progress, stamps the actor as assignee
rp set <id> status=review priority=1
rp set <id> labels+=auth,bug labels-=ui
rp comment <id> "retried with backoff, still 429s"
rp unlink <id> github
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
rp list --match "token refresh"      # case-insensitive substring of the title
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

Every event records who did it under one of three namespaces:

- `user/<git config user.email>` — a human (the default)
- `runner/<agent>@<batch>` — a coding agent. Set via the `ROHRPOST_RUNNER` and
  `ROHRPOST_BATCH` env vars, or `--actor`.
- `remote/<name>` — a change that arrived through sync

`--actor` overrides everything; `ROHRPOST_ACTOR` overrides the env-derived form.

---

## Integrity and maintenance

```bash
rp doctor        # log parses; no dup ids; refs resolve; no cycles;
                 # gitattributes/shadow rules; credentials; fresh snapshot
rp compact       # archive tickets terminal for >90d; main branch only
```

`doctor` is the one place the pneumatic metaphor is allowed out: it reports
tickets "stuck in the tube". `compact` is the only operation that rewrites the
log, so it refuses unless the tree is clean and `HEAD` is on the default branch
(`--force` overrides).

---

## JSON output

Every command takes `--json` and returns structured output. Tickets render as:

```jsonc
{
  "id": "FAC-a1b2c3", "title": "...", "type": "task", "status": "open",
  "priority": 2, "parent": null, "blocked_by": [], "labels": ["auth"],
  "assignee": null, "body": null, "remotes": {}, "last_close_reason": null,
  "created": "2026-08-11T09:14:02Z", "updated": "2026-08-11T11:02:38Z",
  "comments": [...],
  "_fieldts": { "status": "2026-08-11T11:02:38Z", ... }   // last-write ts per field
}
```

Exit codes: `0` success, `1` a domain failure (no such ticket, bad status, …),
`2` a usage error. `NO_COLOR` is respected.

---

## Syncing with a remote tracker

Rohrpost mirrors tickets into a linked SaaS tracker (spec §8). Configure a
remote in `config.toml`, link a ticket to a remote item, then run `rp sync`:

```toml
[remotes.github]
repo  = "owner/name"
policy = "flag"            # flag (default) | local | remote

[remotes.github.fields]
title  = "title"
body   = "body"
status = { open = "open", in_progress = "open", done = "closed", dropped = "closed" }
```

```bash
rp link <id> github 42       # bind a ticket to GitHub issue #42
rp unlink <id> github         # remove the binding
rp sync --dry-run            # print the plan, touch nothing
rp sync                      # three-way merge: pull remote edits, push local edits
rp conflicts                 # tickets flagged when both sides changed a field
rp resolve <id> --take local # clear a conflict (edit the fields first, then resolve)
```

Every sync is a **three-way merge** against a shadow snapshot (the remote's last
synced state, stored under `.rohrpost/shadow/`). When both sides changed the same
field, the `flag` policy moves the ticket to `review`, tags it
`conflict:<remote>`, and records both values in a comment. Prose bodies get a
genuine text merge (via `git merge-file`) instead of overwriting.

The GitHub provider **prefers the `gh` CLI** (so it uses your `gh auth login`)
and falls back to the GitHub REST API via `httpx` (token from `GITHUB_TOKEN` or
`ROHRPOST_GITHUB_TOKEN`) when `gh` is absent. Mapped `labels` merge as a set:
independent additions/removals compose instead of becoming scalar conflicts.

---

## What Rohrpost does not do

Rohrpost stores **tickets** and **notes** (local notes are never synced). It
deliberately does not ingest remote comments, run webhooks, decide *when*
something is `waiting`, or track CI results — those belong to the surrounding
system. The dependency runs one way: the outer system drives `rp`; `rp` only
reaches out during an explicit `rp sync`. See the
[spec](../spec/ROHRPOST-SPEC.md) §9.1 for the full boundary.
