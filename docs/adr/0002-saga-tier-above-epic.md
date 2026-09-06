# ADR 0002 — Saga: one tier above epic, bounded by construction

**Status:** accepted · **Date:** 2026-09-07

## Context

Spec §5.5 fixed the ticket tree at one level: `epic → task`, and stop. The reason was
cost — unbounded trees bring cycle detection and recursive rollups — and at the time no
outcome in this repo needed more. That has changed. Several outcomes now span more than
one epic, and the only tool for grouping epics is a label, which owns nothing and rolls
nothing up. The tracker's own epic rule also turned out to be incomplete: an epic is
`done` only when every child is `done`, so one `dropped` leaf pins its epic `open` for
ever, and three epics in this log are pinned that way today.

Prior art was surveyed against primary sources
([docs/research/saga-tier-prior-art.md](../research/saga-tier-prior-art.md)). Every
tracker bounds depth, either by a numeric cap or by construction. Only Jira keeps the
tier above epic as a work type in one hierarchy; the others switch to a container
object. Every tracker but Backlog.md stores status at every tier and derives only
progress. The child-to-parent link is one field on the child in the majority.

The decisions below were worked as a wayfinder map, `[saga] Saga: a tier above epic in
the Rohrpost ticket model` (`RP-6vbsj5`); each numbered point links the ticket that
holds its full reasoning.

## Decision

1. **`saga` is a ticket type**, the fifth beside `task`, `bug`, `spike`, `epic`. One
   entity, one fold, one write path — the Jira shape, not a container object. A saga
   owns two or more epics that share one outcome no single epic completes, plus any leaf
   that belongs to the outcome and to no epic. It is opened only when the second epic
   for an outcome appears, by whoever creates that epic; a concern that cuts across
   epics owned elsewhere is a label. The standalone epic stays the normal case; a
   wayfinder map stays an epic. (`[saga-1]`, `RP-fbzpka`)
2. **Depth is bounded by construction, one tier deeper.** The tier rule: a saga sits
   under nothing and parents epics or leaves; an epic sits under a saga or nothing and
   parents leaves; a leaf sits under a saga, an epic, or nothing and parents nothing. A
   saga never owns a saga. No cycle walk is needed: self-parent is the only parent cycle
   the shape allows. (fixed while charting; `[saga-2]`, `RP-shh8w8`)
3. **The tier rule is enforced at write time and reported by `doctor`.** Every write
   that sets `type` or `parent` is checked once against the post-write shape and
   rejected whole, exit 1, nothing appended, with a message naming the conflict. The
   parent must resolve. `parent=` with an empty value clears the parent. `doctor` gains
   one failing check, `tier_rule`, for what a union merge or an older binary let
   through. An all-leaf saga is guidance, not a finding. (`[saga-2]`, `RP-shh8w8`)
4. **A parent with children shows a derived status on every read path, and no verb
   writes its stored one.** `dropped` when every child is dropped; `done` when every
   child is settled and one is done; `open` otherwise; composing through the epic tier.
   `close`, `drop`, `claim` and `set status=` on such a parent are accepted only as the
   idempotent no-op that equals the derived status. This replaces the epic rule too;
   it is stricter than every surveyed tracker, on purpose. Rollup and blocking stay
   asymmetric: a dropped child settles its parent, a dropped blocker keeps its
   dependent blocked. (`[saga-3]`, `RP-mqe9sy`)
5. **`parent` stays the only structural field**, and `rp tree` stays the one query-time
   inversion, downward from the root the caller names. Text: two spaces of indent per
   level, line format unchanged, derived status on every line. JSON: an epic entry
   carries a nested `children` array of short tickets, a leaf entry carries none, so
   `tree` on an epic is byte-identical to today. `list --parent` matches the direct
   parent only and takes every filter; `tree` descends and takes none. `rp` surfaces no
   ancestry: a leaf's saga is its epic's `parent`, two `show` calls away.
   (`[saga-4]`, `RP-9qkc5g`; `[saga-5]`, `RP-p3qna7`; `[saga-9]`, `RP-hwzwyd`)

The normative text is spec §5.5, amended in the same commit as this ADR; the
"when to use" paragraph and the worked example the user guide and the skill share
were settled in `[saga-8]` (`RP-hy2rd2`, branch `prototype/saga-8-worked-example`).

## Rejected

- **Unbounded nesting.** Brings the cycle walk and recursive rollup §5.5 refused, for
  a depth no surveyed tracker uses in practice.
- **Saga in saga.** The same cost for one more level nobody asked for; the outcome a
  saga names is already the top of the story.
- **Stored status at the saga tier**, as every surveyed tracker does. Closing a child
  would cascade a write into the parent — the write amplification §5.4 exists to
  avoid — and the current epic rule already shows that a hidden stored status only
  confuses (`RP-0v3thf`).
- **Doctor-only enforcement** of the tier rule. A rejected write costs one fold the
  command already performs; a finding after the fact leaves a tree the fold cannot
  render and a human to untangle it.
- **A container object** (project, initiative, milestone) instead of a ticket type. A
  second entity means a second fold and a second write path.
- **A recursive `list --parent` or a filtered `tree`.** One flag would mean two things,
  and a filtered tree has no rule for a parent whose children are all filtered out.
- **An `ancestors` key on `show` or the short shape.** Ids give no context, bodies do,
  so the agent calls `show` per hop anyway; the tier rule bounds the walk at two.

## Consequences

- Spec §5.3, §5.4, §5.5, §10, §10.1 and §13.6 are amended; §12 lists the saga tier as
  pending until one implementation ticket lands it (`rp` today knows no `saga`).
- The epic rule changes with it: a dropped child settles its epic, an all-dropped epic
  reads `dropped`, and the stored status of an epic with children is never shown or
  written. Bug `RP-0v3thf` (JSON shows the stored status of an epic) is fixed by the
  same change.
- The user guide, the installable skill, `docs/agents/issue-tracker.md` and
  `docs/maintainers/architecture.md` gain the saga type, the shared paragraph, the
  worked example, the `tree` nesting note, the `parent=` clear and the two-hop ancestry
  line, and drop "one level of nesting". `CONTEXT.md` already carries Leaf, Epic, Saga,
  Tier rule, Settled and Derived status.
- Body spill (§13.2) is size-triggered and type-blind, so a saga body needs no rule of
  its own.
- Wayfinder maps stay epics. Turning them into sagas is a separate effort.
