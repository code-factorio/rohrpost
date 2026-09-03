# Issue tracker: Rohrpost

Issues and specs for this repo live in **Rohrpost itself** — this repo is Rohrpost, so it
tracks its own work with `rp`. Tickets are events in `.rohrpost/log.jsonl`, committed with
the code; there is no external tracker to reach for.

## Invoking `rp`

Use the working-tree build, never a published release — dogfooding means the tickets are
written by the code under development:

```bash
cargo run -q -- <command>
```

(`make release` builds `target/release/rp` if you prefer a fixed path.) `rp` walks up from
the current directory to find `.rohrpost/`, so it works from anywhere in the repo. Every
command accepts `--json`; **always pass `--json`** — it is the agent-facing interface, and
the plain output is for human terminals.

Ticket ids are bare (`a1b2c3`) or rendered with this repo's display prefix (`RP-a1b2c3`);
`rp` accepts either. Prefer the bare id in scripts. Exit codes: `0` success, `1` domain
failure (no such ticket, bad status), `2` usage error.

## Conventions

- **Create a ticket**: `rp new "<title>" --body "<markdown>" --json`. Add `--type
  task|bug|spike|epic` (default `task`), `-p 0..4` (0 highest), `--label` (repeatable),
  `--blocked-by <id>` (repeatable), `--parent <epic-id>`.
- **Read a ticket**: `rp show <id> --include body,deps,notes --json`. Notes are the comment
  thread; `rp comments <id> --json` fetches them alone.
- **List tickets**: `rp list --status open --label <label> --json`. Also filters on
  `--type` and `--parent`, and `--match <text>` for a case-insensitive substring of the
  title. Filters compose, so `rp list --match refresh --status open --json` narrows both
  ways. There is no separate search command: matching is a filter, and a title is a search
  key, never an identity.
- **Find work**: `rp ready --json` — open, unblocked, non-epic tickets, highest priority
  first. This is the queue an agent picks from.
- **Comment**: `rp comment <id> "<note>"`. Notes are local to the repo.
- **Apply / remove labels**: `rp set <id> labels+=a,b` / `rp set <id> labels-=a`. Set fields
  use `+=` / `-=` so two concurrent runners compose instead of clobbering.
- **Update any scalar field**: `rp set <id> status=review priority=1` (`title`, `type`,
  `status`, `priority`, `assignee`, `parent`, `body`).
- **Claim**: `rp claim <id>` — moves to `in_progress` and stamps the actor as assignee.
- **Close**: `rp close <id> --reason "<why>"`. **Abandon**: `rp drop <id> --reason "<why>"`.

All mutations are idempotent: re-running `rp close` on a done ticket appends nothing and
still exits `0`.

## Statuses

`open` → `in_progress` → `review` → `done`, with `waiting` for "stalled on a human" and
`dropped` as the other terminal. `ready` is **derived, never set**: a ticket is ready when
it is `open`, not an epic, and every `blocked_by` ticket is `done`. Closing a blocker
unblocks its dependents with no extra write.

Note the asymmetry: a **`dropped` blocker does not unblock** its dependents. If a blocker
is abandoned, remove the edge explicitly with `rp set <dependent> blocked_by-=<id>`.

## Actors

Agents should identify themselves so the log distinguishes them from humans: set
`ROHRPOST_RUNNER=<agent>` and `ROHRPOST_BATCH=<batch>` in the environment (yielding
`runner/<agent>@<batch>`), or pass `--actor` per command. Without either, events are
attributed to `user/<git config user.email>`.

## When a skill says "publish to the issue tracker"

Run `rp new`. Put one-line prose in `--body`; multi-line prose goes through
`--body-file` — a path, or `-` for stdin (strict UTF-8). The file form is the
one every shell passes identically: `$(cat ...)` breaks in PowerShell, and
`\` line continuations are bash-only, so keep the command on one line.
cmd has no here-string — write the body to a file and pass the path.

```bash
cargo run -q -- new "Fix token refresh race" --type bug -p 1 --label needs-triage --json --body-file body.md
```

For stdin, Git Bash pipes a heredoc and PowerShell pipes a here-string:

```bash
cargo run -q -- new "Fix token refresh race" --type bug -p 1 --json --body-file - <<'EOF_BODY'
## Context
...
EOF_BODY
```

```powershell
@'
## Context
...
'@ | cargo run -q -- new "Fix token refresh race" --type bug -p 1 --json --body-file -
```

## When a skill says "fetch the relevant ticket"

Run `cargo run -q -- show <id> --include body,deps,notes --json`.

## No remote mirror

Rohrpost is the source of truth and the only tracker. The former GitHub sync was removed
(ADR 0001); do not look for `link`, `sync` or `conflicts` commands. Anything that must
reach GitHub goes through `gh` directly and is recorded here as a ticket or a comment.

## Pull requests as a request surface

**PRs as a request surface: no.** _(Set to `yes` if this repo should pull external GitHub
PRs into the triage queue; `/triage` reads this flag.)_

When set to `yes`, read PRs with `gh pr view <n> --comments` / `gh pr diff <n>`, then record
each one as a Rohrpost ticket (`rp new "..." --label needs-triage --body "PR:
<url>\n\n..."`) so triage state stays in one place. Keep the GitHub conversation on GitHub
(`gh pr comment`); keep the triage decision in Rohrpost.

