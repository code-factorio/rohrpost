"""The sync round: bidirectional three-way merge per linked ticket (spec §8.4).

For each ticket bound to a remote, sync pulls the live remote item, three-way
merges it against the shadow (merge base) and the folded ticket, applies the
remote-won fields locally (``set`` events, actor ``remote/<name>``), flags
conflicts per policy, pushes the local-won fields to the remote, and rewrites
the shadow from the post-sync remote state.

**First-cut scope:** scalar fields only — ``title``, ``body``, ``status``. The
``body`` gets a genuine three-way text merge (§8.3); everything else is
per-field LWW. Set fields (``labels``) need a set-wise three-way merge and are a
deliberate follow-on. A crash between applying remote-won locally and rewriting
the shadow leaves a stale shadow (a redundant, idempotent merge next round)
rather than a lost update (§8.4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rohrpost import shadow, store
from rohrpost.config import Config
from rohrpost.fold import load_tickets
from rohrpost.merge import FieldConflict, MergeResult, Policy, three_way
from rohrpost.providers import Provider
from rohrpost.util import Clock, now_ts

#: Local fields this first-cut sync round merges. Set fields are a follow-on.
SYNCED_FIELDS: tuple[str, ...] = ("title", "body", "status")


@dataclass(frozen=True, slots=True)
class TicketSync:
    """Per-ticket outcome of a sync round."""

    ticket: str
    ref: str
    pulled: int
    pushed: int
    conflicts: list[str]


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

    for tid, ticket in linked:
        merged, live, base = _merge_ticket(ctx, ticket, provider, remote_config, policy)
        pulled, pushed = _apply_merge(
            ctx, tid, ticket.remotes[remote], merged, live, base, provider
        )
        report.tickets.append(
            TicketSync(
                tid, ticket.remotes[remote], pulled, pushed, [c.field for c in merged.conflicts]
            )
        )
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


def _merge_ticket(
    ctx: _SyncCtx,
    ticket: object,
    provider: Provider,
    remote_config: dict[str, object],
    policy: Policy,
) -> tuple[MergeResult, dict[str, object], dict[str, object]]:
    """Fetch the live remote, load the shadow, and three-way merge the ticket."""
    ref = getattr(ticket, "remotes", {}).get(ctx.remote, "")
    mapped = _mapped_fields(remote_config)
    live = {k: v for k, v in provider.fetch(ref).items() if k in mapped}
    base = {
        k: v
        for k, v in (shadow.read_shadow(ctx.repo, ctx.remote, ref) or {}).items()
        if k in mapped
    }
    local = {k: v for k, v in _ticket_fields(ticket).items() if k in mapped and v not in (None, "")}
    return three_way(base, local, live, policy=policy), live, base


def _apply_merge(
    ctx: _SyncCtx,
    tid: str,
    ref: str,
    merged: object,
    live: dict[str, object],
    base: dict[str, object],
    provider: Provider,
) -> tuple[int, int]:
    """Apply remote-won / conflicts / local-won, push, rewrite shadow. Returns (pulled, pushed)."""
    remote_won: dict[str, object] = getattr(merged, "remote_won", {})
    local_won: dict[str, object] = getattr(merged, "local_won", {})
    conflicts = getattr(merged, "conflicts", [])
    pulled = len(remote_won)
    pushed = 0
    if ctx.dry_run:
        return pulled, len(local_won)
    if remote_won:
        _append_set(ctx.repo, tid, dict(remote_won), ctx.who, ctx.now, ctx.ulid, ctx.event_cls)
    if conflicts:
        _flag_conflict(
            ctx.repo, tid, ctx.remote, conflicts, ctx.who, ctx.now, ctx.ulid, ctx.event_cls
        )
    if local_won:
        live = provider.push(ref, dict(local_won))
        pushed = len(local_won)
    shadow.write_shadow(ctx.repo, ctx.remote, ref, _next_shadow(base, live, remote_won))
    _append_synced(ctx.repo, tid, ctx.remote, ctx.who, ctx.now, ctx.ulid, ctx.event_cls)
    return pulled, pushed


def _ticket_fields(ticket: object) -> dict[str, object]:
    """Extract the synced scalar fields from a folded ticket."""
    return {name: getattr(ticket, name) for name in SYNCED_FIELDS}


def _next_shadow(
    base: dict[str, object], live: dict[str, object], remote_won: dict[str, object]
) -> dict[str, object]:
    """Post-sync merge base: carried-over base values overlaid with live + remote-won.

    After applying remote-won locally and pushing local-won, the remote's
    effective state is ``live`` (updated by the push). Carry base values for any
    field the remote did not return, then overlay remote-won so fields the remote
    won this round record the value both sides now agree on.
    """
    return {**base, **live, **remote_won}


def _append_set(
    rohrpost_dir: Path,
    tid: str,
    fields: dict[str, object],
    who: str,
    now: Clock,
    ulid: Callable[[], str],
    event_cls: type,
) -> None:
    """Apply remote-won fields locally as one ``set`` event (actor ``remote/<name>``)."""
    store.append_event(
        rohrpost_dir,
        event_cls(
            id=ulid(),
            ts=now(),
            ticket=tid,
            op="set",
            actor=who,
            set=fields,
        ),
    )


def _flag_conflict(
    rohrpost_dir: Path,
    tid: str,
    remote: str,
    conflicts: list[FieldConflict],
    who: str,
    now: Clock,
    ulid: Callable[[], str],
    event_cls: type,
) -> None:
    """Move the ticket to review, tag it, and comment both values (§8.2 flag)."""
    store.append_event(
        rohrpost_dir,
        event_cls(
            id=ulid(),
            ts=now(),
            ticket=tid,
            op="set",
            actor=who,
            set={"status": "review", "labels+": [f"conflict:{remote}"]},
        ),
    )
    detail = "; ".join(f"{c.field}: local={c.local!r} remote={c.remote!r}" for c in conflicts)
    store.append_event(
        rohrpost_dir,
        event_cls(
            id=ulid(),
            ts=now(),
            ticket=tid,
            op="comment",
            actor=who,
            text=f"sync conflict with {remote} — {detail}",
        ),
    )


def _append_synced(
    rohrpost_dir: Path,
    tid: str,
    remote: str,
    who: str,
    now: Clock,
    ulid: Callable[[], str],
    event_cls: type,
) -> None:
    """Record that a ticket completed a sync round with ``remote`` (§8.4 step 6)."""
    store.append_event(
        rohrpost_dir,
        event_cls(
            id=ulid(),
            ts=now(),
            ticket=tid,
            op="synced",
            actor=who,
            remote=remote,
            at=now(),
        ),
    )


__all__ = ["SYNCED_FIELDS", "SyncReport", "TicketSync", "sync_round"]
