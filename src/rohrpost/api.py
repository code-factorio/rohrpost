"""High-level ticket operations: the library behind the ``rp`` commands.

This is the *one write path* (spec §3 principle 3). Every mutation constructs a
well-formed :class:`~rohrpost.events.Event`, appends it through
:mod:`rohrpost.store`, and returns the re-folded ticket. The CLI is a thin
adapter over these functions; nothing else should append events.

Idempotency (spec §9.2): re-running ``rp set <id> status=done`` is a no-op, not
an error. Each mutating op folds first, drops assignments that would not change
anything, and appends nothing when there is nothing effective to do — so the log
stays clean under retries.

All functions take the ``.rohrpost/`` directory and return bare-id domain
objects; the display prefix is applied only at the output layer (CLI). The
``now`` clock and ``ulid`` factory are injectable so tests are deterministic.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rohrpost import paths, store
from rohrpost.config import Config, default_config, load_config, render_config_toml, validate_prefix
from rohrpost.events import Event, Op
from rohrpost.exceptions import StoreError, TicketError, TicketNotFoundError
from rohrpost.fold import (
    DEFAULT_PRIORITY,
    DEFAULT_TYPE,
    SET_FIELDS,
    STATUSES,
    TYPES,
    Comment,
    Ticket,
    comment_to_mapping,
    derive_status,
    is_ready,
    load_tickets,
    ticket_to_mapping,
)
from rohrpost.ids import new_ulid, normalize_id
from rohrpost.util import Clock, now_ts

#: Default priority/type re-exported for callers that build UIs.
UlidFactory = Callable[[], str]


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Outcome of a mutation: the ticket and whether an event was appended.

    ``wrote`` is ``False`` when the op was an idempotent no-op (the current
    state already satisfies it), so callers can report "no change" honestly.
    """

    ticket: Ticket
    wrote: bool


# ---------------------------------------------------------------------------
# Field-assignment parsing for ``rp set`` and ``rp new``.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Assignment:
    """One ``field=value`` / ``field+=v,v`` / ``field-=v,v`` directive.

    ``op`` is ``set`` for scalar fields, ``add``/``remove`` for set fields.
    """

    op: Literal["set", "add", "remove"]
    field: str
    value: object  # str for ``set`` (or int via priority); list[str] for add/remove


_SCALAR_FIELD_NAMES: frozenset[str] = frozenset(
    {"title", "type", "status", "priority", "assignee", "parent", "body"}
)
_TEMPLATE_FIELDS: frozenset[str] = _SCALAR_FIELD_NAMES | SET_FIELDS


def parse_assignment(token: str) -> Assignment:
    """Parse a single ``field=value`` token into an :class:`Assignment`.

    ``labels+=auth,bug`` → add to the ``labels`` set; ``labels-=auth`` → remove;
    ``priority=1`` → scalar set (coerced to int); ``status=in_progress`` → scalar
    set. Raises :class:`TicketError` on an unknown field or malformed token.
    """
    if "=" not in token:
        raise TicketError(f"expected field=value, got {token!r}")
    key, raw_value = token.split("=", 1)
    key = key.strip()
    if not key:
        raise TicketError(f"empty field in assignment {token!r}")
    if key.endswith("+") or key.endswith("-"):
        return _parse_set_op(key, raw_value, token)
    return _parse_scalar_assignment(key, raw_value)


def _parse_set_op(key: str, raw_value: str, token: str) -> Assignment:
    field = key[:-1]
    op: Literal["set", "add", "remove"] = "add" if key.endswith("+") else "remove"
    if field not in SET_FIELDS:
        raise TicketError(f"{field!r} is not a set field (cannot use +/-)")
    values = [v.strip() for v in raw_value.split(",") if v.strip()]
    if not values:
        raise TicketError(f"empty value list in assignment {token!r}")
    if field == "blocked_by":
        values = [_normalise_structural(v) for v in values]
    return Assignment(op=op, field=field, value=values)


