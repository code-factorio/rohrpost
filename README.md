# rohrpost

> A git-native ticket system for agentic coding workflows.

`rohrpost` keeps work items as files in the repository they belong to. Coding
agents create, claim, update and close them without leaving the repo. The repo
is canonical; GitHub, Jira, GitLab and Linear are projections that get synced.
The binary is **`rp`**.

The design assumption is that ~95% of all reads and writes come from agents, not
humans — every trade-off resolves in favour of machine ergonomics. The event log
is the single source of truth; everything else is derived and disposable.

## Status

Alpha — the project scaffolding, the id scheme and the event-envelope primitives
are in place; the store, fold and sync layers are under active development. See
[`docs/spec/ROHRPOST-SPEC.md`](docs/spec/ROHRPOST-SPEC.md) for the full design.

## Requirements

- Python **3.14+**
- [uv](https://docs.astral.sh/uv/) for environment and dependency management

## Quick start

```bash
# install the project + the dev toolchain into an isolated .venv
uv sync

# the binary is runnable immediately
uv run rp --version

# format & lint
uv run ruff format
uv run ruff check

# run the test suite with coverage
uv run pytest
```

`uvx rohrpost` runs the tool with no prerequisite toolchain — which matters
because agents invoke it from bare containers.

## Project layout

```
src/rohrpost/
├── __init__.py        # public API surface
├── ids.py             # ticket ids + ULIDs (the load-bearing id scheme)
├── events.py          # append-only event envelope (msgspec) + JSONL codec
├── exceptions.py      # domain error hierarchy
└── cli.py             # the `rp` entry point
```

### The event log is truth

Everything starts as an append-only event in `.rohrpost/log.jsonl`. Tickets are a
*fold* over that log, regenerated on demand and disposable. One write path:
mutations go through `rp`, never by hand-editing the log.

```python
from rohrpost.events import Event, encode, decode_line
from rohrpost.ids import new_ticket_id, new_ulid

event = Event(
    id=new_ulid(),
    ts="2026-08-11T09:20:14.221Z",
    ticket=f"FAC-{new_ticket_id()}",
    op="set",
    actor="runner/claude-code@b-3",
    set={"status": "in_progress"},
)
line = encode(event)  # one JSONL line
decode_line(line) is event  # round-trips
```

## Tooling

A deliberate, layered quality gate is wired up. Fast checks run on every
commit; the full suite runs on push and in CI.

| Layer         | Tool(s)                                   |
| ------------- | ----------------------------------------- |
| Format & lint | `ruff` (format + check)                   |
| Types         | `ty`, `mypy`, `pyright`                   |
| Security      | `bandit`                                  |
| Structure     | `pyscn` (DRY / YAGNI)                     |
| Tests         | `pytest`, `coverage`, `hypothesis`        |
| Complexity    | `radon`, `xenon` (cyclomatic / MI)        |
| Mutation      | `mutmut`                                  |

Run everything locally:

```bash
make help        # list available targets
make check       # the full deterministic gate (lint + types + tests + metrics)
make mutation    # mutation testing (slow; not part of `make check`)
```

### Pre-commit hooks

```bash
uv run pre-commit install   # installs both commit- and pre-push-stage hooks
```

## Contributing

This project uses the layered tooling above. Open an issue first for sizeable
changes so we can align on direction before code is written.

## License

[MIT](./LICENSE) © code-factorio
