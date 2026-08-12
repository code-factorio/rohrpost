"""The fold: turning the append-only event log into the ticket snapshot.

Spec §6. The log is truth; a ticket is a *fold* over the events for one id.
Folding is deterministic and stateless: the same log always yields the same
tickets, which is why ``tickets.jsonl`` is disposable and regenerable.

The algorithm:

1. Read all events (archive then live log).
2. **Deduplicate by event ``id``** — a ``merge=union` of the log can produce
   duplicate lines, and every event id is unique, so dedupe is exact.
3. **Sort by ``(ts, id)``** — the ULID tiebreak makes the order total and
   deterministic even for events that share a millisecond.
4. Replay events in order, applying each op's payload **field by field** and
   recording ``_fieldts[field] = ts``.
5. **Last write wins, per field** — not per record. Two runners updating
   ``status`` and ``priority`` concurrently both win; whole-record LWW would
   silently discard one (spec §6).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path

from rohrpost import store
from rohrpost.events import Event
from rohrpost.exceptions import StoreError
from rohrpost.ids import normalize_id

#: Stored status values (spec §5.4). ``ready`` is deliberately absent: it is
#: derived at query time and never written to the log.
STATUSES: frozenset[str] = frozenset(
    {"open", "in_progress", "review", "waiting", "done", "dropped"}
)
#: Terminal statuses — once reached the work is finished one way or another.
TERMINAL: frozenset[str] = frozenset({"done", "dropped"})
#: Ticket types (spec §5.3). Cheap to add, awkward to remove (§13 open question 6).
TYPES: frozenset[str] = frozenset({"task", "bug", "spike", "epic"})

#: Scalar (whole-value) fields updated by field-level last-write-wins.
SCALAR_FIELDS: frozenset[str] = frozenset(
    {"title", "type", "status", "priority", "assignee", "parent", "body"}
)
#: Array fields updated by set add/remove ops (``<field>+`` / ``<field>-``).
SET_FIELDS: frozenset[str] = frozenset({"labels", "blocked_by"})

DEFAULT_TYPE: str = "task"
DEFAULT_STATUS: str = "open"
DEFAULT_PRIORITY: int = 2


@dataclass(frozen=True, slots=True)
class Comment:
    """A local note appended to a ticket (spec §9). Never synced."""

    ts: str
    actor: str
    text: str


@dataclass(frozen=True, slots=True)
class Ticket:
    """The folded shape of a ticket (spec §5.3).

    ``id``/``parent``/``blocked_by`` are **bare** ticket ids internally (the
    display prefix is applied only at the output layer). ``ready`` is not stored
    here — it is computed from the dependency graph at query time (:func:`is_ready`).
    """

    id: str
    title: str
    type: str
    status: str
    priority: int
    parent: str | None
    blocked_by: list[str]
    labels: list[str]
    assignee: str | None
    body: str | None
    remotes: dict[str, str]
    last_close_reason: str | None
    comments: list[Comment]
    created: str
    updated: str
    fieldts: dict[str, str]


@dataclass(slots=True)
class _Builder:
    """Mutable per-ticket accumulator used while replaying the log."""

    id: str
    title: str | None = None
    type: str | None = None
    status: str | None = None
    priority: int | None = None
    parent: str | None = None
    assignee: str | None = None
    body: str | None = None
    labels: set[str] = field(default_factory=set)
    blocked_by: set[str] = field(default_factory=set)
    remotes: dict[str, str] = field(default_factory=dict)
    last_close_reason: str | None = None
    comments: list[Comment] = field(default_factory=list)
    created: str | None = None
    updated: str | None = None
    fieldts: dict[str, str] = field(default_factory=dict)

    def freeze(self) -> Ticket:
        return Ticket(
            id=self.id,
            title=self.title or "",
            type=self.type or DEFAULT_TYPE,
            status=self.status or DEFAULT_STATUS,
            priority=self.priority if self.priority is not None else DEFAULT_PRIORITY,
            parent=self.parent,
            blocked_by=sorted(self.blocked_by),
            labels=sorted(self.labels),
            assignee=self.assignee,
            body=self.body,
            remotes=dict(self.remotes),
            last_close_reason=self.last_close_reason,
            comments=list(self.comments),
            created=self.created or "",
            updated=self.updated or "",
            fieldts=dict(self.fieldts),
        )


def _bare_id(value: str) -> str:
    """Defensively normalise an event's ticket/parent id to bare form.

    The store writes bare ids, but the fold is tolerant of a hand-authored log
    that uses rendered ids (``RP-a1b2c3``): :func:`normalize_id` accepts both.
    A value that does not parse is returned unchanged so ``rp doctor`` can flag it.
    """
    try:
        return normalize_id(value)
    except StoreError:
        return value


def _set_values(field: str, value: object) -> list[str]:
    """Coerce a set-op payload to a list of strings.

    ``blocked_by`` values are ticket ids and are normalised to bare form; other
    set fields (``labels``) hold free-form strings and are kept verbatim.
    """
    items = value if isinstance(value, list) else [value]
    strs = [str(v) for v in items]
    if field == "blocked_by":
        return [_bare_id(s) for s in strs]
    return strs


def _apply_set(builder: _Builder, payload: dict[str, object], ts: str, reason: str | None) -> None:
    """Apply a ``create``/``set`` payload field-by-field (field-level LWW by order)."""
    for key, value in payload.items():
        if key.endswith("+"):
            name = key[:-1]
            if name in SET_FIELDS:
                getattr(builder, name).update(_set_values(name, value))
                builder.fieldts[name] = ts
        elif key.endswith("-"):
            name = key[:-1]
            if name in SET_FIELDS:
                getattr(builder, name).difference_update(_set_values(name, value))
                builder.fieldts[name] = ts
        elif key in SCALAR_FIELDS:
            _apply_scalar(builder, key, value, ts)
            if key == "status" and isinstance(value, str) and value in TERMINAL:
                builder.last_close_reason = reason
        # Unknown keys are ignored by the fold: a future field should not crash
        # older code. ``rp doctor`` warns about unmapped payload keys.


def _apply_scalar(builder: _Builder, key: str, value: object, ts: str) -> None:
    if key == "priority":
        # bool is an int subclass but a nonsensical priority; anything else must
        # look like a number or numeric string. Malformed values are skipped so the
        # fold stays total — ``rp doctor`` reports them separately.
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return
        try:
            builder.priority = int(value)
        except ValueError:
            return
        builder.fieldts["priority"] = ts
        return
    if not isinstance(value, (str, type(None))):
        return
    if key == "parent" and isinstance(value, str):
        value = _bare_id(value)
    setattr(builder, key, value)
    builder.fieldts[key] = ts


def _dedup_sort(events: list[Event]) -> list[Event]:
    """Deduplicate by event ``id`` then sort by ``(ts, id)`` (§6, total order)."""
    seen: set[str] = set()
    unique: list[Event] = []
    for ev in events:
        if ev.id not in seen:
            seen.add(ev.id)
            unique.append(ev)
    unique.sort(key=lambda e: (e.ts, e.id))
    return unique


def _apply_event(builder: _Builder, ev: Event) -> None:
    """Apply one event's op-specific effect to a ticket builder."""
    if ev.op in ("create", "set") and ev.set:
        _apply_set(builder, ev.set, ev.ts, ev.reason)
    elif ev.op == "comment" and ev.text is not None:
        builder.comments.append(Comment(ts=ev.ts, actor=ev.actor, text=ev.text))
    elif ev.op == "link" and ev.remote is not None and ev.ref is not None:
        builder.remotes[ev.remote] = ev.ref
        builder.fieldts["remotes"] = ev.ts
    elif ev.op == "unlink" and ev.remote is not None:
        builder.remotes.pop(ev.remote, None)
        builder.fieldts["remotes"] = ev.ts
    # "synced" carries no per-ticket field state (it is a sync watermark).


