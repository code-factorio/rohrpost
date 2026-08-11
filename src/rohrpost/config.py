"""Project configuration: ``.rohrpost/config.toml``.

The config file is committed and holds everything that is *not* the event log:
the display prefix, remote definitions, field mappings, and policy. Phase 0
needs only the ``[project]`` table (the display prefix); the ``[remotes.*]``
tables are consumed by the sync layer (spec §8) and are read here without being
validated in depth, so the shape is stable when sync lands.

Per spec §5.1 the prefix is **display-only** — it never enters the log — so a
config edit re-renders every ticket id with no migration through the history.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from rohrpost.exceptions import ConfigError

#: Two to five uppercase letters, matching Jira's project-key convention so the
#: two can be aligned where useful (spec §5.1).
_PREFIX_RE: re.Pattern[str] = re.compile(r"[A-Z]{2,5}\Z")

#: The fallback prefix when no config exists / no ``[project]`` table is present.
#: This is the ``RP-`` from spec §2 — the Rohrpost default, overridable per repo.
DEFAULT_PREFIX: str = "RP"

#: Filename of the committed config within ``.rohrpost/``.
CONFIG_FILENAME: str = "config.toml"


class Config:
    """Parsed ``config.toml``. Frozen value object.

    Only the phase-0 fields are typed members; the raw remote tables are kept
    on :attr:`remotes` for the sync layer to interpret.
    """

    __slots__ = ("default_branch", "prefix", "remotes")

    def __init__(
        self,
        *,
        prefix: str,
        default_branch: str | None = None,
        remotes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.prefix = prefix
        self.default_branch = default_branch
        self.remotes = remotes or {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Config(prefix={self.prefix!r}, default_branch={self.default_branch!r}, "
            f"remotes={list(self.remotes)!r})"
        )


def validate_prefix(prefix: str) -> str:
    """Normalise and validate a project prefix.

    Strips surrounding whitespace and uppercases (``fac`` → ``FAC``). Raises
    :class:`ConfigError` unless the result is two to five uppercase letters.
    """
    candidate = prefix.strip().upper()
    if _PREFIX_RE.match(candidate) is None:
        raise ConfigError(f"prefix must be 2-5 uppercase letters (e.g. 'FAC'), got {prefix!r}")
    return candidate


def default_config() -> Config:
    """The config used when ``config.toml`` is absent (e.g. before ``rp init``)."""
    return Config(prefix=DEFAULT_PREFIX)


def load_config(rohrpost_dir: Path) -> Config:
    """Load and validate ``config.toml`` from a ``.rohrpost/`` directory.

    A missing file yields :func:`default_config` so read paths work before init
    and in bare checkouts. A malformed file raises :class:`ConfigError`.
    """
    path = rohrpost_dir / CONFIG_FILENAME
    if not path.is_file():
        return default_config()

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid {CONFIG_FILENAME}: {exc}") from exc

    project = data.get("project", {})
    if not isinstance(project, dict):
        raise ConfigError("[project] must be a table")

    raw_prefix = project.get("prefix", DEFAULT_PREFIX)
    if not isinstance(raw_prefix, str):
        raise ConfigError("[project].prefix must be a string")
    prefix = validate_prefix(raw_prefix)

    default_branch = project.get("default_branch")
    if default_branch is not None and not isinstance(default_branch, str):
        raise ConfigError("[project].default_branch must be a string")

    remotes_raw = data.get("remotes", {})
    if not isinstance(remotes_raw, dict):
        raise ConfigError("[remotes] must be a table")
    remotes: dict[str, dict[str, Any]] = {
        name: table for name, table in remotes_raw.items() if isinstance(table, dict)
    }

    return Config(prefix=prefix, default_branch=default_branch, remotes=remotes)


def render_config_toml(prefix: str) -> str:
    """Render a minimal, committed ``config.toml`` body for ``rp init``."""
    return (
        "# Rohrpost project configuration. Committed; safe to hand-edit.\n"
        "# The prefix is DISPLAY-ONLY: it never enters the event log, so\n"
        "# renaming it here re-renders every ticket id with no migration.\n"
        "\n"
        f'[project]\nprefix = "{prefix}"\n'
        "\n"
        "# [remotes.github]   # phase 1: see spec §8\n"
        '# url = "https://api.github.com"\n'
        '# repo = "owner/name"\n'
    )
