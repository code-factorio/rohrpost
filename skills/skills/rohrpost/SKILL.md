---
name: rohrpost
description: |
  Drives ticketed work in any repo containing a `.rohrpost/` directory (the `rp`
  CLI): find actionable work, create and query tickets, claim them, record
  progress, and close them without leaving the repo. Use it to obtain a ticket's
  id, status, dependencies, or definition-of-done before starting a task, and to
  update state as the task progresses.
  Trigger on: "work a ticket", "find work", "pick up a ticket", "create a
  ticket", "rp", "rohrpost", or any time ticket state needs reading or advancing.
---

# Working tickets with `rp`

`rp` is the binary for **Rohrpost**, the git-native ticket system used in this
repo. Tickets live in `.rohrpost/`; the append-only `log.jsonl` is the source of
truth. One write path: **always go through `rp`, never hand-edit the log.**

This skill is the short form. Full reference: `rp --help` and the repo's
`docs/users/` and `docs/spec/ROHRPOST-SPEC.md`.

## Conventions that matter

- **Every command takes `--json`.** Prefer it — parse the output, don't scrape
  text. Exit codes: `0` ok, `1` domain error (e.g. no such ticket), `2` usage.
- **Ticket ids are bare 6-char base32** (`a1b2c3`). `rp` also accepts the
  rendered form (`FAC-a1b2c3`); drop the prefix in scripts.
- **`ready` is derived, not stored.** A ticket becomes ready automatically when
  its `blocked_by` tickets are `done` — you never "mark ready".
- **Mutations are idempotent.** Re-running `rp close <id>` on a done ticket is a
  no-op, not an error. Retry freely.
- **Identify yourself.** Set `ROHRPOST_RUNNER=<your-name>` (and optionally
  `ROHRPOST_BATCH=<batch>`) so events are attributed to `runner/<name>@<batch>`
  rather than a human. Or pass `--actor runner/<name>`.

## The work loop

```bash
# 1. Find actionable work (the single most important call):
rp ready --json --limit 5          # open, unblocked, non-epic, by priority

# 2. Claim one (-> in_progress, stamps you as assignee):
rp claim <id> --json

# 3. As you work, record why — local notes, never synced:
rp comment <id> "retried with backoff, still 429s"

# 4. Update fields with `set` (scalar = ; set fields use += / -=):
rp set <id> status=in_progress priority=1
rp set <id> labels+=auth,bug  labels-=spike
rp set <id> blocked_by+=<other-id>

# 5. Before closing, verify the work is actually done (run tests, etc.).
#    Check every command's exit code: 0 ok, 1 domain error, 2 usage.
#    If ticket state looks inconsistent, diagnose before closing:
rp doctor      # integrity check; non-zero exit means something needs attention

# 6. When the work is done (or not):
rp close <id> --reason "implemented with exponential backoff"
rp drop  <id> --reason "wontfix — superseded by <other-id>"
```

Record close reasons on the command (`--reason`), not in a field — they survive
across reopen/re-close cycles.

## Creating tickets

```bash
rp new "Fix token refresh race" --type bug -p 1 --label auth --json
rp new "Auth epic" --type epic --json                              # parent
rp new "Child" --parent <epic-id> --blocked-by <id> --body "..."   # child
```

Flags: `--type task|bug|spike|epic`, `-p/--priority 0..4` (0 highest),
`--label` (repeatable), `--blocked-by` (repeatable), `--parent`, `--assignee`,
`--body`. Created tickets always start `open`.

## Reading

```bash
rp show <id> --include body,deps,notes,fieldts     # full detail
rp show <id> --json                                # machine-readable
rp comments <id>                                   # all local notes
rp tree <epic-id>                                  # epic + direct children
rp list --status open --label auth                 # query
rp list --match "token refresh"                    # title substring, case-insensitive
rp log <id>                                        # raw event history
```

## Statuses

`open → in_progress → review → done`, plus `waiting` (stalled on a human) and
terminal `dropped`. Use `set status=...` for `review`/`waiting`; `claim`/`close`/
`drop` are the dedicated commands for the common transitions.

## When something looks wrong

```bash
rp doctor      # log integrity, dangling refs, cycles, stale snapshot, git rules
rp log <id>    # the raw history — the log is truth; everything else is derived
```

`rp doctor` exits non-zero if anything needs attention. A stale `tickets.jsonl`
is harmless (regenerated automatically) — only the `log.jsonl` matters.

## Syncing with a remote (GitHub)

If the repo configures a remote (`.rohrpost/config.toml` has a `[remotes.*]`
table), `rp sync` does a three-way merge against the linked tracker — it prefers
the `gh` CLI (pre-authenticated) and falls back to REST. It only runs on an
explicit `rp sync`; `rp` never reaches out on its own otherwise.

```bash
rp link <id> github 42        # bind a ticket to GitHub issue #42
rp sync --dry-run             # plan only; CI-safe
rp sync                       # pull remote edits + push local edits
rp conflicts                  # tickets where both sides changed a field
rp resolve <id> --take local  # clear a conflict (edit the field first)
```

When both sides edit the same field, the ticket moves to `review` and is tagged
`conflict:<remote>`; bodies get a real text merge. Sync is idempotent.

## What `rp` does NOT do

It does not ingest remote comments, run webhooks, or decide *when* something is
`waiting`. If you need to ask a human something, record the question as a
`comment` and set `status=waiting`; the surrounding system (not `rp`) decides
when to clear it.