def fold(events: list[Event]) -> dict[str, Ticket]:
    """Fold a list of events into ``{bare_id: Ticket}``.

    Deduplicates by event ``id``, sorts by ``(ts, id)``, then replays. The input
    need not be pre-sorted or de-duplicated.
    """
    builders: dict[str, _Builder] = {}
    for ev in _dedup_sort(events):
        if ev.op == "synced":
            continue
        tid = _bare_id(ev.ticket)
        builder = builders.get(tid)
        if builder is None:
            builder = _Builder(id=tid)
            builders[tid] = builder
        if builder.created is None:
            builder.created = ev.ts
        builder.updated = ev.ts
        _apply_event(builder, ev)
    return {tid: builder.freeze() for tid, builder in builders.items()}


# ---------------------------------------------------------------------------
# Derived/computed views over a folded set (spec §5.4 readiness, §5.5 epics).
# ---------------------------------------------------------------------------
def derive_status(ticket: Ticket, by_id: dict[str, Ticket]) -> str:
    """Stored status, except for epics whose status is derived from children (§5.5).

    An epic with children is ``done`` when every child is ``done``; otherwise
    ``open``. An epic without children keeps its stored status. Non-epics return
    their stored status unchanged.
    """
    if ticket.type != "epic":
        return ticket.status
    children = [c for c in by_id.values() if c.parent == ticket.id]
    if not children:
        return ticket.status
    return "done" if all(c.status == "done" for c in children) else "open"


