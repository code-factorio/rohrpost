"""Argument-parsing errors: exit 2 with argparse's usage and wording."""

from __future__ import annotations

import pytest

from conformance.conftest import Pair

BAD_INVOCATIONS = [
    ["bogus"],
    ["show"],
    ["new"],
    ["new", "t", "-p", "x"],
    ["new", "t", "--foo"],
    ["ready", "--limit"],
    ["resolve", "x", "--take", "nope"],
    ["link", "a", "b"],
    ["set"],
    ["new", "t", "--p", "1"],
    ["show", "a", "b"],
    ["--jsn"],
    ["new", "t", "-p"],
    ["new", "t", "--json", "extra"],
    ["new", "--", "-dashtitle", "--json"],
    ["new", "-x"],
    ["new", "--version"],
    ["new", "t", "--json=1"],
    ["comment", "a", "b", "c"],
    ["compact", "--archive-after", "soon"],
    ["new", "t", "--label"],
    ["--version", "new"],
    ["new", "-h", "extra"],
    ["set", "abcdef", "--", "a=b"],
]


@pytest.mark.parametrize("argv", BAD_INVOCATIONS, ids=lambda a: " ".join(a))
def test_usage_error_parity(bare_pair: Pair, argv: list[str]) -> None:
    bare_pair.same(*argv, check_log=False)


def test_outside_repository(bare_pair: Pair) -> None:
    result = bare_pair.same("list", check_log=False)
    assert result.code == 1
    assert "not a rohrpost repository" in result.err
    bare_pair.same("new", "--json", "t", check_log=False)  # option before positional parses


def test_domain_errors_after_init(pair: Pair) -> None:
    pair.same("show", "zzzzzz")
    pair.same("show", "ABCDEF")
    pair.same("show", "a-b-zzzzzz")
    pair.same("new", "t", "--type", "bogus")
    pair.same("new", "   ")
    pair.same("new", "t", "-p", "5")
    pair.same("new", "t", "-p", "-1")
    pair.same("new", "t", "--parent", "bogus")
    pair.same("new", "t", "--template", "missing")
    pair.same("new", "t", "--body", "x", "--body-file", "-")
    pair.same("new", "t", "--body-file", "no/such/file.md")
    pair.same("set", "zzzzzz", "status=done")
    pair.same("resolve", "zzzzzz")
    pair.same("sync")
    pair.same("sync", "--dry-run", "--json")
    pair.same("sync", "nothere")
    pair.same("link", "zzzzzz", "github", "1")
    pair.same("comments", "zzzzzz")
    pair.same("tree", "zzzzzz")
    pair.same("log", "zzzzzz")
    pair.same("claim", "zzzzzz")
    pair.same("close", "zzzzzz")
    pair.same("drop", "zzzzzz")
    pair.same("unlink", "zzzzzz", "github")
    pair.same("comment", "zzzzzz", "x")