def _parse_scalar_assignment(key: str, raw_value: str) -> Assignment:
    if key not in _SCALAR_FIELD_NAMES:
        raise TicketError(f"unknown field {key!r}")
    if key == "priority":
        try:
            value: object = int(raw_value)
        except ValueError as exc:
            raise TicketError(f"priority must be an integer, got {raw_value!r}") from exc
    elif key == "parent":
        value = _normalise_structural(raw_value)
    else:
        value = raw_value
    return Assignment(op="set", field=key, value=value)


def _normalise_structural(value: str) -> str:
    """Normalise a parent/blocked_by id to bare form (raises on invalid)."""
    try:
        return normalize_id(value)
    except StoreError as exc:
        raise TicketError(str(exc)) from exc


def load_template(rohrpost_dir: Path, name: str) -> dict[str, object]:
    """Load ticket defaults from ``templates/<name>.toml``.

    Defaults may live at the top level or under ``[defaults]``, ``[fields]`` or
    ``[ticket]``. Command-line values are applied by the CLI after loading.
    """
    requested = name.strip()
    if not requested:
        raise TicketError("template name must be non-empty")
    filename = requested if requested.endswith(".toml") else f"{requested}.toml"
    if Path(filename).name != filename:
        raise TicketError("template name must be a simple filename")

    root = paths.templates_dir(rohrpost_dir).resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        raise TicketError("template path must stay under .rohrpost/templates")
    if not path.is_file():
        raise TicketError(f"no such template: {name}")

    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise TicketError(f"invalid template {name!r}: {exc}") from exc
    except OSError as exc:
        raise TicketError(f"cannot read template {name!r}: {exc}") from exc

    values = {key: value for key, value in raw.items() if key in _TEMPLATE_FIELDS}
    for section_name in ("defaults", "fields", "ticket"):
        section = raw.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise TicketError(f"template section [{section_name}] must be a table")
        values.update(section)

    unknown = sorted(set(values) - _TEMPLATE_FIELDS)
    if unknown:
        raise TicketError(f"unknown template field(s): {', '.join(unknown)}")
    return _normalise_template_values(values)


def _normalise_template_values(values: dict[str, object]) -> dict[str, object]:
    """Validate and normalise values read from a template file."""
    result = dict(values)
    if "priority" in result and (
        isinstance(result["priority"], bool) or not isinstance(result["priority"], int)
    ):
        raise TicketError("template priority must be an integer")
    for field in ("labels", "blocked_by"):
        if field not in result:
            continue
        value = result[field]
        items = value if isinstance(value, list) else [value]
        if not all(isinstance(item, str) and item.strip() for item in items):
            raise TicketError(f"template {field} must contain non-empty strings")
        cleaned = [item.strip() for item in items]
        result[field] = (
            [_normalise_structural(item) for item in cleaned]
            if field == "blocked_by"
            else cleaned
        )
    for field in ("title", "type", "status", "assignee", "body"):
        if field in result and not isinstance(result[field], str):
            raise TicketError(f"template {field} must be a string")
    if "parent" in result:
        parent = result["parent"]
        if not isinstance(parent, str):
            raise TicketError("template parent must be a ticket id")
        result["parent"] = _normalise_structural(parent)
    return result


# ---------------------------------------------------------------------------
# Mutation helpers.
# ---------------------------------------------------------------------------
def _append(rohrpost_dir: Path, event: Event) -> None:
    store.append_event(rohrpost_dir, event)


def _resolve(by_id: dict[str, Ticket], ticket_ref: str) -> Ticket:
    """Look up a ticket by bare id (rendered or bare input), raising :class:`TicketNotFoundError`."""
    tid = _normalise_structural(ticket_ref)
    ticket = by_id.get(tid)
    if ticket is None:
        raise TicketNotFoundError(f"no such ticket: {ticket_ref}")
    return ticket