## Wayfinding operations

Used by `/wayfinder`. The **map** is an epic; its **child tickets** are the epic's children.
Rohrpost has native parent and blocking relationships, so no body conventions are needed.

- **Map**: an epic labelled `wayfinder:map`, holding the Destination / Notes /
  Decisions-so-far / Fog body:
  `rp new "<destination>" --type epic --label wayfinder:map --body "<map body>" --json`.
  Update it in place with `rp set <map-id> body="<new map body>"` — read the current body
  first (`rp show <map-id> --include body --json`), edit it, write it back whole.
- **Child ticket**: `rp new "<question>" --parent <map-id> --label wayfinder:<type> --body
  "## Question\n\n<...>" --json`, where `<type>` is `research`, `prototype`, `grilling`, or
  `task`. Epics nest one level: children of the map, never grandchildren.
- **Blocking**: native `blocked_by`. Set at creation with `--blocked-by <id>` (repeatable)
  or later with `rp set <child> blocked_by+=<id>` / `blocked_by-=<id>`. A ticket is
  unblocked when every blocker is `done`.
- **Frontier query**: `rp ready --json`, then keep tickets whose `parent` is the map id.
  `ready` already excludes blocked tickets, epics, and anything claimed (a claim moves the
  ticket to `in_progress`). First in the list wins.
- **Claim**: `rp claim <id>` — the session's first write, before any work.
- **Resolve**: `rp comment <id> "<answer>"`, then `rp close <id> --reason "<one-line
  gist>"`, then append the gist plus a link to the map's Decisions-so-far via
  `rp set <map-id> body=...`.
- **Whole-map view**: `rp tree <map-id> --json` renders the epic and its children.

### Handles

Ticket ids are random, so they are hard to type and impossible to remember. A map therefore
gives every ticket a **handle**: a short, sequential, human-typeable label carried as a
**leading bracketed prefix in the title**.

```
[addr]    Human-addressable tickets: how a person points at a Rohrpost ticket   ← the map
[addr-1]  Which human moments actually require typing a ticket id?              ← a child
```

The prefix (`addr`) is agreed with the user when the map is charted; the sequence is allocated
by the charting session. The bare `[addr]` names the map itself, so a human can say
`/wayfinder addr`.

Three properties make this safe, and all three depend on the handle **not** being an
identity:

- **`rp` knows nothing about it.** There is no handle field, no uniqueness contract, and no
  `doctor` check. The 6-char id remains the only identity; the handle is a search key.
- **Clashes are survivable.** Two branches allocating `[addr-7]` produce two tickets whose
  titles share a string — not a corrupt id. Nothing is lost; renumbering one repairs it.
- **Substring search is exact**, because the brackets and the dash delimit. Three shapes:
  `[addr]` is the map epic, `[addr-` is every child, `[addr-2]` is one ticket. Each is safe
  against a longer prefix because the next character differs — `[addr-` cannot match
  `[address-1]`. **Never search the bare `[addr`**: dropping the trailing delimiter is what
  drags in every prefix that merely starts the same way. So **a map prefix must not contain a
  dash** (`addr-x` would put `[addr-x-1]` in range of `[addr-`). `--match` is substring, never
  regex, and deliberately so: as a regex `[addr]` is a character class matching `a`/`d`/`r`
  and would silently match nearly every title.

To load a whole map, resolve the handle to an id, then follow the native parent edges rather
than enumerating children by title — `rp tree <map-id> --json` still finds a child whose
handle was renumbered or never applied.

The decision and its rejected alternatives are in `[addr-5] Should rp resolve a ticket by
title?`. Allocating handles and repairing a clash are the charting skill's business, not
`rp`'s — the tool has no opinion beyond the three properties above.


## Reference

The full user guide is [docs/users/README.md](../users/README.md); the behavioural contract
is [docs/spec/ROHRPOST-SPEC.md](../spec/ROHRPOST-SPEC.md). The installable agent skill in
[.agents/skills/rohrpost](../../.agents/skills/rohrpost) is the condensed version of this
workflow.
