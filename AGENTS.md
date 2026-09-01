This is the opensource project `rohrpost` the tool is supposed to be agent first, users second.

## Agent skills

### Issue tracker

Issues live in Rohrpost itself — this repo dogfoods `rp` (run as `uv run rp ... --json`); no external tracker. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, unchanged: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Post implementation tasks
- After you finished a task, analyze and reflect on the last two changes you made. Identify potential improvements,
  optimizations that could enhance code quality, performance, readability or maintanability.


### Coding preferences
- Maintainability is a must.
- Keep things simple. `KISS` and follow the `YAGNI` mantra unless told otherwise.
- Typehints are useful, use them.
- Tests are good! Smoke tests, regression tests for feature deletions are not useful. Tests should be focused, not slop.
- Comments are a great way to clarify functionality and how code is used. Don't comment every line. Simple functions that are mostly self describing by the name do not need a doc string. More complex functions do. Also what the purpose of a class and what the purpose of a module is, is a good thing to document.
- Keep comments and documentation up to date! When making changes it's important to keep things in sync.

### Documentation
- Separate documentation for maintainers in docs/maintainers, for end users docs/users and maintain an installable skill for coding agents to understand how to use rohrpost most efficiently in ./skills/rohrpost 
  Use `tessl` for skill review, verification and validation.
- Research notes — prior art surveyed against primary sources to inform a decision — go in docs/research, one Markdown file per question, every claim carrying its source link. They record evidence, not recommendations; the decision itself belongs in a ticket or an ADR.


### Committing
- Commit often, self contained changes with a good concise but comprehensive description of what the change in the commit is addressing.