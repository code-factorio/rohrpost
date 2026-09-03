# rohrpost

> A git-native ticket system for agentic coding workflows.

`rohrpost` keeps work items as files in the repository they belong to. Coding
agents create, claim, update and close them without leaving the repo. The repo
is canonical; GitHub, Jira, GitLab and Linear are projections that get synced.

The binary is **`rp`**. The design assumption is that ~95% of all reads and
writes come from agents, not humans — every trade-off resolves in favour of
machine ergonomics. The event log is the single source of truth; everything else
is derived and disposable.

## Status

**Phase 0 — complete.** The store, fold, lock, ids and the full ticket lifecycle
(`init`/`new`/`ready`/`show`/`claim`/`set`/`close`/`drop`/`comment`/`list`/
`tree`/`log`), plus `doctor` and `compact`, are implemented and pass the full
quality gate. A runner can work a ticket end to end.

**Phase 1 (sync) — implemented.** The shadow merge base, the
three-way per-field merge (with a real `git merge-file` text merge for bodies),
the GitHub provider, and the `sync`/`conflicts`/`resolve` commands are in. The
GitHub provider **prefers the `gh` CLI** (pre-authenticated in agent
environments) and falls back to the REST API via `httpx`. Mapped scalar fields
use per-field three-way merge, prose bodies use Git's text merge, and `labels`
use set-wise add/remove semantics. Jira/Linear/GitLab providers are not yet built.

See [`docs/spec/ROHRPOST-SPEC.md`](docs/spec/ROHRPOST-SPEC.md) for the full design,
[`docs/users/`](docs/users/) for usage, and
[`docs/maintainers/`](docs/maintainers/) for internals.

## Requirements

`rp` ships as a **single native binary** (C++23) for Linux (x86_64, aarch64),
macOS (universal) and Windows (x86_64, arm64) — no runtime, no toolchain.
Download it from the [releases](https://github.com/code-factorio/rohrpost/releases)
and put it on `PATH`, or build it yourself:

```bash
cmake --preset linux && cmake --build --preset linux   # macos | windows presets exist too
./build/linux/rp --version
```

The Python package in `src/rohrpost` is the **frozen reference
implementation**: the same tool, kept unchanged as the oracle the native binary
is tested against (see below). It still runs with Python **3.14+** and
[uv](https://docs.astral.sh/uv/), so `uvx rohrpost` and `uv run rp` keep working
and are byte-for-byte compatible with the native `rp` — both can share one
repository.

## Quick start

```bash
rp init                 # scaffold .rohrpost/ in this repo (proposes a prefix)
rp new "Fix token refresh race" --type bug -p 1 --label auth
rp ready                # the actionable work queue
rp show <id>            # bare id or PREFIX-id both work
```

(`uv sync && uv run rp ...` drives the Python reference instead; the output is
identical.)

### A ticket end to end

```bash
rp new "Blocker" --json                      # create, print machine-readable JSON
rp new "Real work" --blocked-by <id> -p 0    # depends on the blocker
rp ready                                     # empty: "Real work" is blocked
rp close <blocker-id> --reason "shipped"     # unblock it
rp ready                                     # now shows "Real work"
rp claim <id>                                # -> in_progress, stamps the actor
rp comment <id> "retried with backoff"
rp close <id> --reason "done"
```

Every command takes `--json`. The log is the only thing that accumulates state;
`rp doctor` checks integrity, `rp log` shows the raw event history.

## How it works

Everything starts as an append-only event in `.rohrpost/log.jsonl`:

```jsonc
{"id":"01K2X8P4RQ7YFZ3M9NVB6TDHWC","ts":"2026-08-11T09:20:14.221Z",
 "ticket":"a1b2c3","op":"set","actor":"runner/claude-code@b-3","set":{"status":"in_progress"}}
```

Tickets are a **fold** over that log — deduplicated by event id, sorted by
`(ts, id)`, replayed field-by-field with **per-field last-write-wins**. Two
runners updating `status` and `priority` on the same ticket concurrently both
win. The fold is deterministic and disposable (`tickets.jsonl` is gitignored and
regenerated on demand).

One write path: mutations go through `rp`, never by hand-editing the log.

## Project layout

```
cpp/                     # the native rp: one header + source per module below
  include/rohrpost/      #   (pyfmt/json/argparse reproduce Python's byte-level rules)
  src/, tests/           #   implementation, doctest unit tests
third_party/             # vendored header-only deps: nlohmann/json, toml++, doctest
tests/conformance/       # differential suite: native rp vs the Python reference
scripts/ci/plan.py       # the dynamic CI matrix (build legs, release targets, shards)
src/rohrpost/            # the frozen Python reference (module map below)
├── ids.py        # ticket ids (base32) + ULIDs — the load-bearing id scheme
├── events.py     # append-only event envelope (msgspec) + JSONL codec
├── store.py      # the log: advisory flock + O_APPEND, read archive+log
├── fold.py       # events -> tickets: dedupe, sort, per-field LWW, derived state
├── config.py     # .rohrpost/config.toml (project prefix, remotes)
├── paths.py      # the .rohrpost/ layout + repo discovery
├── api.py        # the one write path: create/set/claim/close/... (idempotent)
├── merge.py      # sync: three-way per-field merge + git merge-file for bodies
├── shadow.py     # sync: the shadow merge base (shadow/<remote>/<ref>.json)
├── sync.py       # sync: the round — fetch/merge/apply/push/rewrite-shadow
├── providers/    # sync providers; github.py (gh-preferred, httpx fallback)
├── doctor.py     # rp doctor — integrity + config checks
├── compact.py    # rp compact — archive terminal tickets, truncate the log
└── cli.py        # the `rp` entry point (--json, NO_COLOR)
```

## Tooling

A deliberate, layered quality gate runs on every push and in CI. Fast checks run
on commit; the full suite runs on push. The native binary adds three layers: the
C++ unit tests (`make native-test`), the **conformance suite** that runs every
command through both implementations and diffs output, exit codes and the event
log (`make conformance`), and release builds with `-Werror`. CI computes its job
matrix dynamically (`scripts/ci/plan.py`), builds every target, runs one
conformance shard per test module per OS, and a `v*` tag publishes the binaries
with a `SHA256SUMS` manifest. See
[`docs/maintainers/native.md`](docs/maintainers/native.md).

| Layer         | Tool(s)                                   |
| ------------- | ----------------------------------------- |
| Format & lint | `ruff` (format + check)                   |
| Types         | `ty`, `mypy`, `pyright`                   |
| Security      | `bandit`                                  |
| Structure     | `pyscn` (complexity / deadcode / clones)  |
| Tests         | `pytest`, `coverage`, `hypothesis`        |
| Complexity    | `radon`, `xenon` (cyclomatic / MI)        |
| Mutation      | `mutmut`                                  |

```bash
make help        # list available targets
make check       # the full deterministic gate
make mutation    # mutation testing (slow; not part of `make check`)
```

### Pre-commit hooks

```bash
uv run pre-commit install   # commit- and pre-push-stage hooks
```

## Contributing

Open an issue first for sizeable changes so we can align on direction before code
is written. Keep the event envelope strict and write events generously — it is
the one load-bearing decision; field names and CLI shape are cheap to change.

## License

[MIT](./LICENSE) © code-factorio
