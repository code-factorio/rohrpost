"""The executable sync truth table: add regressions as rows, not test functions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    name: str
    base: dict[str, object]
    local: dict[str, object]
    remote: dict[str, object]
    policy: str = "flag"
    expect_local: dict[str, object] | None = None
    expect_remote: dict[str, object] | None = None
    expect_pushed: tuple[str, ...] = ()
    expect_conflict: bool = False


BASE_BODY = "one\ntwo\nthree\nfour\nfive\nsix\n"
LOCAL_BODY = "LOCAL\ntwo\nthree\nfour\nfive\nsix\n"
REMOTE_BODY = "one\ntwo\nthree\nfour\nfive\nREMOTE\n"
MERGED_BODY = "LOCAL\ntwo\nthree\nfour\nfive\nREMOTE\n"


SCENARIOS = (
    Scenario(
        "T1 scalar unchanged",
        {"title": "A"},
        {"title": "A"},
        {"title": "A"},
        expect_local={"title": "A"},
        expect_remote={"title": "A"},
    ),
    Scenario(
        "T2 scalar remote",
        {"title": "A"},
        {"title": "A"},
        {"title": "B"},
        expect_local={"title": "B"},
        expect_remote={"title": "B"},
    ),
    Scenario(
        "T3 scalar local",
        {"title": "A"},
        {"title": "B"},
        {"title": "A"},
        expect_local={"title": "B"},
        expect_remote={"title": "B"},
        expect_pushed=("title",),
    ),
    Scenario(
        "T4 scalar conflict",
        {"title": "A"},
        {"title": "B"},
        {"title": "C"},
        expect_local={"title": "B"},
        expect_remote={"title": "C"},
        expect_conflict=True,
    ),
    Scenario(
        "T5 enum remote",
        {"status": "open"},
        {"status": "open"},
        {"status": "done"},
        expect_local={"status": "done"},
        expect_remote={"status": "done"},
    ),
    Scenario(
        "T6 enum local",
        {"status": "open"},
        {"status": "done"},
        {"status": "open"},
        expect_local={"status": "done"},
        expect_remote={"status": "done"},
        expect_pushed=("status",),
    ),
    Scenario(
        "T7 enum conflict",
        {"status": "open"},
        {"status": "review"},
        {"status": "done"},
        expect_local={"status": "review"},
        expect_remote={"status": "done"},
        expect_conflict=True,
    ),
    Scenario(
        "T8 array local add",
        {"labels": ["a"]},
        {"labels": ["a", "b"]},
        {"labels": ["a"]},
        expect_local={"labels": ["a", "b"]},
        expect_remote={"labels": ["a", "b"]},
        expect_pushed=("labels",),
    ),
    Scenario(
        "T9 array remote add",
        {"labels": ["a"]},
        {"labels": ["a"]},
        {"labels": ["a", "c"]},
        expect_local={"labels": ["a", "c"]},
        expect_remote={"labels": ["a", "c"]},
    ),
    Scenario(
        "T10 array union",
        {"labels": ["a"]},
        {"labels": ["a", "b"]},
        {"labels": ["a", "c"]},
        expect_local={"labels": ["a", "b", "c"]},
        expect_remote={"labels": ["a", "b", "c"]},
        expect_pushed=("labels",),
    ),
    Scenario(
        "T11 array remove and add",
        {"labels": ["a", "b"]},
        {"labels": ["a"]},
        {"labels": ["a", "b", "c"]},
        expect_local={"labels": ["a", "c"]},
        expect_remote={"labels": ["a", "c"]},
        expect_pushed=("labels",),
    ),
    Scenario(
        "T12 prose local",
        {"body": BASE_BODY},
        {"body": LOCAL_BODY},
        {"body": BASE_BODY},
        expect_local={"body": LOCAL_BODY},
        expect_remote={"body": LOCAL_BODY},
        expect_pushed=("body",),
    ),
    Scenario(
        "T13 prose clean merge",
        {"body": BASE_BODY},
        {"body": LOCAL_BODY},
        {"body": REMOTE_BODY},
        expect_local={"body": MERGED_BODY},
        expect_remote={"body": MERGED_BODY},
        expect_pushed=("body",),
    ),
    Scenario(
        "T14 prose conflict",
        {"body": "shared paragraph\n"},
        {"body": "local paragraph\n"},
        {"body": "remote paragraph\n"},
        expect_remote={"body": "remote paragraph\n"},
        expect_conflict=True,
    ),
)


CONFLICT_SCENARIOS = tuple(scenario for scenario in SCENARIOS if scenario.expect_conflict)
