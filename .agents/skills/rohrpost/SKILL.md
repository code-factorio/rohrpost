---
name: rohrpost
description: >-
  `rp` — Rohrpost, the git-native ticket system in any repo holding a
  `.rohrpost/` directory. Use when finding or claiming work, creating a ticket,
  reading a ticket's status, dependencies or body before starting, recording
  progress, closing or dropping it, or resolving a ticket id (`a1b2c3`,
  `RP-a1b2c3`).
---

# Working tickets with `rp`

## Invocation

The wrapper lives beside this skill. Resolve the skill directory first, then
invoke its `scripts/rohrpost` path; use the resolved path from any caller
working directory:

```bash
<rohrpost-skill>/scripts/rohrpost ready --json --limit 5
```

On Windows each shell runs its own wrapper beside this skill — same contract,
same flags:

```text
PowerShell:  <rohrpost-skill>\scripts\rohrpost.ps1 ready --json --limit 5
cmd:         <rohrpost-skill>\scripts\rohrpost.cmd ready --json --limit 5
Git Bash:    <rohrpost-skill>/scripts/rohrpost ready --json --limit 5
```

The `.ps1` and `.cmd` wrappers default the install home to
`%LOCALAPPDATA%\rohrpost` and honour `ROHRPOST_HOME`; the Git Bash wrapper
keeps the POSIX default. All of them run the single static `rp` binary at
`<home>/bin/rp` (`rp.exe` on Windows) — there is no runtime to activate.

The wrapper preserves the caller's working directory and validates the local
installation before invoking Rohrpost. If the wrapper is missing or reports
that the installation is incomplete, load `playbooks/install-local.md` from
this skill; on Windows it routes to `playbooks/windows.md`. That playbook asks
the user for permission before installing anything. Do not bypass the wrapper
with a system `rp` or a different checkout.

Tickets are events in an append-only `.rohrpost/log.jsonl`, committed with the
code; every ticket is a **fold** over that log. The log is truth — mutate it
through the wrapper and leave it unedited by hand. Rohrpost walks up from the
working directory to find `.rohrpost/`, so it runs from anywhere in the repo.

Pass `--json` on every command (after the subcommand): it is the agent-facing
interface, and the plain text is for human terminals. Exit codes: `0` success,
`1` domain failure (no such ticket, bad status), `2` usage error. Full flags live
in `rp <command> --help`; this file carries what `--help` does not say.

Identify yourself so the log separates agents from humans — export
`ROHRPOST_RUNNER=<agent>` and `ROHRPOST_BATCH=<batch>` (yielding
`runner/<agent>@<batch>`), or pass `--actor runner/<agent>` per command. The
default is `user/<git config user.email>`, i.e. a human.

## The work loop

```bash
<rohrpost-skill>/scripts/rohrpost ready --json --limit 5                       # 1. the queue: open, unblocked, non-epic, priority first
<rohrpost-skill>/scripts/rohrpost show <id> --json                             # 2. read it before starting
<rohrpost-skill>/scripts/rohrpost claim <id> --json                            # 3. take it -> in_progress, stamps you as assignee
<rohrpost-skill>/scripts/rohrpost comment <id> "429s persist after backoff" --json    # 4. record findings as you go
<rohrpost-skill>/scripts/rohrpost close <id> --reason "exponential backoff" --json    # 5. finish, once the repo's tests pass
```

`ready --json` is the call that matters — it is how work is found. Its output,
like `list`, carries no bodies, so `show` is the only way to read ticket prose.
Comments are local notes.

Abandon instead of closing when the work should not happen:
`<rohrpost-skill>/scripts/rohrpost drop <id> --reason "superseded by <other-id>" --json`.
Reasons ride on the command rather than a field, so they survive reopen/re-close
cycles.

Mutations are idempotent: re-running `close` on a done ticket appends nothing
and still exits `0`. Retry freely.

## Creating

```bash
<rohrpost-skill>/scripts/rohrpost new "Fix token refresh race" --type bug -p 1 --label auth --json
<rohrpost-skill>/scripts/rohrpost new "Auth epic" --type epic --json
<rohrpost-skill>/scripts/rohrpost new "Child task" --parent <epic-id> --blocked-by <id> --json --body-file - <<'EOF'
## Context
...
EOF
```

