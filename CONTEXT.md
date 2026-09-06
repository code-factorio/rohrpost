# Rohrpost

Rohrpost is a git-native issue tracker for coding agents. Tickets are events in a
log committed with the code, and `rp` is the only write path.

## Language

### Ticket structure

**Leaf**:
A ticket that carries work an actor does: a task, a bug, or a spike. A leaf has no children.
_Avoid_: issue, item, story, subtask

**Epic**:
A ticket that owns the leaves of one deliverable. An epic without a saga is the normal case.
_Avoid_: feature, milestone, project

**Saga**:
A ticket that owns two or more epics that share one outcome no single epic completes,
plus any leaf that belongs to the outcome and to no epic. The top tier: a saga never owns a saga.
_Avoid_: initiative, theme, label, view, project

**Tier rule**:
The shape every parent edge must keep: a saga sits under nothing, an epic sits under a saga
or nothing, a leaf sits under a saga, an epic, or nothing.
_Avoid_: hierarchy check, depth limit, nesting constraint

**Settled**:
A child whose work is over: its status is terminal, `done` or `dropped`. A parent rolls
up over settled children, and a dropped child settles it as much as a done one does.
_Avoid_: closed, finished, resolved

**Derived status**:
The status a parent with children shows on every read path, computed from its children
and never stored: `dropped` when all are dropped, `done` when all are settled and one is
done, `open` otherwise. No verb writes a stored status to such a parent.
_Avoid_: rollup status, computed status, effective status
