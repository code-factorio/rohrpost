"""The ``rp`` command-line entry point.

Every mutation goes through :mod:`rohrpost.api` — this module is only an
argparse adapter plus output rendering (spec §10). ``--json`` is honoured on
every command and returns machine-readable output; the default is readable
text that respects ``NO_COLOR`` and non-tty streams.

Exit codes: ``0`` success, ``1`` a domain failure (no such ticket, bad status,
…), ``2`` usage error or an unimplemented command (argparse convention).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from rohrpost import api, paths
from rohrpost.config import Config
from rohrpost.exceptions import RohrpostError
from rohrpost.fold import DEFAULT_PRIORITY, derive_status, ticket_to_mapping
from rohrpost.providers import Provider
from rohrpost.util import resolve_actor

#: Subcommands that require a remote provider not yet wired into the CLI.
_UNIMPLEMENTED: tuple[str, ...] = ()

#: Subcommands that mutate the log and accept ``--actor``.
_ACTOR_COMMANDS: frozenset[str] = frozenset(
    {"new", "set", "claim", "close", "drop", "comment", "link", "unlink"}
)


# ---------------------------------------------------------------------------
# Output helpers (NO_COLOR-aware).
# ---------------------------------------------------------------------------
def _use_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR") == "0":
        return False
    return stream.isatty()


def _style(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


_STATUS_COLOR: dict[str, str] = {
    "done": "32",  # green
    "dropped": "90",  # bright black
    "in_progress": "36",  # cyan
    "review": "35",  # magenta
    "waiting": "33",  # yellow
    "ready": "32",  # green
    "open": "0",
}


def _color_status(status: str, enabled: bool) -> str:
    return _style(status, _STATUS_COLOR.get(status, "0"), enabled)


@dataclass(frozen=True, slots=True)
class _Out:
    """Bundle of stdout/stderr + resolved flags so handlers stay terse."""

    json: bool
    color: bool
    prefix: str
    stdout: TextIO
    stderr: TextIO

    def print(self, *args: object) -> None:
        print(*args, file=self.stdout)

    def emit_json(self, obj: object) -> None:
        json.dump(obj, self.stdout, indent=2, ensure_ascii=False)
        print(file=self.stdout)


def _short(ticket: api.Ticket, out: _Out) -> dict[str, object]:
    # The list/ready shape omits fieldts, comments AND the body: the work-queue
    # view must not carry ticket prose into the agent context (decision E7).
    return ticket_to_mapping(
        ticket,
        prefix=out.prefix,
        include_fieldts=False,
        include_comments=False,
        include_body=False,
    )


def _full(ticket: api.Ticket, out: _Out) -> dict[str, object]:
    return ticket_to_mapping(ticket, prefix=out.prefix, include_fieldts=True)


# ---------------------------------------------------------------------------
# argparse wiring.
# ---------------------------------------------------------------------------
def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--actor",
        default=None,
        help="override the event actor (default: user/<git email> or runner from env)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rp",
        description="Rohrpost — a git-native ticket system for agentic coding workflows.",
    )
    parser.add_argument("--version", action="version", version=f"rohrpost {_version()}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # init
    p = sub.add_parser("init", help="scaffold .rohrpost/ in this repository")
    p.add_argument("--prefix", default=None, help="project id prefix (2-5 uppercase letters)")
    _add_json(p)

    # new
    p = sub.add_parser("new", help="create a ticket")
    p.add_argument("title", help="ticket title")
    p.add_argument("--template", default=None, help="load defaults from templates/<name>.toml")
    p.add_argument("--type", default=None, help="task | bug | spike | epic (default: task)")
    p.add_argument("-p", "--priority", type=int, default=None, help="0 highest .. 4 lowest")
    p.add_argument("--label", action="append", default=None, help="label (repeatable)")
    p.add_argument("--blocked-by", action="append", default=None, help="ticket id (repeatable)")
    p.add_argument("--parent", default=None, help="parent epic id")
    p.add_argument("--assignee", default=None, help="assignee actor string")
    p.add_argument("--body", default=None, help="ticket body / description")
    _add_actor(p)
    _add_json(p)

    # ready
    p = sub.add_parser("ready", help="unblocked, actionable work")
    p.add_argument("--limit", type=int, default=None, help="cap the number of results")
    _add_json(p)

    # show
    p = sub.add_parser("show", help="show a ticket")
    p.add_argument("id", help="ticket id (bare or PREFIX-id)")
    p.add_argument(
        "--include",
        default="body",
        help="comma list of extra sections: body,deps,notes,fieldts (default: body)",
    )
    _add_json(p)

    # tree
    p = sub.add_parser("tree", help="an epic and its children")
    p.add_argument("id", help="ticket id")
    _add_json(p)

    # list
    p = sub.add_parser("list", help="query tickets")
    p.add_argument("--status", default=None, help="filter by (possibly derived) status")
    p.add_argument("--label", default=None, help="filter by label")
    p.add_argument("--parent", default=None, help="filter by parent id")
    p.add_argument("--type", default=None, help="filter by type")
    p.add_argument("--match", default=None, help="case-insensitive substring of the title")
    _add_json(p)

    # claim
    p = sub.add_parser("claim", help="mark a ticket in_progress and stamp the actor")
    p.add_argument("id", help="ticket id")
    _add_actor(p)
    _add_json(p)

    # set
    p = sub.add_parser("set", help="update one or more fields (field=value ...)")
    p.add_argument("id", help="ticket id")
    p.add_argument(
        "assignments", nargs="+", metavar="field=value", help="e.g. status=done labels+=auth"
    )
    _add_actor(p)
    _add_json(p)

    # close
    p = sub.add_parser("close", help="set status to done")
    p.add_argument("id", help="ticket id")
    p.add_argument("--reason", default=None, help="close reason (recorded on the event)")
    _add_actor(p)
    _add_json(p)

    # drop
    p = sub.add_parser("drop", help="set status to dropped")
    p.add_argument("id", help="ticket id")
    p.add_argument("--reason", default=None, help="drop reason (recorded on the event)")
    _add_actor(p)
    _add_json(p)

    # comment
    p = sub.add_parser("comment", help="append a local note")
    p.add_argument("id", help="ticket id")
    p.add_argument("text", help="note text")
    _add_actor(p)
    _add_json(p)

    # comments
    p = sub.add_parser("comments", help="show all notes on a ticket")
    p.add_argument("id", help="ticket id")
    _add_json(p)

    # link
    p = sub.add_parser("link", help="bind a ticket to a remote item")
    p.add_argument("id", help="ticket id")
    p.add_argument("remote", help="remote name (e.g. github)")
    p.add_argument("ref", help="remote item reference (e.g. issue number)")
    _add_actor(p)
    _add_json(p)

    # unlink
    p = sub.add_parser("unlink", help="remove a ticket's remote binding")
    p.add_argument("id", help="ticket id")
    p.add_argument("remote", help="remote name (e.g. github)")
    _add_actor(p)
    _add_json(p)

    # log
    p = sub.add_parser("log", help="raw event history")
    p.add_argument("id", nargs="?", default=None, help="optional ticket id to filter to")
    _add_json(p)

    # doctor
    p = sub.add_parser("doctor", help="integrity and config checks")
    _add_json(p)

    # compact
    p = sub.add_parser("compact", help="archive old events and truncate the log (main only)")
    p.add_argument("--force", action="store_true", help="bypass the clean-main-branch guard")
    p.add_argument(
        "--archive-after",
        type=int,
        default=None,
        help=f"days a terminal ticket must sit before archiving (default: {compact_default_days()})",
    )
    _add_json(p)

    # stats (spec §13.1): size distributions + fold timing, computed from the log.
    p = sub.add_parser("stats", help="repository statistics: body/line sizes, fold timing")
    _add_json(p)

    # sync (spec §8): three-way merge with a linked remote
    p = sub.add_parser("sync", help="three-way sync with a remote tracker")
    p.add_argument("remote", nargs="?", default=None, help="remote name (default: the only one)")
    p.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    _add_json(p)

    # conflicts / resolve (spec §8.2)
    p = sub.add_parser("conflicts", help="list tickets flagged by sync")
    _add_json(p)
    p = sub.add_parser("resolve", help="clear a sync conflict")
    p.add_argument("id", help="ticket id")
    p.add_argument("--take", choices=["local", "remote"], required=False)
    _add_actor(p)
    _add_json(p)

    # Anything still in _UNIMPLEMENTED is registered for discovery only.
    for name in _UNIMPLEMENTED:
        sp = sub.add_parser(name, help=f"(not yet implemented) {name}")
        _add_json(sp)

    return parser


def _version() -> str:
    from rohrpost import __version__

    return __version__


# ---------------------------------------------------------------------------
# Command handlers. Each returns an exit code.
# ---------------------------------------------------------------------------
def _repo_dir() -> Path:
    return paths.require_rohrpost_dir()


def _make_out(args: argparse.Namespace) -> _Out:
    repo = _repo_dir()
    config = api.load_repo_config(repo)
    return _Out(
        json=getattr(args, "json", False),
        color=_use_color(sys.stdout),
        prefix=config.prefix,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _actor_of(args: argparse.Namespace) -> str:
    return resolve_actor(explicit=getattr(args, "actor", None))


def cmd_init(args: argparse.Namespace) -> int:
    result = api.init_repo(Path.cwd(), prefix=args.prefix)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "rohrpost_dir": str(result.rohrpost_dir),
                    "prefix": result.prefix,
                    "created_config": result.created_config,
                    "updated_gitattributes": result.updated_gitattributes,
                    "updated_gitignore": result.updated_gitignore,
                }
            )
        )
        return 0
    print(f"Initialised rohrpost at {result.rohrpost_dir} (prefix={result.prefix})")
    if result.created_config:
        print(f"  wrote {paths.CONFIG_FILENAME}")
    if result.updated_gitattributes:
        print("  updated .gitattributes (union-merge rules)")
    if result.updated_gitignore:
        print("  updated .gitignore (snapshot is regenerable)")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    out = _make_out(args)
    repo = _repo_dir()
    defaults = api.load_template(repo, args.template) if args.template else {}
    result = api.create_ticket(
        repo,
        args.title,
        type=args.type if args.type is not None else str(defaults.get("type", "task")),
        priority=(args.priority if args.priority is not None else _template_priority(defaults)),
        labels=args.label if args.label is not None else _template_list(defaults, "labels"),
        blocked_by=(
            args.blocked_by
            if args.blocked_by is not None
            else _template_list(defaults, "blocked_by")
        ),
        parent=args.parent if args.parent is not None else _template_optional(defaults, "parent"),
        assignee=(
            args.assignee if args.assignee is not None else _template_optional(defaults, "assignee")
        ),
        body=args.body if args.body is not None else _template_optional(defaults, "body"),
        actor=_actor_of(args),
    )
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        _print_created(result.ticket, out)
    return 0


def _template_list(defaults: dict[str, object], field: str) -> list[str]:
    value = defaults.get(field, [])
    if not isinstance(value, list):
        raise RohrpostError(f"template {field} must be a list")
    return [str(item) for item in value]


def _template_priority(defaults: dict[str, object]) -> int:
    """Return a validated integer priority from template defaults."""
    value = defaults.get("priority", DEFAULT_PRIORITY)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RohrpostError("template priority must be an integer")
    return value


def _template_optional(defaults: dict[str, object], field: str) -> str | None:
    value = defaults.get(field)
    return str(value) if value is not None else None


def cmd_ready(args: argparse.Namespace) -> int:
    out = _make_out(args)
    repo = _repo_dir()
    by_id = api.load_tickets_map(repo)
    tickets = api.ready_tickets(repo, limit=args.limit)
    if out.json:
        out.emit_json([_short(t, out) for t in tickets])
        return 0
    if not tickets:
        print("No actionable work. The tube is empty.")
        return 0
    for t in tickets:
        _print_summary(t, out, by_id, status_override="ready")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    out = _make_out(args)
    repo = _repo_dir()
    ticket = api.show_ticket(repo, args.id)
    by_id = api.load_tickets_map(repo)
    if out.json:
        out.emit_json(_full(ticket, out))
        return 0
    _print_detail(ticket, out, include=args.include, by_id=by_id)
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    out = _make_out(args)
    repo = _repo_dir()
    by_id = api.load_tickets_map(repo)
    tree = api.tree(repo, args.id)
    if out.json:
        out.emit_json(
            {
                "root": _full(tree.root, out),
                "children": [_short(c, out) for c in tree.children],
            }
        )
        return 0
    _print_summary(tree.root, out, by_id)
    for child in tree.children:
        print("  ", end="")
        _print_summary(child, out, by_id)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    out = _make_out(args)
    repo = _repo_dir()
    by_id = api.load_tickets_map(repo)
    tickets = api.list_tickets(
        repo,
        status=args.status,
        label=args.label,
        parent=args.parent,
        type=args.type,
        match=args.match,
    )
    if out.json:
        out.emit_json([_short(t, out) for t in tickets])
        return 0
    if not tickets:
        print("No tickets match.")
        return 0
    for t in tickets:
        _print_summary(t, out, by_id)
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    out = _make_out(args)
    result = api.claim(_repo_dir(), args.id, actor=_actor_of(args))
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        verb = "Claimed" if result.wrote else "Already claimed"
        print(f"{verb} {_rend(result.ticket.id, out)} -> in_progress")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    out = _make_out(args)
    assignments = [api.parse_assignment(tok) for tok in args.assignments]
    result = api.set_fields(_repo_dir(), args.id, assignments, actor=_actor_of(args))
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        verb = "Updated" if result.wrote else "No change to"
        print(f"{verb} {_rend(result.ticket.id, out)}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    out = _make_out(args)
    result = api.close(_repo_dir(), args.id, reason=args.reason, actor=_actor_of(args))
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        verb = "Closed" if result.wrote else "Already closed"
        print(f"{verb} {_rend(result.ticket.id, out)} -> done")
    return 0


def cmd_drop(args: argparse.Namespace) -> int:
    out = _make_out(args)
    result = api.drop(_repo_dir(), args.id, reason=args.reason, actor=_actor_of(args))
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        verb = "Dropped" if result.wrote else "Already dropped"
        print(f"{verb} {_rend(result.ticket.id, out)} -> dropped")
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    out = _make_out(args)
    result = api.add_comment(_repo_dir(), args.id, args.text, actor=_actor_of(args))
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        print(f"Noted on {_rend(result.ticket.id, out)}")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    out = _make_out(args)
    result = api.link_remote(_repo_dir(), args.id, args.remote, args.ref, actor=_actor_of(args))
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        print(f"Linked {_rend(result.ticket.id, out)} -> {args.remote}/{args.ref}")
    return 0


def cmd_unlink(args: argparse.Namespace) -> int:
    out = _make_out(args)
    result = api.unlink_remote(_repo_dir(), args.id, args.remote, actor=_actor_of(args))
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        verb = "Unlinked" if result.wrote else "Already unlinked"
        print(f"{verb} {_rend(result.ticket.id, out)} from {args.remote}")
    return 0


def cmd_comments(args: argparse.Namespace) -> int:
    out = _make_out(args)
    notes = api.comments(_repo_dir(), args.id)
    if out.json:
        out.emit_json([api.snapshot_comment(n) for n in notes])
        return 0
    if not notes:
        print("No notes.")
        return 0
    for n in notes:
        print(f"[{n.ts}] {n.actor}: {n.text}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    out = _make_out(args)
    events = api.event_log(_repo_dir(), args.id)
    if out.json:
        import msgspec

        out.emit_json([msgspec.json.decode(msgspec.json.encode(e)) for e in events])
        return 0
    if not events:
        print("No events.")
        return 0
    for e in events:
        ref = e.reason or e.text or e.remote or ""
        detail = f"  {ref}" if ref else ""
        print(f"[{e.ts}] {e.id} {e.actor} {e.op} {_rend(e.ticket, out)}{detail}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from rohrpost import doctor

    return doctor.run(_repo_dir(), json_output=getattr(args, "json", False))


def cmd_compact(args: argparse.Namespace) -> int:
    from rohrpost import compact

    days = (
        args.archive_after if args.archive_after is not None else compact.DEFAULT_ARCHIVE_AFTER_DAYS
    )
    return compact.run(
        _repo_dir(),
        archive_after_days=days,
        force=args.force,
        json_output=getattr(args, "json", False),
    )


def compact_default_days() -> int:
    from rohrpost import compact

    return compact.DEFAULT_ARCHIVE_AFTER_DAYS


def cmd_stats(args: argparse.Namespace) -> int:
    from rohrpost import stats as stats_mod

    out = _make_out(args)
    data = stats_mod.compute_stats(_repo_dir())
    if out.json:
        out.emit_json(data)
        return 0
    _print_stats(data, out)
    return 0


def _build_provider(remote: str, config: Config) -> Provider:
    """Build the provider for ``remote`` from config. GitHub is built first (§8.5)."""
    from rohrpost.providers.github import GitHubProvider

    raw = config.remotes.get(remote)
    if raw is None:
        raise RohrpostError(f"no [remotes.{remote}] configured")
    if remote == "github" or raw.get("type") == "github":
        return GitHubProvider(raw)
    raise RohrpostError(f"no provider available for remote {remote!r}")


def cmd_sync(args: argparse.Namespace) -> int:
    from rohrpost import sync

    out = _make_out(args)
    repo = _repo_dir()
    config = api.load_repo_config(repo)
    remote = args.remote or _single_remote(config)
    provider = _build_provider(remote, config)
    report = sync.sync_round(repo, remote, provider, config, dry_run=args.dry_run)
    if out.json:
        payload = {
            "remote": report.remote,
            "tickets": [
                {
                    "ticket": f"{out.prefix}-{t.ticket}",
                    "ref": t.ref,
                    "pulled": t.pulled,
                    "pushed": t.pushed,
                    "conflicts": t.conflicts,
                }
                for t in report.tickets
            ],
            "pulled": report.pulled,
            "pushed": report.pushed,
            "conflicts": report.conflicts,
        }
        out.emit_json(payload)
        return 0
    mode = " (dry run)" if args.dry_run else ""
    print(
        f"Synced {report.remote}{mode}: pulled {report.pulled}, pushed {report.pushed}, "
        f"{report.conflicts} conflict(s) across {len(report.tickets)} ticket(s)."
    )
    for t in report.tickets:
        if t.conflicts:
            print(f"  conflict: {_rend(t.ticket, out)} on {','.join(t.conflicts)}")
    return 0


def _single_remote(config: Config) -> str:
    if len(config.remotes) != 1:
        raise RohrpostError("specify a remote: rp sync <remote>")
    return str(next(iter(config.remotes)))


def cmd_conflicts(args: argparse.Namespace) -> int:
    out = _make_out(args)
    tickets = api.list_conflicts(_repo_dir())
    if out.json:
        out.emit_json([_short(t, out) for t in tickets])
        return 0
    if not tickets:
        print("No conflicts.")
        return 0
    for t in tickets:
        print(_rend(t.id, out), [lab for lab in t.labels if lab.startswith("conflict:")])
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    out = _make_out(args)
    if not args.take:
        raise RohrpostError("resolve requires --take local|remote")
    result = api.resolve_conflict(_repo_dir(), args.id, args.take, actor=_actor_of(args))
    if out.json:
        out.emit_json(_full(result.ticket, out))
    else:
        print(f"Resolved {_rend(result.ticket.id, out)} (took {args.take})")
    return 0


def _unimplemented(args: argparse.Namespace) -> int:
    print(f"rp: '{args.command}' is not implemented yet", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Text rendering.
# ---------------------------------------------------------------------------
def _rend(bare_id: str, out: _Out) -> str:
    return f"{out.prefix}-{bare_id}"


def _print_summary(
    ticket: api.Ticket,
    out: _Out,
    by_id: dict[str, api.Ticket],
    *,
    status_override: str | None = None,
) -> None:
    status = status_override or derive_status(ticket, by_id)
    print(
        f"{_rend(ticket.id, out)}  "
        f"[{_color_status(status, out.color)}]  "
        f"{ticket.type}  p{ticket.priority}  {ticket.title}"
    )


def _print_created(ticket: api.Ticket, out: _Out) -> None:
    print(f"Created {_rend(ticket.id, out)}  {ticket.title}")


def _print_detail(
    ticket: api.Ticket, out: _Out, *, include: str, by_id: dict[str, api.Ticket]
) -> None:
    sections = {s.strip() for s in include.split(",") if s.strip()}
    status = derive_status(ticket, by_id)
    print(f"{_rend(ticket.id, out)}  {ticket.title}")
    print(f"  status:   {_color_status(status, out.color)}")
    print(f"  type:     {ticket.type}")
    print(f"  priority: {ticket.priority}")
    for label, value in _detail_fields(ticket, out):
        print(f"  {label}:   {value}")
    print(f"  created:  {ticket.created}")
    print(f"  updated:  {ticket.updated}")
    _print_detail_sections(ticket, out, sections=sections, by_id=by_id)


def _detail_fields(ticket: api.Ticket, out: _Out) -> list[tuple[str, str]]:
    """Optional scalar fields that only render when non-empty."""
    fields: list[tuple[str, str]] = []
    if ticket.assignee:
        fields.append(("assignee", ticket.assignee))
    if ticket.parent:
        fields.append(("parent", _rend(ticket.parent, out)))
    if ticket.labels:
        fields.append(("labels", ", ".join(ticket.labels)))
    if ticket.remotes:
        fields.append(("remotes", ", ".join(f"{k}/{v}" for k, v in ticket.remotes.items())))
    if ticket.last_close_reason:
        fields.append(("close", ticket.last_close_reason))
    return fields


def _print_detail_sections(
    ticket: api.Ticket,
    out: _Out,
    *,
    sections: set[str],
    by_id: dict[str, api.Ticket],
) -> None:
    if "deps" in sections:
        _print_deps(ticket, out, by_id)
    if "body" in sections:
        _print_body(ticket)
    if "notes" in sections:
        _print_notes(ticket)
    if "fieldts" in sections:
        _print_fieldts(ticket)


def _print_deps(ticket: api.Ticket, out: _Out, by_id: dict[str, api.Ticket]) -> None:
    if not ticket.blocked_by:
        return
    print("  blocked_by:")
    for dep in ticket.blocked_by:
        blocker = by_id.get(dep)
        mark = blocker.status if blocker else "missing"
        print(f"    - {_rend(dep, out)} ({mark})")


def _print_body(ticket: api.Ticket) -> None:
    if ticket.body:
        print()
        print(ticket.body)


def _print_notes(ticket: api.Ticket) -> None:
    if not ticket.comments:
        return
    print("  notes:")
    for note in ticket.comments[-10:]:
        print(f"    [{note.ts}] {note.actor}: {note.text}")


def _print_fieldts(ticket: api.Ticket) -> None:
    print("  _fieldts:")
    for key in sorted(ticket.fieldts):
        print(f"    {key}: {ticket.fieldts[key]}")


def _print_stats(data: dict[str, object], out: _Out) -> None:
    """Render ``rp stats`` as a compact, human-readable summary (spec §13.1)."""
    body = _as_stats_dist(data.get("body_bytes"))
    line = _as_stats_dist(data.get("event_line_bytes"))
    over = line.get("over_pipe_buf", 0)
    lock_share = line.get("lock_share_pct", 0.0)
    out.print(
        f"events: {data.get('events')}  tickets: {data.get('tickets')}  "
        f"PIPE_BUF: {data.get('pipe_buf')}"
    )
    out.print(
        "body bytes:       "
        f"p50 {body['p50']}  p90 {body['p90']}  p95 {body['p95']}  "
        f"p99 {body['p99']}  max {body['max']}  (n={body['count']})"
    )
    out.print(
        "event line bytes: "
        f"p50 {line['p50']}  p95 {line['p95']}  max {line['max']}  "
        f"over PIPE_BUF: {over} ({lock_share}% of set events)"
    )
    out.print(f"cold fold: {data.get('fold_ms')} ms (median)")


def _as_stats_dist(value: object) -> dict[str, object]:
    """Narrow a stats distribution mapping, defaulting the standard keys to 0.

    Non-standard keys (``over_pipe_buf``, ``lock_share_pct`` on the line
    distribution) are carried through unchanged.
    """
    raw = value if isinstance(value, dict) else {}
    base = {key: raw.get(key, 0) for key in ("p50", "p90", "p95", "p99", "max", "count")}
    for key, val in raw.items():
        if key not in base:
            base[key] = val
    return base


# ---------------------------------------------------------------------------
# Dispatch.
# ---------------------------------------------------------------------------
_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": cmd_init,
    "new": cmd_new,
    "ready": cmd_ready,
    "show": cmd_show,
    "tree": cmd_tree,
    "list": cmd_list,
    "claim": cmd_claim,
    "set": cmd_set,
    "close": cmd_close,
    "drop": cmd_drop,
    "comment": cmd_comment,
    "comments": cmd_comments,
    "link": cmd_link,
    "unlink": cmd_unlink,
    "log": cmd_log,
    "doctor": cmd_doctor,
    "compact": cmd_compact,
    "stats": cmd_stats,
    "sync": cmd_sync,
    "conflicts": cmd_conflicts,
    "resolve": cmd_resolve,
}


def _force_utf8_streams() -> None:
    """Pin stdout/stderr to UTF-8 so all rp output is locale-independent.

    On Windows, Python encodes piped/redirected streams with the ANSI code page
    (cp1252), which cannot represent non-ASCII ticket text — ``--json`` output
    (``ensure_ascii=False``) would crash instead of printing. Best-effort: a
    replaced stream may not support reconfiguration (e.g. test capture).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``rp`` CLI. Returns a process exit code."""
    _force_utf8_streams()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command in _UNIMPLEMENTED:
        return _unimplemented(args)

    handler = _HANDLERS[args.command]
    try:
        return handler(args)
    except RohrpostError as exc:
        print(f"rp: {exc}", file=sys.stderr)
        return 1