Types are `task|bug|spike|epic|saga`; `-p 0..4` runs 0 highest to 4 lowest; `--label`
and `--blocked-by` repeat. Multi-line bodies go through `--body-file` (a path,
or `-` for stdin). In bash, pipe a heredoc into `-` as above; in PowerShell,
pipe a here-string:

```powershell
@'
## Context
...
'@ | <rohrpost-skill>\scripts\rohrpost.ps1 new "Child task" --json --body-file -
```

`--template <name>` loads defaults from `.rohrpost/templates/<name>.toml`, and
explicit flags override them. Every ticket starts `open`.

Open an epic when a deliverable needs more than one leaf. Open a saga only when a
second epic appears for the same outcome: create the saga, then set both epics'
parent to it. Whoever creates the second epic opens the saga, agent or human. Never
open a saga for one epic, or for a concern that cuts across epics owned elsewhere;
that is a label. A leaf goes under the epic it belongs to. A leaf that belongs to
the outcome and to no epic, such as a spike that clears fog or the ADR that records
the decision, goes under the saga. A saga whose children are all leaves is an epic
with the wrong type.

```bash
<rohrpost-skill>/scripts/rohrpost new "Passkey sign-in" --type epic --label auth --json      # second epic for the outcome: the saga appears now
<rohrpost-skill>/scripts/rohrpost new "Passwordless sign-in" --type saga --json              # → RP-7k2m9q
<rohrpost-skill>/scripts/rohrpost set RP-3f8xa1 parent=RP-7k2m9q --json                      # the epic that already existed
<rohrpost-skill>/scripts/rohrpost set RP-b6nd4z parent=RP-7k2m9q --json
<rohrpost-skill>/scripts/rohrpost new "Spike: one session record for both flows" --type spike --parent RP-7k2m9q --json   # belongs to the outcome, to no epic
```

The shape is `saga → epic → leaf`, and stop: a saga sits under nothing, an epic
under a saga or nothing, a leaf under any of them or nothing. A `new --parent` or a
`set type=`/`parent=` that would break it is refused whole, exit 1, with the conflict
named (`cannot set type=task on RP-x: it has 3 children (...)`); move or drop the
children first.

## Updating fields

```bash
<rohrpost-skill>/scripts/rohrpost set <id> status=review priority=1 --json
<rohrpost-skill>/scripts/rohrpost set <id> labels+=auth,bug labels-=spike --json
<rohrpost-skill>/scripts/rohrpost set <id> blocked_by+=<other-id> --json
```

Scalars (`title`, `type`, `status`, `priority`, `assignee`, `parent`, `body`)
take `=`; an empty value clears a nullable one (`parent=` detaches the ticket
from its epic or saga, `assignee=` and `body=` likewise). The set fields (`labels`, `blocked_by`) take `+=` / `-=` so two runners
editing at once compose instead of clobbering each other. `body=` replaces the
whole body: read it with `<rohrpost-skill>/scripts/rohrpost show <id> --json`,
edit, write it back whole — `--body-file` works here too for multi-line text.

## Statuses and blocking

`open → in_progress → review → done`, plus `waiting` (stalled on a human) and the
terminal `dropped`. `claim`, `close` and `drop` are the dedicated transitions;
use `<rohrpost-skill>/scripts/rohrpost set <id> status=review|waiting --json` for
the rest.

`ready` is **derived, never set**: a ticket is ready when it is `open`, a leaf
(not an epic, not a saga), and every `blocked_by` ticket is `done`. Closing a
blocker unblocks its dependents with no extra write.

The status of an epic or a saga with children is **derived, never written**, on
every read path: `dropped` when every child is dropped, `done` when every child is
settled (`done` or `dropped`) and one is `done`, `open` otherwise; an epic counts
toward its saga by its own derived status. `close`, `drop`, `claim` and
`set status=` on such a parent are refused unless they equal the derived status
(then they are the usual no-op): `cannot close RP-x: it has 2 open children (RP-a,
RP-b)`. Close the leaves; the parents follow. A **dropped blocker keeps its dependents
blocked** — when a blocker is abandoned, cut the edge explicitly with
`<rohrpost-skill>/scripts/rohrpost set <dependent> blocked_by-=<id> --json`.

