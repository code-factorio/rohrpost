"""Small shared utilities: timestamps and the actor-resolution policy.

Timestamps are RFC 3339, UTC, millisecond precision (``2026-08-11T09:20:14.221Z``),
matching the event example in spec §5.2. The actor namespace (``user/*``,
``runner/*``, ``remote/*``) is load-bearing per §5.2 — it distinguishes a human
decision from a runner write from a change that arrived through sync — and is
resolved here from git config and environment so callers never hardcode a name.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import lru_cache

#: A clock returns the current timestamp string. Injectable for deterministic tests.
Clock = Callable[[], str]

#: Process-local high-water mark for the monotonic clock (see :func:`now_ts`).
_last_ms: int = 0


def now_ts() -> str:
    """RFC 3339 UTC timestamp, millisecond precision, strictly increasing per process.

    Two events written in the same millisecond would otherwise tie on ``ts`` and
    fall back to the ULID's random suffix for sort order (spec §6) — reordering
    append-only things like comments. Bumping the millisecond on collision keeps
    insertion order deterministic within a process. The drift under a burst is
    negligible (one ms per event) and cross-process ordering still relies on the
    ULID tiebreak, which is correct for last-write-wins field semantics.
    """
    global _last_ms
    ms = time.time_ns() // 1_000_000
    if ms <= _last_ms:
        ms = _last_ms + 1
    _last_ms = ms
    return (
        datetime.fromtimestamp(ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@lru_cache(maxsize=1)
def _git_email() -> str | None:
    """Best-effort ``git config user.email``. Cached; ``None`` if git or unset."""
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        result = subprocess.run(
            ["git", "config", "user.email"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        email = result.stdout.strip()
        return email or None
    return None


def resolve_actor(
    *,
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the actor string for an event, honouring explicit override > env > git.

    Precedence (spec §5.2 namespaces — never hardcode a name):

    1. ``explicit`` — a caller-provided ``--actor`` value, used verbatim.
    2. ``ROHRPOST_ACTOR`` env — used verbatim.
    3. ``ROHRPOST_RUNNER`` env → ``runner/<name>`` with optional ``@<batch>``
       (``ROHRPOST_BATCH``). This is how a runner identifies itself.
    4. ``user/<git config user.email>`` — the human default.

    Falls back to ``user/<login>`` then ``user/unknown`` when git is unavailable.
    """
    if explicit:
        return explicit

    environ: Mapping[str, str] = env if env is not None else os.environ

    actor = environ.get("ROHRPOST_ACTOR")
    if actor:
        return actor

    runner = environ.get("ROHRPOST_RUNNER")
    if runner:
        batch = environ.get("ROHRPOST_BATCH")
        return f"runner/{runner}@{batch}" if batch else f"runner/{runner}"

    email = _git_email()
    if email:
        return f"user/{email}"

    import getpass

    with contextlib.suppress(KeyError, OSError):  # pragma: no cover - platform-dependent
        return f"user/{getpass.getuser()}"
    return "user/unknown"
