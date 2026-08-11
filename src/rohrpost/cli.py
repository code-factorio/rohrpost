"""The ``rp`` command-line entry point.

This is the single write path: every mutation goes through ``rp``. Phase 0 wires
up the full command surface from the spec (§10) so the binary, ``--help`` and
``--json`` plumbing are stable from day one; the implementations land behind each
subcommand as the store, fold and sync layers are built.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from rohrpost import __version__

#: Every subcommand the spec defines, registered as a scaffold. Each becomes a
#: real command as its phase lands; until then `rp <cmd>` reports it unimplemented.
_COMMANDS: tuple[str, ...] = (
    "init",
    "new",
    "ready",
    "show",
    "tree",
    "list",
    "claim",
    "set",
    "close",
    "drop",
    "comment",
    "comments",
    "link",
    "sync",
    "conflicts",
    "resolve",
    "log",
    "compact",
    "doctor",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rp",
        description="Rohrpost — a git-native ticket system for agentic coding workflows.",
    )
    parser.add_argument("--version", action="version", version=f"rohrpost {__version__}")
    subcommands = parser.add_subparsers(dest="command", metavar="<command>")
    for command in _COMMANDS:
        subcommands.add_parser(command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``rp`` CLI. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(f"rp: '{args.command}' is not implemented yet", file=sys.stderr)
    return 2
