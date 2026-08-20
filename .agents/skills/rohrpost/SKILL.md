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

Tickets are events in an append-only `.rohrpost/log.jsonl`, committed with the
code; every ticket is a **fold** over that log. The log is truth — mutate it
through `rp` and leave it unedited by hand. `rp` walks up from the working
directory to find `.rohrpost/`, so it runs from anywhere in the repo.

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
rp ready --json --limit 5                       # 1. the queue: open, unblocked, non-epic, priority first
rp show <id> --json                             # 2. read it before starting
rp claim <id> --json                            # 3. take it -> in_progress, stamps you as assignee
rp comment <id> "429s persist after backoff"    # 4. record findings as you go
rp close <id> --reason "exponential backoff"    # 5. finish, once the repo's tests pass
```

`rp ready --json` is the call that matters — it is how work is found. Its output,
like `rp list`, carries no bodies, so `rp show` is the only way to read ticket
prose. Comments are local notes and never sync anywhere.

Abandon instead of closing when the work should not happen:
`rp drop <id> --reason "superseded by <other-id>"`. Reasons ride on the command
rather than a field, so they survive reopen/re-close cycles.

Mutations are idempotent: re-running `rp close` on a done ticket appends nothing
and still exits `0`. Retry freely.

## Creating

```bash
rp new "Fix token refresh race" --type bug -p 1 --label auth --json
rp new "Auth epic" --type epic --json
rp new "Child task" --parent <epic-id> --blocked-by <id> --json --body "$(cat <<'EOF'
## Context
...
EOF
)"
```

Types are `task|bug|spike|epic`; `-p 0..4` runs 0 highest to 4 lowest; `--label`
and `--blocked-by` repeat. A heredoc keeps multi-line markdown bodies intact.
`--template <name>` loads defaults from `.rohrpost/templates/<name>.toml`, and
explicit flags override them. Every ticket starts `open`.

An **epic** is `--type epic`; children point at it with `--parent`, one level
deep. Epic status is derived from its children.

## Updating fields

```bash
rp set <id> status=review priority=1
rp set <id> labels+=auth,bug labels-=spike
rp set <id> blocked_by+=<other-id>
```

Scalars (`title`, `type`, `status`, `priority`, `assignee`, `parent`, `body`)
take `=`. The set fields (`labels`, `blocked_by`) take `+=` / `-=` so two runners
editing at once compose instead of clobbering each other. `body=` replaces the
whole body: read it with `rp show <id> --json`, edit, write it back whole.

## Statuses and blocking

`open → in_progress → review → done`, plus `waiting` (stalled on a human) and the
terminal `dropped`. `claim`, `close` and `drop` are the dedicated transitions;
use `rp set <id> status=review|waiting` for the rest.

`ready` is **derived, never set**: a ticket is ready when it is `open`, not an
epic, and every `blocked_by` ticket is `done`. Closing a blocker unblocks its
dependents with no extra write. A **dropped blocker keeps its dependents
blocked** — when a blocker is abandoned, cut the edge explicitly with
`rp set <dependent> blocked_by-=<id>`.

## Reading

```bash
rp show <id> --json                         # everything: body, comments, _fieldts
rp list --status open --label auth --json   # filters compose
rp list --match "token refresh" --json      # case-insensitive substring of the title
rp tree <epic-id> --json                    # an epic and its direct children
rp comments <id> --json                     # the note thread alone
rp log <id> --json                          # the raw events behind the fold
```

`show --json` already returns body, comments and per-field timestamps;
`--include body,deps,notes,fieldts` shapes the human output only. `list` filters
on `--status`, `--label`, `--type`, `--parent` and `--match`, and derived
statuses are queryable (`--status ready`). Matching is a filter, never an
identity — a title is a search key.

Ids come back rendered with the repo's display prefix (`RP-a1b2c3`); `rp` accepts
that form or the bare `a1b2c3` on input. The prefix is display-only, so renaming
it re-renders every id with no migration.

## When state looks wrong

```bash
rp doctor --json     # log integrity, dangling refs, cycles, git rules, snapshot freshness
rp log <id> --json   # the history that produced the current fold
rp stats --json      # body and line size distributions, fold timing
```

`rp doctor` exits non-zero when something needs attention. A stale
`tickets.jsonl` is harmless — it is a regenerable cache, and only `log.jsonl` is
truth.

## Repository-level commands

`rp init --prefix ABC` scaffolds `.rohrpost/` in a repo that lacks one, and is
idempotent. `rp compact` archives long-terminal tickets and is the one operation
that rewrites the log, so it refuses unless the tree is clean and `HEAD` is on
the default branch. Run compaction when a maintainer asks for it.

## Mirroring to a remote tracker

Available when the repo configures one (a `[remotes.*]` table in
`.rohrpost/config.toml`). `rp` reaches the network during an explicit `rp sync`
and at no other time.

```bash
rp link <id> github 42          # bind a ticket to issue #42
rp unlink <id> github
rp sync --dry-run               # print the plan, touch nothing
rp sync
rp conflicts                    # tickets where both sides changed a field
rp resolve <id> --take local    # after fixing the field
```

Sync is a three-way merge against a shadow snapshot: prose bodies get a real text
merge, and a contested field moves the ticket to `review` with a
`conflict:<remote>` label. It is idempotent, and prefers the pre-authenticated
`gh` CLI over the REST API. **Treat linking, syncing and conflict resolution as
maintainer decisions** — do them on request, not as part of ordinary ticket work.

## Boundaries

`rp` stores tickets and local notes. Ingesting remote comments, running webhooks,
tracking CI and deciding *when* something is `waiting` belong to the surrounding
system. To ask a human something, record the question with `rp comment` and set
`status=waiting`; that system decides when to clear it.
