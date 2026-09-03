# ADR 0001 — Rewrite `rp` in dependency-free Rust and drop the sync layer

**Status:** accepted · **Date:** 2026-09-03

## Context

`rp` was a Python 3.14 package (`msgspec`, `httpx`) run through `uv`. Agents
invoke it from bare containers on Linux, macOS and Windows, where a Python
toolchain, a virtual environment and a lock file are a heavier prerequisite
than the tool itself. The Windows pass (map `RP-06ywvd`) spent most of its
effort on runtime plumbing — `uv` on Windows, `msvcrt` locking, text-mode file
descriptors, wrapper scripts per shell — rather than on tickets.

The phase-1 sync layer (three-way merge against shadow snapshots, a GitHub
provider, `link`/`unlink`/`sync`/`conflicts`/`resolve`) was an order of
magnitude more code than the store and had no user: mirroring into a SaaS
tracker turned out to be overkill for the workflows Rohrpost serves.

## Decision

1. **Rust, standard library only.** One static binary per platform, no crates
   beyond `std`. JSON, the TOML subset used by `config.toml` and templates,
   argv parsing, timestamps and entropy are implemented in-tree. The lock is
   `std::fs::File::lock` (`flock` / `LockFileEx`), appends use append mode
   (`O_APPEND` / `FILE_APPEND_DATA`). Minimum Rust is 1.89 (where `File::lock`
   stabilised).
2. **The sync layer is removed**, together with the `remotes` field, the
   `shadow/` directory, the `merge=ours` git attribute and the `[remotes.*]`
   config tables. The `link`, `unlink` and `synced` ops still *decode* so old
   logs keep folding; the fold ignores their payload and `rp doctor` reports
   how many such legacy events a log carries.
3. **No snapshot cache.** `tickets.jsonl` and its mtime staleness protocol are
   gone. The native cold fold of the 30 000-event reference log measures under
   ~100 ms on a slow aarch64 box (~25 ms on a laptop), well below the point at
   which the cache paid for itself in Python. `rp stats` keeps reporting
   `fold_ms` so this can be revisited with data.
4. **Compaction moves only live-log events** and appends to the archive before
   rewriting the log, closing `RP-7hwnnt` (events lost if interrupted between
   the two steps) and a latent re-archiving duplication.

## Consequences

- Install is "put `rp` on `PATH`": release binaries per platform, or
  `cargo install`. The agent skill's wrappers resolve `$ROHRPOST_HOME/bin/rp`.
- Behavioural compatibility with the Python `rp` is bug-for-bug for everything
  kept: `list`/`log --json` are byte-identical on this repository's log; `show`
  loses the `remotes` key and emits `_fieldts` in sorted key order.
- `rp list --status ready` now works as documented (it matched nothing before).
- Anything that wants a remote tracker lives one level up and drives `rp --json`,
  exactly as spec §3.1 always required for everything else.
