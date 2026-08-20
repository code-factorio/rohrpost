# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring
the codebase. This repo is **single-context**: one `CONTEXT.md` and one `docs/adr/` at the
root, no `CONTEXT-MAP.md`.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root: the glossary and domain narrative.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't
suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs`
and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually
get resolved.

## File structure

```
/
├── CONTEXT.md
├── docs/
│   ├── adr/
│   │   ├── 0001-append-only-log-as-truth.md
│   │   └── 0002-display-only-prefix.md
│   ├── agents/       ← this file: skill configuration
│   ├── maintainers/  ← how Rohrpost is built
│   ├── spec/         ← the behavioural contract
│   └── users/        ← how Rohrpost is driven
└── src/
```

Note the neighbours: `docs/spec/ROHRPOST-SPEC.md` is the normative contract and
`docs/maintainers/architecture.md` describes the implementation. `CONTEXT.md` and ADRs do
not restate them — they capture vocabulary and the reasoning behind decisions. When a
decision is already settled in the spec, cite it rather than duplicating it.

## Use the glossary's vocabulary

When your output names a domain concept (in a ticket title, a refactor proposal, a
hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms
the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing
language the project doesn't use (reconsider) or there's a real gap (note it for
`/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently
overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
