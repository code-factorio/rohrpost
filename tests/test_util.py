"""Focused tests for :mod:`rohrpost.util` — timestamps and actor resolution."""

from __future__ import annotations

from rohrpost.util import now_ts, resolve_actor


def test_now_ts_is_rfc3339_utc_with_z() -> None:
    ts = now_ts()
    assert ts.endswith("Z")
    assert len(ts) == 24  # 2026-08-11T09:20:14.221Z
    assert ts[10] == "T"
    assert ts[19] == "."


def test_now_ts_is_strictly_increasing() -> None:
    a = now_ts()
    b = now_ts()
    c = now_ts()
    assert a < b < c


def test_resolve_actor_explicit_wins() -> None:
    assert resolve_actor(explicit="custom/x", env={"ROHRPOST_ACTOR": "env/y"}) == "custom/x"


def test_resolve_actor_env_actor() -> None:
    assert resolve_actor(env={"ROHRPOST_ACTOR": "env/y"}) == "env/y"


def test_resolve_actor_runner_with_batch() -> None:
    env = {"ROHRPOST_RUNNER": "claude-code", "ROHRPOST_BATCH": "b-3"}
    assert resolve_actor(env=env) == "runner/claude-code@b-3"


def test_resolve_actor_runner_without_batch() -> None:
    assert resolve_actor(env={"ROHRPOST_RUNNER": "claude-code"}) == "runner/claude-code"


def test_resolve_actor_falls_back_to_user_namespace(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # No explicit, no actor env, no runner env, no git email -> user/<login> or user/unknown.
    actor = resolve_actor(env={})
    assert actor.startswith("user/")
