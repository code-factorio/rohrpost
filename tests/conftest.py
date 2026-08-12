"""Shared pytest fixtures for the rohrpost test-suite."""

from __future__ import annotations

import datetime as dt
import itertools
import os
import subprocess
from pathlib import Path

import pytest

from rohrpost import api, paths
from rohrpost.api import UlidFactory
from rohrpost.util import Clock


def ms_to_ts(ms: int) -> str:
    """Render milliseconds since epoch as an RFC 3339 UTC ms timestamp."""
    return (
        dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A temp directory with git + ``rp init`` already run; returns the ``.rohrpost`` dir."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@rohrpost.local"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=tmp_path, check=True)
    api.init_repo(tmp_path, prefix="TST")
    found = paths.find_rohrpost_dir(tmp_path)
    assert found is not None
    return found


@pytest.fixture
def deterministic_clock() -> Clock:
    """A clock returning strictly increasing ms timestamps (2026-01-01 base)."""
    counter = itertools.count(0)
    return lambda: ms_to_ts(1_767_225_600_000 + next(counter))


@pytest.fixture
def deterministic_ulid() -> UlidFactory:
    """A ULID factory returning strictly increasing ULIDs (deterministic)."""
    from rohrpost.ids import new_ulid

    counter = itertools.count(0)
    return lambda: new_ulid(timestamp_ms=1_767_225_600_000 + next(counter))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``@pytest.mark.experiment`` tests unless ``ROHRPOST_RUN_EXPERIMENTS`` is set.

    Concurrency races and timings are informative experiments but not deterministic
    regressions, so they never run in the default gate — only on explicit request.
    """
    if os.environ.get("ROHRPOST_RUN_EXPERIMENTS"):
        return
    skip = pytest.mark.skip(reason="set ROHRPOST_RUN_EXPERIMENTS=1 to run experiments")
    for item in items:
        if "experiment" in item.keywords:
            item.add_marker(skip)
