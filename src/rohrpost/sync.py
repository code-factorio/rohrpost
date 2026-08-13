"""The sync round: bidirectional three-way merge per linked ticket (spec §8.4).

For each ticket bound to a remote, sync pulls the live remote item, three-way
merges it against the shadow (merge base) and the folded ticket, applies the
remote-won fields locally (``set`` events, actor ``remote/<name>``), flags
conflicts per policy, pushes the local-won fields to the remote, and rewrites
the shadow from the post-sync remote state.

``body`` gets a genuine three-way text merge (§8.3), while ``labels`` uses
set-wise three-way merge semantics. A crash between applying remote-won locally
and rewriting the shadow leaves a stale shadow (a redundant, idempotent merge
next round) rather than a lost update (§8.4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rohrpost import shadow, store
from rohrpost.config import Config
from rohrpost.events import SYNC_TICKET
from rohrpost.exceptions import RemoteItemNotFoundError
from rohrpost.fold import Ticket, fold_all, load_tickets
from rohrpost.merge import FieldConflict, MergeResult, Policy, three_way
from rohrpost.providers import Provider
from rohrpost.util import Clock, now_ts

#: Ticket fields supported by the provider mapping and merge engine.
SYNCED_FIELDS: tuple[str, ...] = ("title", "body", "status", "priority", "labels")


@dataclass(frozen=True, slots=True)
class TicketSync:
    """Per-ticket outcome of a sync round."""

    ticket: str
    ref: str
    pulled: int
    pushed: int
    conflicts: list[str]
    changed: bool = False


@dataclass(frozen=True, slots=True)
class SyncReport:
    """Aggregate outcome of a sync round over one remote."""

    remote: str
    tickets: list[TicketSync] = field(default_factory=list)

    @property
    def pulled(self) -> int:
        return sum(t.pulled for t in self.tickets)

    @property
    def pushed(self) -> int:
        return sum(t.pushed for t in self.tickets)

    @property
    def conflicts(self) -> int:
        return sum(len(t.conflicts) for t in self.tickets)


def _mapped_fields(remote_config: dict[str, object]) -> set[str]:
    """The local field names this remote maps (keys of its ``[fields]`` table)."""
    fields = remote_config.get("fields", {})
    return set(fields.keys()) if isinstance(fields, dict) else set()


def _config_for(remote: str, config: Config) -> dict[str, object]:
    raw = config.remotes.get(remote)
    if raw is None:
        from rohrpost.exceptions import ConfigError

        raise ConfigError(f"no [remotes.{remote}] configured")
    return raw


def _policy_of(remote_config: dict[str, object]) -> Policy:
    p = remote_config.get("policy", "flag")
    if p == "local":
        return "local"
    if p == "remote":
        return "remote"
    return "flag"


def sync_round(
    rohrpost_dir: Path,
    remote: str,
    provider: Provider,
    config: Config,
    *,
    dry_run: bool = False,
    actor: str | None = None,
    now: Clock = now_ts,
    ulid: Callable[[], str] | None = None,
) -> SyncReport:
    """Run one sync round against ``remote``. Returns a :class:`SyncReport`."""
    from rohrpost.events import Event
    from rohrpost.ids import new_ulid

    remote_config = _config_for(remote, config)
    policy = _policy_of(remote_config)
    who = actor or f"remote/{remote}"
    mk_ulid = ulid or new_ulid
    report = SyncReport(remote=remote)

    by_id = load_tickets(rohrpost_dir)
    linked = [(tid, t) for tid, t in by_id.items() if remote in t.remotes]
    ctx = _SyncCtx(rohrpost_dir, remote, who, now, mk_ulid, Event, dry_run)
    mapped = _mapped_fields(remote_config)

    for tid, ticket in linked:
        report.tickets.append(
            _sync_ticket(ctx, tid, ticket, provider, remote_config, policy, mapped)
        )
    if not dry_run and any(ticket.changed for ticket in report.tickets):
        _append_synced(rohrpost_dir, remote, who, now, mk_ulid, Event)
    return report


@dataclass(frozen=True, slots=True)
class _SyncCtx:
    """Bundle of the per-round immutable context handed to the per-ticket helpers."""

    repo: Path
    remote: str
    who: str
    now: Clock
    ulid: Callable[[], str]
    event_cls: type
    dry_run: bool


def _sync_ticket(
    ctx: _SyncCtx,
    tid: str,
    ticket: Ticket,
    provider: Provider,
    remote_config: dict[str, object],
    policy: Policy,
    mapped: set[str],
) -> TicketSync:
    """Fetch, merge, and apply one linked ticket, including deletion handling."""
    ref = ticket.remotes[ctx.remote]
    try:
        merged, live, base, had_shadow = _merge_ticket(ctx, ticket, provider, remote_config, policy)
    except RemoteItemNotFoundError:
        changed = _flag_deleted(ctx, tid, ref, ticket)
        return TicketSync(tid, ref, 0, 0, ["remote"], changed)
    pulled, pushed, changed = _apply_merge(
        ctx, tid, ref, ticket, merged, live, base, had_shadow, provider, mapped
    )
    return TicketSync(
        tid,
        ref,
        pulled,
        pushed,
        [conflict.field for conflict in merged.conflicts],
        changed,
    )


def _merge_ticket(
    ctx: _SyncCtx,
    ticket: Ticket,
    provider: Provider,
    remote_config: dict[str, object],
    policy: Policy,
) -> tuple[MergeResult, dict[str, object], dict[str, object], bool]:
    """Fetch the live remote, load the shadow, and three-way merge the ticket."""
    ref = getattr(ticket, "remotes", {}).get(ctx.remote, "")
    mapped = _mapped_fields(remote_config)
    live = {k: v for k, v in provider.fetch(ref).items() if k in mapped}
    stored = shadow.read_shadow(ctx.repo, ctx.remote, ref)
    base = {k: v for k, v in (stored or {}).items() if k in mapped}
    local = {k: v for k, v in _ticket_fields(ticket).items() if k in mapped}
    if stored is None:
        # A missing/corrupt shadow is not enough information to choose a winner.
        # Establish a base without touching either side; the next genuine edit is
        # then classified normally.
        return MergeResult(), live, base, False
    return three_way(base, local, live, policy=policy), live, base, True


def _apply_merge(
    ctx: _SyncCtx,
    tid: str,
    ref: str,
    ticket: Ticket,
    merged: MergeResult,
    live: dict[str, object],
    base: dict[str, object],
    had_shadow: bool,
    provider: Provider,
    mapped: set[str],
) -> tuple[int, int, bool]:
    """Apply remote-won / conflicts / local-won, push, rewrite shadow. Returns (pulled, pushed)."""
    if _should_defer(ctx, tid, ticket, mapped, had_shadow, live, base):
        return 0, 0, False
    pulled = len(merged.remote_won)
    pushed = len(merged.local_won)
    if ctx.dry_run:
        return pulled, pushed, _has_planned_change(merged, live, base, had_shadow)
    changed = _apply_local_merge(ctx, tid, ticket, merged)
    live, pushed_change = _push_merge(provider, ref, live, merged.local_won)
    shadow_change = _update_shadow(ctx, ref, live, base, had_shadow)
    changed = changed or pushed_change or shadow_change
    return pulled, pushed, changed


def _should_defer(
    ctx: _SyncCtx,
    tid: str,
    ticket: Ticket,
    mapped: set[str],
    had_shadow: bool,
    live: dict[str, object],
    base: dict[str, object],
) -> bool:
    completed_conflict = had_shadow and _is_flagged(ticket, ctx.remote) and live == base
    return completed_conflict or _changed_during_fetch(ctx.repo, tid, ticket, mapped)


def _has_planned_change(
    merged: MergeResult,
    live: dict[str, object],
    base: dict[str, object],
    had_shadow: bool,
) -> bool:
    return bool(
        merged.remote_won
        or merged.local_won
        or merged.conflicts
        or merged.resolved
        or not had_shadow
        or live != base
    )


def _apply_local_merge(ctx: _SyncCtx, tid: str, ticket: Ticket, merged: MergeResult) -> bool:
    if merged.conflicts:
        changed = _flag_conflict(ctx, tid, ticket, merged.conflicts, merged.remote_won)
    else:
        changed = bool(merged.remote_won) and _append_set(ctx, tid, ticket, merged.remote_won)
    for conflict in merged.resolved:
        changed = _record_resolution(ctx, tid, ticket, conflict, merged.local_won) or changed
    return changed


def _push_merge(
    provider: Provider,
    ref: str,
    live: dict[str, object],
    local_won: dict[str, object],
) -> tuple[dict[str, object], bool]:
    if not local_won:
        return live, False
    return provider.push(ref, dict(local_won)), True


def _update_shadow(
    ctx: _SyncCtx,
    ref: str,
    live: dict[str, object],
    base: dict[str, object],
    had_shadow: bool,
) -> bool:
    next_shadow = dict(live)
    if had_shadow and next_shadow == base:
        return False
    shadow.write_shadow(ctx.repo, ctx.remote, ref, next_shadow)
    return True


def _ticket_fields(ticket: Ticket) -> dict[str, object]:
    """Extract fields understood by the sync merge engine from a ticket."""
    return {name: getattr(ticket, name) for name in SYNCED_FIELDS}


def _append_set(
    ctx: _SyncCtx,
    tid: str,
    ticket: Ticket,
    fields: dict[str, object],
) -> bool:
    """Apply remote-won fields locally as one provenance-stamped event."""
    payload = _event_payload(ticket, fields)
    if not payload:
        return False
    store.append_event(
        ctx.repo,
        ctx.event_cls(
            id=ctx.ulid(),
            ts=ctx.now(),
            ticket=tid,
            op="set",
            actor=ctx.who,
            set=payload,
        ),
    )
    return True


def _event_payload(ticket: Ticket, fields: dict[str, object]) -> dict[str, object]:
    """Translate merged whole values to the event log's scalar/set operations."""
    payload: dict[str, object] = {}
    for name, value in fields.items():
        if name != "labels":
            if getattr(ticket, name) != value:
                payload[name] = value
            continue
        current = set(ticket.labels)
        target = {str(item) for item in value} if isinstance(value, list) else set()
        added = sorted(target - current)
        removed = sorted(current - target)
        if added:
            payload["labels+"] = added
        if removed:
            payload["labels-"] = removed
    return payload


