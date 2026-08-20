# Prior art: addressing objects that have random identifiers

Research note for **How comparable tools make random ids addressable** (`RP-twaq4c`), a child
of the wayfinder map **Human-addressable tickets** (`RP-3er9f4`).

Rohrpost ticket ids are 6 lowercase base32 characters from 30 random bits, rendered
`RP-jdt9c2` (spec §5.1). Sequential numbers are rejected deliberately: parallel runners on
separate branches cannot allocate them without coordination. The question is therefore not
*what should the id be* but *how does a person address it* — and Rohrpost is not the first
tool to face that.

This is **evidence, not a recommendation.** The consuming decisions are separate tickets.

## Summary

- Jujutsu resolves any non-empty **unique** change-id prefix and rejects ambiguous ones; it
  displays at least eight characters while separately styling the actually distinguishing
  prefix, so the reader can see how much they need to type.
- Jujutsu stabilises short prefixes by resolving first within a configurable **revset** —
  defaulting to the revisions relevant to `jj log` — rather than letting every object in the
  repository lengthen them.
- Git also accepts unique object-id prefixes, but its automatic display minimum grows with
  the approximate object count, on birthday-bound collision maths.
- Mercurial's local integers are convenient but explicitly documented as **unsafe for
  communication**, because they differ between copies of a repository.
- Sapling de-emphasises those integers in favour of short hashes, bookmarks, revsets, and
  relative navigation.
- GitHub, Linear, and Jira can use compact sequential keys only because creation passes
  through a **centralised allocator**.
- Beads supports unique id prefixes with deterministic candidate lists on ambiguity; Seeds
  requires exact ids and keeps title search as a separate, list-producing command.
- Across every primary source, **title matching is treated as search or set selection, never
  as identity.**

## Jujutsu (`jj`)

The closest analogue: random change ids that humans must nonetheless type.

