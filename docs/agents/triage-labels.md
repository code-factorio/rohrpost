# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the
actual label strings used in this repo's issue tracker (Rohrpost — see
[issue-tracker.md](./issue-tracker.md)).

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the
corresponding label string from this table.

## Applying them

Rohrpost labels are free-form strings — nothing needs creating up front.

```bash
cargo run -q -- set <id> labels+=ready-for-agent labels-=needs-triage --json
cargo run -q -- list --label needs-triage --json          # the triage queue
```

A `wontfix` verdict is also a status change: label it, then `rp drop <id> --reason
"wontfix: <why>"` so the ticket leaves the actionable queue.

Edit the right-hand column to match whatever vocabulary you actually use.