def is_ready(ticket: Ticket, by_id: dict[str, Ticket]) -> bool:
    """True if the ticket is actionable now (spec §5.4): ``open`` and unblocked.

    Epics are never "ready" (you work their children, not the epic). ``waiting``
    is excluded because it is a distinct stored status. A ``blocked_by`` edge to
    an unknown or non-``done`` ticket keeps this false.
    """
    if ticket.type == "epic":
        return False
    if ticket.status != "open":
        return False
    for dep in ticket.blocked_by:
        blocker = by_id.get(dep)
        if blocker is None or blocker.status != "done":
            return False
    return True


def find_cycle(by_id: dict[str, Ticket]) -> list[str] | None:
    """Return a cyclic path of bare ids via ``blocked_by`` edges, or ``None`` if acyclic.

    A dependency cycle makes readiness non-well-founded; ``rp doctor`` reports it.
    """
    white, gray, black = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(by_id, white)
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = gray
        stack.append(node)
        for dep in by_id[node].blocked_by:
            if dep not in color:
                continue
            if color[dep] == gray:
                return [*stack[stack.index(dep) :], dep]
            if color[dep] == white:
                found = dfs(dep)
                if found is not None:
                    return found
        stack.pop()
        color[node] = black
        return None

    for tid in by_id:
        if color[tid] == white:
            cycle = dfs(tid)
            if cycle is not None:
                return cycle
    return None


# ---------------------------------------------------------------------------
# Snapshot cache: tickets.jsonl is regenerable; reload only when stale (§7).
# ---------------------------------------------------------------------------
def fold_all(rohrpost_dir: Path) -> dict[str, Ticket]:
    """Read + dedupe + sort + fold the whole log into ``{bare_id: Ticket}``."""
    return fold(store.read_events(rohrpost_dir))


def load_tickets(rohrpost_dir: Path) -> dict[str, Ticket]:
    """Return the folded tickets, using the snapshot cache when fresh (§7).

    ``tickets.jsonl`` is GITIGNORED and disposable. It exists only to avoid
    re-folding on every CLI invocation. It is reused when its mtime is at least
    as new as the live log's; otherwise it is regenerated and overwritten.
    """
    from rohrpost import paths

    log = paths.log_path(rohrpost_dir)
    snap = paths.snapshot_path(rohrpost_dir)
    log_mtime_ns = log.stat().st_mtime_ns if log.is_file() else 0
    # Strictly-newer: a snapshot written in the same nanosecond tick as the last
    # append is treated as stale and re-folded. That trades a rare redundant fold
    # for never returning data that predates an append (correctness over speed).
    if snap.is_file() and snap.stat().st_mtime_ns > log_mtime_ns:
        cached = _read_snapshot(snap)
        if cached is not None:
            return cached
    tickets = fold_all(rohrpost_dir)
    _write_snapshot(snap, tickets)
    return tickets


def _write_snapshot(snap: Path, tickets: dict[str, Ticket]) -> None:
    """Overwrite the snapshot file with the current fold (best-effort; ignore errors)."""
    import json

    try:
        payload = [ticket_to_mapping(t) for t in tickets.values()]
        tmp = snap.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(json.dumps(line) for line in payload) + ("\n" if payload else ""))
        tmp.replace(snap)
    except OSError:
        pass  # the snapshot is disposable; failure to cache is non-fatal


def _read_snapshot(snap: Path) -> dict[str, Ticket] | None:
    """Read a snapshot back into tickets, or ``None`` if it cannot be parsed."""
    import json

    result: dict[str, Ticket] = {}
    # The snapshot is a disposable cache: any read/parse/schema failure means
    # "treat as stale and re-fold", hence the broad suppress.
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        for raw in snap.read_text().splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            obj = json.loads(stripped)
            result[str(obj["id"])] = _mapping_to_ticket(obj)
        return result
    return None


