"""Sync through a fake `gh` CLI: the round, its log writes, shadows and reports."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from conformance.conftest import Impl, Normalizer, Pair, gh_state, write_gh_state

# The fake gh is a script. Both implementations launch `gh` with CreateProcess
# on Windows, which only starts executables, so neither can reach it there;
# the sync round is platform-independent and is covered on Linux and macOS.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake gh CLI is a script that CreateProcess cannot launch"
)

CONFIG = """[project]
prefix = "TST"

[remotes.github]
repo = "owner/name"
policy = "%s"

[remotes.github.fields]
title = "title"
body = "body"
labels = "labels"
status = { open = "open", in_progress = "open", review = "open", done = "closed", dropped = "closed" }
"""


def _setup(pair: Pair, fake_gh: Path, tmp_path: Path, policy: str = "flag") -> dict[str, Path]:
    states = {}
    for impl in pair.both:
        (impl.rohrpost_dir / "config.toml").write_text(
            CONFIG % policy, encoding="utf-8", newline="\n"
        )
        state = tmp_path / f"gh-{impl.name}.json"
        write_gh_state(
            state,
            {
                "42": {
                    "number": 42,
                    "title": "Remote title",
                    "body": "one\ntwo\nthree\nfour\nfive\nsix\n",
                    "state": "open",
                    "labels": [{"name": "a"}],
                }
            },
        )
        impl.env_extra = {
            "PATH": f"{fake_gh}{os.pathsep}{os.environ['PATH']}",
            "GH_STATE": str(state),
        }
        states[impl.name] = state
    return states


def _same_calls(states: dict[str, Path], pair: Pair) -> None:
    ref_calls = gh_state(states["reference"])["calls"]
    nat_calls = gh_state(states["native"])["calls"]
    norm_ref, norm_nat = Normalizer(pair.reference), Normalizer(pair.native)
    assert [norm_nat(json.dumps(c)) for c in nat_calls] == [
        norm_ref(json.dumps(c)) for c in ref_calls
    ]


def _same_shadows(pair: Pair) -> None:
    def read(impl: Impl) -> dict[str, bytes]:
        root = impl.rohrpost_dir / "shadow"
        return (
            {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.json")}
            if root.is_dir()
            else {}
        )

    assert read(pair.native) == read(pair.reference)


def test_first_sync_establishes_base_then_pulls_and_pushes(
    pair: Pair, fake_gh: Path, tmp_path: Path
) -> None:
    states = _setup(pair, fake_gh, tmp_path)
    ids = pair.new("Local title", "--body", "one\ntwo\nthree\nfour\nfive\nsix\n", "--label", "a")
    pair.each(ids, "link", "github", "42")
    pair.same("doctor", "--json")
    pair.same("sync", "--dry-run", "--json")
    pair.same("sync", "--dry-run")
    pair.same("sync")  # establishes the shadow; no winner chosen yet
    _same_shadows(pair)
    pair.same("sync", "--json")  # now a clean three-way merge: title conflict? no — base == remote
    pair.each(ids, "show", "--json")
    # Local edit only -> pushed.
    pair.each(ids, "set", "title=Edited locally", "labels+=b")
    pair.same("sync", "github", "--json")
    _same_shadows(pair)
    _same_calls(states, pair)
    pair.each(ids, "show", "--json")
    # Remote edit only -> pulled with actor remote/github.
    for impl in pair.both:
        state = gh_state(states[impl.name])
        state["issues"]["42"]["body"] = "one\ntwo\nthree\nfour\nfive\nREMOTE\n"
        state["issues"]["42"]["state"] = "closed"
        (tmp_path / f"gh-{impl.name}.json").write_text(json.dumps(state), encoding="utf-8")
    pair.same("sync")
    pair.each(ids, "show", "--json")
    pair.each(ids, "log", "--json")
    pair.same("log", "--json")  # includes the synced watermark events
    _same_shadows(pair)


def test_conflict_flag_and_resolve(pair: Pair, fake_gh: Path, tmp_path: Path) -> None:
    states = _setup(pair, fake_gh, tmp_path)
    ids = pair.new("Local title", "--body", "one\ntwo\nthree\nfour\nfive\nsix\n", "--label", "a")
    pair.each(ids, "link", "github", "42")
    pair.same("sync")
    pair.each(ids, "set", "title=Local edit", "body=LOCAL\ntwo\nthree\nfour\nfive\nsix\n")
    for impl in pair.both:
        state = gh_state(states[impl.name])
        state["issues"]["42"]["title"] = "Remote edit"
        state["issues"]["42"]["body"] = "one\ntwo\nthree\nfour\nfive\nREMOTE\n"
        (tmp_path / f"gh-{impl.name}.json").write_text(json.dumps(state), encoding="utf-8")
    pair.same("sync", "--dry-run", "--json")
    pair.same("sync")
    pair.same("conflicts")
    pair.same("conflicts", "--json")
    pair.each(ids, "show", "--json")
    pair.same("sync")  # completed conflict: deferred, no new writes
    pair.each(ids, "resolve", "--take", "local")
    pair.each(ids, "show", "--json")
    pair.same("sync", "--json")
    _same_shadows(pair)
    _same_calls(states, pair)


def test_policy_local_and_remote(pair: Pair, fake_gh: Path, tmp_path: Path) -> None:
    for policy in ("local", "remote"):
        states = _setup(pair, fake_gh, tmp_path, policy)
        ids = pair.new(f"Ticket {policy}", "--label", "a")
        pair.each(ids, "link", "github", "42")
        pair.same("sync")
        pair.each(ids, "set", "title=Local edit")
        for impl in pair.both:
            state = gh_state(states[impl.name])
            state["issues"]["42"]["title"] = "Remote edit"
            (tmp_path / f"gh-{impl.name}.json").write_text(json.dumps(state), encoding="utf-8")
        pair.same("sync", "--json")
        pair.each(ids, "show", "--json")
        pair.each(ids, "unlink", "github")
        _same_calls(states, pair)


def test_deleted_remote_item_is_flagged(pair: Pair, fake_gh: Path, tmp_path: Path) -> None:
    _setup(pair, fake_gh, tmp_path)
    ids = pair.new("Linked to nothing")
    pair.each(ids, "link", "github", "404")
    pair.same("sync", "--dry-run", "--json")
    pair.same("sync")
    pair.each(ids, "show", "--json")
    pair.same("sync", "--dry-run", "--json")
    pair.same("sync")
    pair.same("doctor", "--json")


def test_sync_without_gh_and_without_token_falls_back_and_fails_cleanly(
    pair: Pair, tmp_path: Path
) -> None:
    for impl in pair.both:
        (impl.rohrpost_dir / "config.toml").write_text(
            CONFIG.replace("owner/name", "owner/name").replace("%s", "flag")
            + 'url = "http://127.0.0.1:9"\n',
            encoding="utf-8",
            newline="\n",
        )
        impl.env_extra = {"PATH": str(tmp_path / "empty-bin")}
    (tmp_path / "empty-bin").mkdir()
    ids = pair.new("t")
    pair.each(ids, "link", "github", "1")
    ref, nat = pair.run("doctor", "--json")
    assert ref.code == nat.code == 1
    assert json.loads(nat.stdout)[-1]["ok"] is False
