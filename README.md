# rohrpost

> A git-native ticket system for agentic coding workflows.

`rohrpost` keeps work items as files in the repository they belong to. Coding
agents create, claim, update and close them without leaving the repo. The repo
is canonical; there is no server, no daemon and no external tracker.

The binary is **`rp`**: a single static executable written in Rust with **no
dependencies beyond the standard library**, for Linux, macOS and Windows. The
design assumption is that ~95% of all reads and writes come from agents, not
humans — every trade-off resolves in favour of machine ergonomics. The event
log is the single source of truth; everything else is derived and disposable.

## Status

The store, fold, lock, ids and the full ticket lifecycle
(`init`/`new`/`ready`/`show`/`claim`/`set`/`close`/`drop`/`comment`/`list`/
`tree`/`log`), plus `doctor`, `compact` and `stats`, are implemented and
covered by unit and end-to-end tests on all three platforms. A runner can work
a ticket end to end.

The earlier sync layer (mirroring tickets into GitHub/Jira/GitLab/Linear) was
removed as overkill; see [ADR 0001](docs/adr/0001-rust-std-only-rewrite.md).
Logs written while it existed keep folding — the legacy events are kept and
ignored.

See [`docs/spec/ROHRPOST-SPEC.md`](docs/spec/ROHRPOST-SPEC.md) for the full design,
[`docs/users/`](docs/users/) for usage, and
[`docs/maintainers/`](docs/maintainers/) for internals.

## Install

Prebuilt binaries for Linux (x86_64, aarch64), macOS (Apple silicon, Intel)
and Windows (x86_64) are attached to every
[GitHub release](https://github.com/code-factorio/rohrpost/releases). Put `rp`
(or `rp.exe`) on your `PATH`; it has no runtime requirements.

From source (Rust 1.89 or newer):

```bash
cargo install --git https://github.com/code-factorio/rohrpost --locked
```

## Quick start

```bash
rp init                 # scaffold .rohrpost/ in this repo (proposes a prefix)
rp new "Fix token refresh race" --type bug -p 1 --label auth
rp ready                # the actionable work queue
rp show <id>            # bare id or PREFIX-id both work
```

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
win. The fold is deterministic and cheap: a 30 000-event log folds in tens of
milliseconds, so there is no cache to keep fresh.

One write path: mutations go through `rp`, never by hand-editing the log.

## Project layout

```
src/
├── ids.rs        # ticket ids (base32) + ULIDs — the load-bearing id scheme
├── events.rs     # the append-only event envelope + JSONL codec
├── store.rs      # the log: exclusive lock + append mode, read archive+log
├── fold.rs       # events -> tickets: dedupe, sort, per-field LWW, derived state
├── config.rs     # .rohrpost/config.toml (project prefix, default branch)
├── paths.rs      # the .rohrpost/ layout + repo discovery
├── api.rs        # the one write path: create/set/claim/close/... (idempotent)
├── doctor.rs     # rp doctor — integrity + config checks
├── compact.rs    # rp compact — archive terminal tickets, truncate the log
├── stats.rs      # rp stats — size distributions + fold timing
├── json.rs       # JSON value, parser and serialisers (std only)
├── toml.rs       # the TOML subset config and templates need (std only)
├── time.rs       # RFC 3339 ms timestamps, monotonic per process
├── util.rs       # actor resolution, git helpers
├── cli.rs        # the `rp` entry point (--json, NO_COLOR)
└── main.rs
tests/cli.rs      # end-to-end tests driving the built binary
```

## Tooling

```bash
make check       # cargo fmt --check, clippy -D warnings, cargo test
make release     # target/release/rp
make install     # cargo install into ~/.cargo/bin
```

CI runs the same gate on Linux, macOS and Windows and builds the release
binaries on `v*` tags. `pre-commit install` wires the format/clippy hooks on
commit and the tests on push.

## Contributing

Open an issue first for sizeable changes so we can align on direction before code
is written. Keep the event envelope strict and write events generously — it is
the one load-bearing decision; field names and CLI shape are cheap to change.
Keep the crate dependency-free: `rp` runs in bare agent containers.

## License

[MIT](./LICENSE) © code-factorio