def _build_event(
    *,
    ticket: str,
    op: Op,
    actor: str,
    now: Clock,
    ulid: UlidFactory,
    set_payload: dict[str, object] | None = None,
    text: str | None = None,
    remote: str | None = None,
    ref: str | None = None,
    reason: str | None = None,
) -> Event:
    return Event(
        id=ulid(),
        ts=now(),
        ticket=ticket,
        op=op,
        actor=actor,
        set=set_payload,
        text=text,
        remote=remote,
        ref=ref,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class InitResult:
    """Outcome of ``rp init``: what was created and where."""

    rohrpost_dir: Path
    prefix: str
    created_config: bool
    updated_gitattributes: bool
    updated_gitignore: bool


def propose_prefix(directory: Path) -> str:
    """Derive a 2-5 letter uppercase prefix from a directory name (spec §5.1).

    Falls back to ``RP`` when the name yields no usable letters.
    """
    letters = [c for c in directory.name.upper() if c.isalpha()]
    candidate = "".join(letters)[:5]
    if len(candidate) < 2:
        return "RP"
    return candidate


def init_repo(
    target_dir: Path | None = None,
    *,
    prefix: str | None = None,
) -> InitResult:
    """Scaffold ``.rohrpost/`` and the committed git housekeeping files.

    Idempotent: re-running fills in anything missing without clobbering an
    existing ``config.toml``. The scaffold lands at the git root when inside a
    repo, otherwise at ``target_dir`` (default cwd).
    """
    base = (target_dir or Path.cwd()).resolve()
    git_root = paths.find_git_root(base)
    repo_root = git_root or base
    rohrpost_dir = repo_root / paths.ROHRPOST_DIR_NAME

    paths.ensure_layout(rohrpost_dir)

    cfg_path = paths.config_path(rohrpost_dir)
    if cfg_path.is_file():
        created_config = False
        config = load_config(rohrpost_dir)
    else:
        chosen = validate_prefix(prefix) if prefix else propose_prefix(repo_root)
        cfg_path.write_text(render_config_toml(chosen), encoding="utf-8")
        created_config = True
        config = load_config(rohrpost_dir)

    updated_gitattributes = paths.write_gitattributes(repo_root)
    updated_gitignore = paths.write_gitignore(repo_root)

    return InitResult(
        rohrpost_dir=rohrpost_dir,
        prefix=config.prefix,
        created_config=created_config,
        updated_gitattributes=updated_gitattributes,
        updated_gitignore=updated_gitignore,
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
def _validate_new_ticket(title: str, type: str, priority: int) -> None:
    if not title.strip():
        raise TicketError("title must be non-empty")
    if type not in TYPES:
        raise TicketError(f"type must be one of {sorted(TYPES)}, got {type!r}")
    if not 0 <= priority <= 4:
        raise TicketError(f"priority must be 0..4, got {priority}")


def _create_payload(
    title: str,
    *,
    type: str,
    priority: int,
    parent: str | None,
    labels: Sequence[str],
    blocked_by: Sequence[str],
    assignee: str | None,
    body: str | None,
) -> dict[str, object]:
    """Build the ``create`` event payload. Created tickets always start ``open`` (§5.4)."""
    payload: dict[str, object] = {
        "title": title.strip(),
        "type": type,
        "status": "open",
        "priority": priority,
    }
    if parent is not None:
        payload["parent"] = _normalise_structural(parent)
    if labels:
        payload["labels+"] = sorted(set(labels))
    if blocked_by:
        payload["blocked_by+"] = sorted({_normalise_structural(b) for b in blocked_by})
    if assignee:
        payload["assignee"] = assignee
    if body is not None and body.strip():
        payload["body"] = body
    return payload


def create_ticket(
    rohrpost_dir: Path,
    title: str,
    *,
    type: str = DEFAULT_TYPE,
    priority: int = DEFAULT_PRIORITY,
    parent: str | None = None,
    labels: Sequence[str] = (),
    blocked_by: Sequence[str] = (),
    assignee: str | None = None,
    body: str | None = None,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Create a ticket (append a ``create`` event) and return the folded result."""
    _validate_new_ticket(title, type, priority)
    payload = _create_payload(
        title,
        type=type,
        priority=priority,
        parent=parent,
        labels=labels,
        blocked_by=blocked_by,
        assignee=assignee,
        body=body,
    )
    tid = _new_id(rohrpost_dir)
    event = _build_event(
        ticket=tid, op="create", actor=actor, now=now, ulid=ulid, set_payload=payload
    )
    _append(rohrpost_dir, event)
    return WriteResult(_require_after(rohrpost_dir, tid), wrote=True)


def _new_id(rohrpost_dir: Path) -> str:
    """Allocate a ticket id not already present in the log (retries on the rare collision)."""
    from rohrpost.ids import new_ticket_id

    existing = set(load_tickets(rohrpost_dir))
    for _ in range(8):
        candidate = new_ticket_id()
        if candidate not in existing:
            return candidate
    raise StoreError(
        "could not allocate a non-colliding ticket id after 8 tries"
    )  # pragma: no cover


def _require_after(rohrpost_dir: Path, tid: str) -> Ticket:
    by_id = load_tickets(rohrpost_dir)
    ticket = by_id.get(tid)
    if ticket is None:  # pragma: no cover - append+fold should always produce it
        raise StoreError(f"ticket {tid} did not appear after its event was appended")
    return ticket


# ---------------------------------------------------------------------------
# set / claim / close / drop / comment / link
# ---------------------------------------------------------------------------
def set_fields(
    rohrpost_dir: Path,
    ticket_ref: str,
    assignments: Sequence[Assignment],
    *,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Apply ``field=value`` assignments as one ``set`` event (idempotent).

    Returns the (possibly unchanged) ticket and whether an event was appended.
    When every assignment is already satisfied by the current state, no event is
    appended and the log stays clean. Close reasons are not carried here — use
    :func:`close`/:func:`drop` for that.
    """
    by_id = load_tickets(rohrpost_dir)
    ticket = _resolve(by_id, ticket_ref)
    effective = _effective_assignments(ticket, assignments)
    if not effective:
        return WriteResult(ticket, wrote=False)

    _validate_set_assignments(effective)
    payload = _assignments_to_payload(effective)

    event = _build_event(
        ticket=ticket.id, op="set", actor=actor, now=now, ulid=ulid, set_payload=payload
    )
    _append(rohrpost_dir, event)
    return WriteResult(_require_after(rohrpost_dir, ticket.id), wrote=True)


def _as_str_list(value: object) -> list[str]:
    """Narrow an :class:`Assignment` value to ``list[str]`` (set-add/remove payloads)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _effective_assignments(ticket: Ticket, assignments: Sequence[Assignment]) -> list[Assignment]:
    """Filter out assignments that would not change the current ticket state."""
    out: list[Assignment] = []
    for a in assignments:
        effective = _effective_one(ticket, a)
        if effective is not None:
            out.append(effective)
    return out


def _effective_one(ticket: Ticket, a: Assignment) -> Assignment | None:
    """Return the effective form of one assignment, or ``None`` if it is a no-op."""
    if a.op == "set":
        return a if _current_scalar(ticket, a.field) != a.value else None
    current = set(getattr(ticket, a.field))
    values = _as_str_list(a.value)
    if a.op == "add":
        changed = [v for v in values if v not in current]
        return Assignment("add", a.field, changed) if changed else None
    changed = [v for v in values if v in current]  # remove
    return Assignment("remove", a.field, changed) if changed else None


def _current_scalar(ticket: Ticket, field: str) -> object:
    if field == "priority":
        return ticket.priority
    return getattr(ticket, field)


def _validate_set_assignments(assignments: Sequence[Assignment]) -> None:
    for a in assignments:
        if a.field == "status" and a.value not in STATUSES:
            raise TicketError(f"status must be one of {sorted(STATUSES)}, got {a.value!r}")
        if a.field == "type" and a.value not in TYPES:
            raise TicketError(f"type must be one of {sorted(TYPES)}, got {a.value!r}")
        if a.field == "priority" and (not isinstance(a.value, int) or not (0 <= a.value <= 4)):
            raise TicketError(f"priority must be 0..4, got {a.value!r}")


def _assignments_to_payload(assignments: Sequence[Assignment]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for a in assignments:
        if a.op == "set":
            payload[a.field] = a.value
        elif a.op == "add":
            payload[f"{a.field}+"] = a.value
        else:
            payload[f"{a.field}-"] = a.value
    return payload


def claim(
    rohrpost_dir: Path,
    ticket_ref: str,
    *,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Mark a ticket ``in_progress`` and stamp the runner as assignee (idempotent)."""
    assignments = [
        Assignment("set", "status", "in_progress"),
        Assignment("set", "assignee", actor),
    ]
    return set_fields(rohrpost_dir, ticket_ref, assignments, actor=actor, now=now, ulid=ulid)


def _terminate(
    rohrpost_dir: Path,
    ticket_ref: str,
    status: str,
    *,
    reason: str | None,
    actor: str,
    now: Clock,
    ulid: UlidFactory,
) -> WriteResult:
    by_id = load_tickets(rohrpost_dir)
    ticket = _resolve(by_id, ticket_ref)
    if ticket.status == status:
        return WriteResult(ticket, wrote=False)

    event = _build_event(
        ticket=ticket.id,
        op="set",
        actor=actor,
        now=now,
        ulid=ulid,
        set_payload={"status": status},
        reason=(reason.strip() if reason and reason.strip() else None),
    )
    _append(rohrpost_dir, event)
    return WriteResult(_require_after(rohrpost_dir, ticket.id), wrote=True)


def close(
    rohrpost_dir: Path,
    ticket_ref: str,
    *,
    reason: str | None = None,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Set status to ``done`` with an optional close reason (idempotent)."""
    return _terminate(
        rohrpost_dir, ticket_ref, "done", reason=reason, actor=actor, now=now, ulid=ulid
    )


def drop(
    rohrpost_dir: Path,
    ticket_ref: str,
    *,
    reason: str | None = None,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Set status to ``dropped`` with an optional reason (idempotent)."""
    return _terminate(
        rohrpost_dir, ticket_ref, "dropped", reason=reason, actor=actor, now=now, ulid=ulid
    )


def add_comment(
    rohrpost_dir: Path,
    ticket_ref: str,
    text: str,
    *,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Append a local note (``comment`` event) to a ticket."""
    if not text.strip():
        raise TicketError("comment text must be non-empty")
    by_id = load_tickets(rohrpost_dir)
    ticket = _resolve(by_id, ticket_ref)
    event = _build_event(
        ticket=ticket.id, op="comment", actor=actor, now=now, ulid=ulid, text=text.strip()
    )
    _append(rohrpost_dir, event)
    return WriteResult(_require_after(rohrpost_dir, ticket.id), wrote=True)


def link_remote(
    rohrpost_dir: Path,
    ticket_ref: str,
    remote: str,
    ref: str,
    *,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Bind a ticket to a remote tracker item (``link`` event)."""
    if not remote.strip() or not ref.strip():
        raise TicketError("remote and ref must be non-empty")
    by_id = load_tickets(rohrpost_dir)
    ticket = _resolve(by_id, ticket_ref)
    remote = remote.strip()
    ref = ref.strip()
    if ticket.remotes.get(remote) == ref:
        return WriteResult(ticket, wrote=False)
    event = _build_event(
        ticket=ticket.id,
        op="link",
        actor=actor,
        now=now,
        ulid=ulid,
        remote=remote,
        ref=ref,
    )
    _append(rohrpost_dir, event)
    return WriteResult(_require_after(rohrpost_dir, ticket.id), wrote=True)


def unlink_remote(
    rohrpost_dir: Path,
    ticket_ref: str,
    remote: str,
    *,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Remove a ticket's binding to a remote tracker (``unlink`` event)."""
    remote = remote.strip()
    if not remote:
        raise TicketError("remote must be non-empty")
    by_id = load_tickets(rohrpost_dir)
    ticket = _resolve(by_id, ticket_ref)
    if remote not in ticket.remotes:
        return WriteResult(ticket, wrote=False)
    event = _build_event(
        ticket=ticket.id,
        op="unlink",
        actor=actor,
        now=now,
        ulid=ulid,
        remote=remote,
    )
    _append(rohrpost_dir, event)
    return WriteResult(_require_after(rohrpost_dir, ticket.id), wrote=True)


# ---------------------------------------------------------------------------
# Read-side operations.
# ---------------------------------------------------------------------------
def show_ticket(rohrpost_dir: Path, ticket_ref: str) -> Ticket:
    """Return the folded ticket (raises :class:`TicketNotFoundError`)."""
    by_id = load_tickets(rohrpost_dir)
    return _resolve(by_id, ticket_ref)


def list_tickets(
    rohrpost_dir: Path,
    *,
    status: str | None = None,
    label: str | None = None,
    parent: str | None = None,
    type: str | None = None,
    ready: bool = False,
) -> list[Ticket]:
    """Query folded tickets with optional filters. ``ready`` selects actionable work."""
    by_id = load_tickets(rohrpost_dir)
    parent_bare = _normalise_structural(parent) if parent else None
    predicates = _list_predicates(status, label, parent_bare, type, by_id)
    out = [t for t in by_id.values() if all(p(t) for p in predicates)]
    if ready:
        out = [t for t in out if is_ready(t, by_id)]
    out.sort(key=lambda t: (t.priority, t.created))
    return out


def _list_predicates(
    status: str | None,
    label: str | None,
    parent_bare: str | None,
    type: str | None,
    by_id: dict[str, Ticket],
) -> list[Callable[[Ticket], bool]]:
    """Build the active filter predicates for :func:`list_tickets`."""
    preds: list[Callable[[Ticket], bool]] = []
    if status is not None:
        preds.append(lambda t: derive_status(t, by_id) == status)
    if label is not None:
        preds.append(lambda t: label in t.labels)
    if parent_bare is not None:
        preds.append(lambda t: t.parent == parent_bare)
    if type is not None:
        preds.append(lambda t: t.type == type)
    return preds


def ready_tickets(rohrpost_dir: Path, *, limit: int | None = None) -> list[Ticket]:
    """The actionable work queue (spec §10): ``open``, unblocked, non-epic, by priority."""
    tickets = list_tickets(rohrpost_dir, ready=True)
    if limit is not None and limit >= 0:
        tickets = tickets[:limit]
    return tickets


def list_conflicts(rohrpost_dir: Path) -> list[Ticket]:
    """Tickets flagged with a ``conflict:<remote>`` label by sync (spec §8.2)."""
    by_id = load_tickets(rohrpost_dir)
    out = [t for t in by_id.values() if any(lab.startswith("conflict:") for lab in t.labels)]
    out.sort(key=lambda t: (t.priority, t.created))
    return out


def resolve_conflict(
    rohrpost_dir: Path,
    ticket_ref: str,
    take: str,
    *,
    actor: str,
    now: Clock = now_ts,
    ulid: UlidFactory = new_ulid,
) -> WriteResult:
    """Clear a sync conflict: drop ``conflict:*`` labels, reopen, record the choice.

    ``take`` is ``local`` or ``remote``. The chosen field values are whatever the
    operator has set (edit the fields, then resolve); this clears the flag and
    reopens the ticket so the next sync round re-merges cleanly.
    """
    if take not in ("local", "remote"):
        raise TicketError("resolve --take must be 'local' or 'remote'")
    by_id = load_tickets(rohrpost_dir)
    ticket = _resolve(by_id, ticket_ref)
    conflict_labels = [lab for lab in ticket.labels if lab.startswith("conflict:")]
    if not conflict_labels:
        return WriteResult(ticket, wrote=False)

    assignments = [Assignment("remove", "labels", conflict_labels)]
    result = set_fields(rohrpost_dir, ticket.id, assignments, actor=actor, now=now, ulid=ulid)
    add_comment(
        rohrpost_dir,
        ticket.id,
        f"sync conflict resolved taking {take}",
        actor=actor,
        now=now,
        ulid=ulid,
    )
    # Reopen unless the operator has since moved it to a terminal state.
    reopened = set_fields(
        rohrpost_dir,
        ticket.id,
        [Assignment("set", "status", "open")],
        actor=actor,
        now=now,
        ulid=ulid,
    )
    final = reopened.ticket if reopened.ticket.status == "open" else result.ticket
    return WriteResult(final, wrote=True)


@dataclass(frozen=True, slots=True)
class Tree:
    """An epic and its direct children (one level of nesting, spec §5.5)."""

    root: Ticket
    children: list[Ticket]


def tree(rohrpost_dir: Path, ticket_ref: str) -> Tree:
    """Return a ticket and its direct children (the epic view, spec §5.5)."""
    by_id = load_tickets(rohrpost_dir)
    root = _resolve(by_id, ticket_ref)
    children = sorted(
        (c for c in by_id.values() if c.parent == root.id),
        key=lambda t: (t.priority, t.created),
    )
    return Tree(root=root, children=children)


def event_log(rohrpost_dir: Path, ticket_ref: str | None = None) -> list[Event]:
    """Raw event history, optionally filtered to one ticket (sorted by ts, id)."""
    events = store.read_events(rohrpost_dir)
    if ticket_ref is not None:
        tid = _normalise_structural(ticket_ref)
        events = [
            e
            for e in events
            if e.op != "synced" and _normalise_structural(e.ticket) == tid
        ]
    events.sort(key=lambda e: (e.ts, e.id))
    return events


def comments(rohrpost_dir: Path, ticket_ref: str) -> list[Comment]:
    """All notes on a ticket, oldest first."""
    ticket = show_ticket(rohrpost_dir, ticket_ref)
    return list(ticket.comments)


# ---------------------------------------------------------------------------
# Config convenience for the CLI.
# ---------------------------------------------------------------------------
def load_repo_config(rohrpost_dir: Path) -> Config:
    """Load config, falling back to defaults if unreadable for any reason."""
    try:
        return load_config(rohrpost_dir)
    except StoreError:
        return default_config()


def snapshot_mapping(
    ticket: Ticket, *, prefix: str, include_fieldts: bool = True
) -> dict[str, object]:
    """Public alias of :func:`fold.ticket_to_mapping` with a prefix applied."""
    return ticket_to_mapping(ticket, prefix=prefix, include_fieldts=include_fieldts)


def snapshot_comment(comment: Comment) -> dict[str, str]:
    """Public alias of :func:`fold.comment_to_mapping`."""
    return comment_to_mapping(comment)


def load_tickets_map(rohrpost_dir: Path) -> dict[str, Ticket]:
    """Public alias of :func:`fold.load_tickets` returning the ``{bare_id: Ticket}`` map."""
    return load_tickets(rohrpost_dir)


__all__ = [
    "Assignment",
    "Comment",
    "InitResult",
    "Ticket",
    "Tree",
    "WriteResult",
    "add_comment",
    "claim",
    "close",
    "comments",
    "create_ticket",
    "drop",
    "event_log",
    "init_repo",
    "link_remote",
    "load_template",
    "list_tickets",
    "load_repo_config",
    "load_tickets_map",
    "parse_assignment",
    "propose_prefix",
    "ready_tickets",
    "set_fields",
    "show_ticket",
    "snapshot_comment",
    "snapshot_mapping",
    "tree",
    "unlink_remote",
]