# ---------------------------------------------------------------------------
# (De)serialisation of the folded ticket shape for the snapshot and for --json.
# ---------------------------------------------------------------------------
def ticket_to_mapping(
    ticket: Ticket,
    *,
    prefix: str | None = None,
    include_fieldts: bool = True,
    include_comments: bool = True,
    include_body: bool = True,
) -> dict[str, object]:
    """Convert a ticket to a plain mapping.

    With ``prefix`` set, ``id``/``parent``/``blocked_by`` are rendered with the
    display prefix (the form humans and the spec's examples use); ``fieldts`` is
    emitted under the spec's ``_fieldts`` key. ``include_comments`` carries the
    notes list (needed by the snapshot cache and ``rp show``; dropped for the
    short ``rp list`` shape). ``include_body`` is dropped for the short shape too
    — the work-queue view (``rp ready``/``rp list``) must not carry ticket prose
    into the agent context (decision experiment E7). Drop
    ``include_fieldts``/``include_comments`` for compact collection output.
    """

    def rnd(tid: str | None) -> str | None:
        if tid is None:
            return None
        return f"{prefix}-{tid}" if prefix else tid

    mapping: dict[str, object] = {
        "id": rnd(ticket.id),
        "title": ticket.title,
        "type": ticket.type,
        "status": ticket.status,
        "priority": ticket.priority,
        "parent": rnd(ticket.parent),
        "blocked_by": [rnd(b) for b in ticket.blocked_by],
        "labels": list(ticket.labels),
        "assignee": ticket.assignee,
        "body": ticket.body,
        "remotes": dict(ticket.remotes),
        "last_close_reason": ticket.last_close_reason,
        "created": ticket.created,
        "updated": ticket.updated,
    }
    if not include_body:
        # The short list/ready shape drops the body: it is prose that does not
        # belong in the work-queue view (decision experiment E7).
        del mapping["body"]
    if include_comments:
        mapping["comments"] = [comment_to_mapping(c) for c in ticket.comments]
    if include_fieldts:
        mapping["_fieldts"] = dict(ticket.fieldts)
    return mapping


def comment_to_mapping(comment: Comment) -> dict[str, str]:
    return {"ts": comment.ts, "actor": comment.actor, "text": comment.text}


def _as_str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _as_str_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _as_comments(value: object) -> list[Comment]:
    if not isinstance(value, list):
        return []
    return [
        Comment(str(item["ts"]), str(item["actor"]), str(item["text"]))
        for item in value
        if isinstance(item, dict) and {"ts", "actor", "text"} <= item.keys()
    ]


def _mapping_to_ticket(obj: dict[str, object]) -> Ticket:
    """Inverse of :func:`ticket_to_mapping` for snapshot reads (bare ids assumed)."""
    parent = obj.get("parent")
    raw_priority = obj.get("priority", DEFAULT_PRIORITY)
    return Ticket(
        id=str(obj["id"]),
        title=str(obj.get("title", "")),
        type=str(obj.get("type", DEFAULT_TYPE)),
        status=str(obj.get("status", DEFAULT_STATUS)),
        priority=int(raw_priority) if isinstance(raw_priority, (int, float)) else DEFAULT_PRIORITY,
        parent=str(parent) if parent else None,
        blocked_by=_as_str_list(obj.get("blocked_by")),
        labels=_as_str_list(obj.get("labels")),
        assignee=str(obj["assignee"]) if obj.get("assignee") else None,
        body=str(obj["body"]) if obj.get("body") else None,
        remotes=_as_str_dict(obj.get("remotes")),
        last_close_reason=str(obj["last_close_reason"]) if obj.get("last_close_reason") else None,
        comments=_as_comments(obj.get("comments")),
        created=str(obj.get("created", "")),
        updated=str(obj.get("updated", "")),
        fieldts=_as_str_dict(obj.get("_fieldts")),
    )


__all__ = [
    "STATUSES",
    "TERMINAL",
    "TYPES",
    "Comment",
    "Ticket",
    "comment_to_mapping",
    "derive_status",
    "find_cycle",
    "fold",
    "fold_all",
    "is_ready",
    "load_tickets",
    "ticket_to_mapping",
]
