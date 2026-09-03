"""Help, usage and version output must be byte-identical: agents read it."""

from __future__ import annotations

import pytest

from conformance.conftest import Pair

COMMANDS = [
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
    "unlink",
    "log",
    "doctor",
    "compact",
    "stats",
    "sync",
    "conflicts",
    "resolve",
]


def test_version(bare_pair: Pair) -> None:
    bare_pair.same("--version", check_log=False)


def test_root_help(bare_pair: Pair) -> None:
    bare_pair.same("--help", check_log=False)
    bare_pair.same("-h", check_log=False)


def test_no_command_prints_help(bare_pair: Pair) -> None:
    result = bare_pair.same(check_log=False)
    assert result.code == 0
    assert "usage:" in result.out


@pytest.mark.parametrize("command", COMMANDS)
def test_subcommand_help(bare_pair: Pair, command: str) -> None:
    bare_pair.same(command, "--help", check_log=False)


@pytest.mark.parametrize("columns", ["60", "100", "120"])
def test_help_honours_columns(bare_pair: Pair, columns: str) -> None:
    bare_pair.same("new", "--help", env={"COLUMNS": columns}, check_log=False)
    bare_pair.same("--help", env={"COLUMNS": columns}, check_log=False)