## Reading

```bash
<rohrpost-skill>/scripts/rohrpost show <id> --json                         # everything: body, comments, _fieldts
<rohrpost-skill>/scripts/rohrpost list --status open --label auth --json   # filters compose
<rohrpost-skill>/scripts/rohrpost list --match "token refresh" --json      # case-insensitive substring of the title
<rohrpost-skill>/scripts/rohrpost tree <epic-or-saga-id> --json            # the whole subtree; an epic entry nests a `children` array, a leaf entry has none
<rohrpost-skill>/scripts/rohrpost comments <id> --json                     # the note thread alone
<rohrpost-skill>/scripts/rohrpost log <id> --json                          # the raw events behind the fold
```

`show --json` already returns body, comments and per-field timestamps;
`--include body,deps,notes,fieldts` shapes the human output only. `list` filters
on `--status`, `--label`, `--type`, `--parent` and `--match`, and derived
statuses are queryable (`--status ready`). Matching is a filter, never an
identity — a title is a search key, never an identity.

`tree --json` on a saga, short shape trimmed to what matters (`...` stands for
`priority`, `blocked_by`, `labels`, `assignee`, `last_close_reason`, `created`,
`updated`); an empty epic carries `"children": []`:

```json
{
  "root": { "id": "RP-7k2m9q", "type": "saga", "status": "open", "title": "Passwordless sign-in", "parent": null, ... },
  "children": [
    { "id": "RP-3f8xa1", "type": "epic", "status": "open", "title": "Magic-link sign-in", "parent": "RP-7k2m9q", ...,
      "children": [
        { "id": "RP-c1q7we", "type": "task", "status": "done", "title": "Send the sign-in mail", "parent": "RP-3f8xa1", ... },
        { "id": "RP-x9r2ht", "type": "task", "status": "in_progress", "title": "Verify the link token", "parent": "RP-3f8xa1", ... }
      ] },
    { "id": "RP-b6nd4z", "type": "epic", "status": "open", "title": "Passkey sign-in", "parent": "RP-7k2m9q", ...,
      "children": [
        { "id": "RP-m5t8vk", "type": "task", "status": "open", "title": "Log every passkey enrolment", "parent": "RP-b6nd4z", ... }
      ] },
    { "id": "RP-2wjy6e", "type": "spike", "status": "open", "title": "Spike: one session record for both flows", "parent": "RP-7k2m9q", ... }
  ]
}
```

`rp` keeps no ancestry: a leaf's epic is its `parent`, the saga is the epic's
`parent`, so two `show` calls reach it.

Ids come back rendered with the repo's display prefix (`RP-a1b2c3`); Rohrpost
accepts that form or the bare `a1b2c3` on input. The prefix is display-only, so
renaming it re-renders every id with no migration.

## When state looks wrong

```bash
<rohrpost-skill>/scripts/rohrpost doctor --json     # log integrity, dangling refs, cycles, git rules
<rohrpost-skill>/scripts/rohrpost log <id> --json   # the history that produced the current fold
<rohrpost-skill>/scripts/rohrpost stats --json      # body and line size distributions, fold timing
```

`doctor` exits non-zero when something needs attention. Only `log.jsonl` (plus
`archive/`) is truth; every ticket is re-folded from it on each call.

## Repository-level commands

`<rohrpost-skill>/scripts/rohrpost init --prefix ABC --json` scaffolds
`.rohrpost/` in a repo that lacks one, and is idempotent. `<rohrpost-skill>/scripts/rohrpost compact --json`
archives long-terminal tickets and is the one operation that rewrites the log,
so it refuses unless the tree is clean and `HEAD` is on the default branch. Run
compaction when a maintainer asks for it.

## Boundaries

Rohrpost stores tickets and local notes, and it is the only tracker: there is
no `link`, `sync` or `conflicts` command, and `rp` never touches the network.
Mirroring into GitHub or Jira, ingesting remote comments, running webhooks,
tracking CI and deciding *when* something is `waiting` belong to the
surrounding system. To ask a human something, record the question with
`<rohrpost-skill>/scripts/rohrpost comment <id> --json` and set `status=waiting`;
that system decides when to clear it.