def _flag_conflict(
    ctx: _SyncCtx,
    tid: str,
    ticket: Ticket,
    conflicts: list[FieldConflict],
    inbound: dict[str, object],
) -> bool:
    """Apply inbound values/markers and the conflict flag atomically."""
    detail = "; ".join(f"{c.field}: local={c.local!r} remote={c.remote!r}" for c in conflicts)
    comment = f"sync conflict with {ctx.remote} — {detail}"
    changed = _append_comment_once(ctx, tid, ticket, comment)
    updates = dict(inbound)
    updates["status"] = "review"
    labels = updates.get("labels", ticket.labels)
    target_labels = (
        {str(item) for item in labels} if isinstance(labels, list) else set(ticket.labels)
    )
    target_labels.add(f"conflict:{ctx.remote}")
    updates["labels"] = sorted(target_labels)
    return _append_set(ctx, tid, ticket, updates) or changed


def _record_resolution(
    ctx: _SyncCtx,
    tid: str,
    ticket: Ticket,
    conflict: FieldConflict,
    local_won: dict[str, object],
) -> bool:
    winner = "local" if conflict.field in local_won else "remote"
    text = (
        f"sync conflict with {ctx.remote} resolved by {winner} policy — "
        f"{conflict.field}: local={conflict.local!r} remote={conflict.remote!r}"
    )
    return _append_comment_once(ctx, tid, ticket, text)