**Mechanism.** Change ids are random 16-byte values. Their canonical representation uses the
letters `z` through `k` as hex digits, while commit ids use ordinary `0-9a-f` — the alphabets
are **disjoint**, so a token cannot be syntactically valid as both kinds of id. The glossary
documents both representations, though no prose rationale beyond that separation was found.
([glossary source](https://github.com/jj-vcs/jj/blob/7007d08537a872dd708f112e2b289ca5a681c692/docs/glossary.md),
[docs](https://docs.jj-vcs.dev/latest/glossary/#change-id))

Any non-empty prefix is accepted when it identifies exactly one change; empty prefixes are
always ambiguous. The shortest displayed length is one more hex digit than the longest common
prefix with either neighbouring id, minimum one character.
([id_prefix.rs](https://github.com/jj-vcs/jj/blob/7007d08537a872dd708f112e2b289ca5a681c692/lib/src/id_prefix.rs),
[object_id.rs](https://github.com/jj-vcs/jj/blob/7007d08537a872dd708f112e2b289ca5a681c692/lib/src/object_id.rs))

The default template calls `id.shortest(8)`: output is at least eight characters but expands
when needed. `ShortestIdPrefix` distinguishes the unique prefix from the padding, and the
default colours render `prefix` bold and `rest` bright black.
([templates.toml](https://github.com/jj-vcs/jj/blob/7007d08537a872dd708f112e2b289ca5a681c692/cli/src/config/templates.toml),
[colors.toml](https://github.com/jj-vcs/jj/blob/7007d08537a872dd708f112e2b289ca5a681c692/cli/src/config/colors.toml),
[template API](https://docs.jj-vcs.dev/latest/templates/#changeid-type))

**Ambiguity.** Resolution has three explicit outcomes: no match, one match, ambiguous. An
ambiguous revset symbol fails with exactly `Change ID prefix '<prefix>' is ambiguous`.
`change_id(prefix)` and `commit_id(prefix)` likewise document non-unique prefixes as errors.
([revset.rs](https://github.com/jj-vcs/jj/blob/7007d08537a872dd708f112e2b289ca5a681c692/lib/src/revset.rs),
[revsets](https://docs.jj-vcs.dev/latest/revsets/#functions))

A second kind of ambiguity exists when one change id has multiple visible commits — a
divergent change — disambiguated by offsets such as `xyz/0` and `xyz/1`.
([glossary](https://docs.jj-vcs.dev/latest/glossary/#change-offset))

**Prefix stability and cost.** `IdPrefixContext` builds a disambiguation index over a
configured revset. Resolution consults that narrower set first and falls back to the whole
repository only when the prefix has no match there. The default follows `revsets.log`, which
limits interference from unrelated history; bookmark and tag names are also considered,
because they win symbol-resolution priority.
([id_prefix.rs](https://github.com/jj-vcs/jj/blob/7007d08537a872dd708f112e2b289ca5a681c692/lib/src/id_prefix.rs),
[revsets.toml](https://github.com/jj-vcs/jj/blob/7007d08537a872dd708f112e2b289ca5a681c692/cli/src/config/revsets.toml))

The trade-off is **context dependence**: the required prefix changes when the configured
revset, visible changes, bookmarks, or tags change. A prefix unique inside the scope also
resolves intentionally despite an unrelated collision outside it.

## Git

**Mechanism.** Git accepts a leading object-id substring only when unique in the repository.
`--abbrev=<n>` and `core.abbrev` set a minimum output length; uniqueness checks may lengthen
the displayed value. ([gitrevisions](https://git-scm.com/docs/gitrevisions),
[--abbrev](https://git-scm.com/docs/git-log#Documentation/git-log.txt---abbrevltngt),
[core.abbrev](https://git-scm.com/docs/git-config#Documentation/git-config.txt-coreabbrev))

In automatic mode Git approximates the object count `N`, computes `b = floor(log2 N)+1`, then
selects at least `ceil(b/2)` hex characters with a floor of seven. The source explains this as
a birthday bound: among roughly `2^b` objects, expect a collision around `2^(b/2)`. Individual
abbreviations are then lengthened until unique.
([odb.c](https://github.com/git/git/blob/dea0ea3582e6980ddbc1173cc8e3e9f9db91cde0/odb.c),
[object-name.c](https://github.com/git/git/blob/dea0ea3582e6980ddbc1173cc8e3e9f9db91cde0/object-name.c))

**Ambiguity.** Git rejects an ambiguous short id with `error: short object ID <prefix> is
ambiguous` and — when advice is enabled — **lists the candidates** with object types and
commit dates/subjects.
([object-name.c](https://github.com/git/git/blob/dea0ea3582e6980ddbc1173cc8e3e9f9db91cde0/object-name.c))

**Escape hatches and cost.** Hashes can be avoided entirely via branch/tag names, `@`/`HEAD`,
`@{-1}`, reflog selectors, and ancestry expressions like `HEAD~3`. Git also supports
`:/regex`, which selects the **youngest** reachable commit whose message matches — unlike
unique-id resolution, this title-like lookup deliberately picks one of several matches.
([git-rev-parse](https://git-scm.com/docs/git-rev-parse))

The cost is repository-relative meaning: fetching more objects can invalidate a formerly
unique abbreviation, and names and relative expressions depend on mutable refs, reflogs, or
checkout state.

## Mercurial and Sapling

### Mercurial

**Mechanism.** Accepts local integer revision numbers, stable 40-digit node hashes, unique
hash prefixes, bookmarks, tags, branches, `tip`, and `.`.
([revisions](https://www.mercurial-scm.org/help/topics/revisions.html))

**Ambiguity.** A short hash is valid only when it prefixes exactly one full identifier.
Integer revnums never appear ambiguous locally — they simply mean a *different* changeset in
another clone.

**Cost / regret.** The Mercurial wiki calls revnums "strictly local convenience identifiers",
says they are "very likely" to differ in another copy, and warns explicitly: **"Do not use
them to talk about changesets with other people."**
([RevisionNumber](https://wiki.mercurial-scm.org/RevisionNumber))

This is the sharpest first-party statement of the hazard Rohrpost's §5.1 avoids by
construction.

### Sapling

**Mechanism.** Documented navigation foregrounds full hashes, short unique prefixes,
bookmarks, revsets, and relative commands — `next`, `prev`, `top`, `bottom`. Smartlog shows
short hashes, bookmarks, titles, and graph context.
([navigation](https://sapling-scm.com/docs/overview/navigation/),
[smartlog](https://sapling-scm.com/docs/overview/smartlog/))

**Ambiguity.** Short hashes must be unique; `next`/`prev` alert when the graph offers several
choices. ([next/prev](https://sapling-scm.com/docs/overview/navigation/#nextprev))

**Cost.** Sapling still exposes Mercurial-derived local revision numbers in templates but
omits them from documented `goto` identifier forms and the default Smartlog. It pays instead
for graph-aware navigation and richer contextual display.
([log](https://sapling-scm.com/docs/commands/log/))

## Centrally allocated keys: GitHub, Linear, Jira

These are the counterexample — compact, memorable keys bought with coordination.

**GitHub CLI.** `gh issue view 21` takes a repository-scoped integer or URL; `--repo` supplies
the namespace. No prefix ambiguity exists inside a repository — the integer is exact — but
without the intended repository context the same number means something else. Creation passes
through GitHub's centralised API, which returns a server-assigned `number`; pull requests
share the namespace. ([gh manual](https://cli.github.com/manual/gh_issue_view),
[REST API](https://docs.github.com/en/rest/issues/issues#create-an-issue))

**Linear.** Every issue belongs to a team and carries a team identifier plus number, e.g.
`BLA-123`; the API accepts both the shorthand and a UUID. Creation is a mutation against a
centralised GraphQL endpoint carrying a `teamId`, which lets the service allocate and enforce
the team-local number. ([creating issues](https://linear.app/docs/creating-issues),
[GraphQL](https://linear.app/developers/graphql#creating-editing-issues))

**Jira.** A project key plus sequential number forms the key, e.g. `EXAMPLE-1`. Jira preserves
previous project-key aliases after a rename so old links keep resolving. The central service
owns key configuration and allocation; renaming requires administrator coordination and
retained alias state.
([project details](https://support.atlassian.com/jira-cloud-administration/docs/edit-a-projects-details/))

The pattern is consistent: **short sequential keys require a single allocator.** That is
precisely the dependency Rohrpost's design exists to avoid.

## Beads and Seeds

Named in spec Appendix A as design ancestors.

### Beads

**Mechanism.** Adaptive random/hash ids: four characters for small stores, growing with issue
count. Collision probability is computed with the birthday approximation, and generation
retries on collision.
([adaptive-ids.md](https://github.com/gastownhall/beads/blob/d38ac728b581c8595fae36344ecca68830c7f3b5/docs/core-concepts/adaptive-ids.md))

It accepts bare or prefixed leading abbreviations, with **exact matches taking priority over
prefix matches**.
([id_parser.go](https://github.com/gastownhall/beads/blob/d38ac728b581c8595fae36344ecca68830c7f3b5/internal/utils/id_parser.go))

**Ambiguity.** Multiple matches return `ErrAmbiguousID` carrying a **sorted** candidate list
and the message `Use more characters to disambiguate`. The sort makes output deterministic
across storage backends.

**Cost.** Id lengths vary over time, prefix resolution requires searching stored ids, and the
optional counter mode reintroduces branch-coordination hazards. Beads advertises fuzzy title
lookup, but the id resolver itself is strictly prefix-based.

### Seeds

**Mechanism.** Generates `prefix-` plus four random lowercase hex characters, checking current
ids for exact collisions and falling back to eight characters after 100 failed attempts.
([id.ts](https://github.com/jayminwest/seeds/blob/608a13be9dee932f925e991ef2cf84dfd70097d5/src/id.ts))

**Ambiguity.** None — `sd show` and the mutation commands compare `i.id === id`; abbreviated
ids are not resolved, and a miss produces `Issue not found: <id>`.
([show.ts](https://github.com/jayminwest/seeds/blob/608a13be9dee932f925e991ef2cf84dfd70097d5/src/commands/show.ts))

**Cost.** Four hex characters give only 65,536 possibilities, so collision checking depends on
the locally visible store. Title and description matching exist **only** through `sd search`,
which returns case-insensitive substring matches as a list rather than resolving a title to an
identity. Shell completion covers command names and flags, **not** ticket ids.
([search.ts](https://github.com/jayminwest/seeds/blob/608a13be9dee932f925e991ef2cf84dfd70097d5/src/commands/search.ts),
[completions.ts](https://github.com/jayminwest/seeds/blob/608a13be9dee932f925e991ef2cf84dfd70097d5/src/commands/completions.ts))

## Additional prior art

ULID encodes 128 bits in 26 Crockford-base32 characters, excluding `I`, `L`, `O`, and `U` for
readability — the same alphabet choice Rohrpost makes in `ids.py`. The specification defines
**no** abbreviation or ambiguity-resolution rule, so shortening discards the uniqueness
guarantee unless the application adds its own index. ([spec](https://github.com/ulid/spec))

## What this leaves for the consuming decisions

**For id-prefix resolution:** every tool surveyed that has random ids and human users
implements it, and all of them treat ambiguity as a hard error. The live variables are the
*minimum length* (fixed vs. scaling with count), whether the candidate list is printed on
ambiguity (git and beads print; both find it worth the code), and whether resolution is scoped
to a subset (jj's revset) or the whole store.

**For title resolution:** no surveyed tool treats a title as an identity. The two that offer
title lookup at all keep it in a separate command that returns a *list* (`sd search`), or make
the multiple-match rule explicit and positional (`git :/regex` takes the youngest). This is
the strongest signal in the survey.