def _append_comment_once(ctx: _SyncCtx, tid: str, ticket: Ticket, text: str) -> bool:
    if any(comment.actor == ctx.who and comment.text == text for comment in ticket.comments):
        return False
    store.append_event(
        ctx.repo,
        ctx.event_cls(
            id=ctx.ulid(),
            ts=ctx.now(),
            ticket=tid,
            op="comment",
            actor=ctx.who,
            text=text,
        ),
    )
    return True


def _flag_deleted(ctx: _SyncCtx, tid: str, ref: str, ticket: Ticket) -> bool:
    conflict = FieldConflict("remote", f"linked ticket {tid}", f"missing item {ref}")
    if ctx.dry_run:
        detail = f"remote: local={conflict.local!r} remote={conflict.remote!r}"
        text = f"sync conflict with {ctx.remote} — {detail}"
        has_comment = any(c.actor == ctx.who and c.text == text for c in ticket.comments)
        return not has_comment or not _is_flagged(ticket, ctx.remote) or ticket.status != "review"
    return _flag_conflict(ctx, tid, ticket, [conflict], {})


def _is_flagged(ticket: Ticket, remote: str) -> bool:
    return f"conflict:{remote}" in ticket.labels


def _changed_during_fetch(repo: Path, tid: str, before: Ticket, mapped: set[str]) -> bool:
    current = fold_all(repo).get(tid)
    if current is None:
        return True
    before_fields = {k: v for k, v in _ticket_fields(before).items() if k in mapped}
    current_fields = {k: v for k, v in _ticket_fields(current).items() if k in mapped}
    return current_fields != before_fields


def _append_synced(
    rohrpost_dir: Path,
    remote: str,
    who: str,
    now: Clock,
    ulid: Callable[[], str],
    event_cls: type,
) -> None:
    """Record that a ticket completed a sync round with ``remote`` (§8.4 step 6)."""
    timestamp = now()
    store.append_event(
        rohrpost_dir,
        event_cls(
            id=ulid(),
            ts=timestamp,
            ticket=SYNC_TICKET,
            op="synced",
            actor=who,
            remote=remote,
            at=timestamp,
        ),
    )


__all__ = ["SYNCED_FIELDS", "SyncReport", "TicketSync", "sync_round"]
