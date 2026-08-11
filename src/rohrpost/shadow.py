"""Shadow snapshots: the sync merge base (spec §8.1).

``shadow/<remote>/<ref>.json`` holds the remote's field values **as of the last
successful sync**. Without it you cannot distinguish "local changed" from "remote
changed" from "both changed", and you will either clobber remote edits or refuse
to sync anything.

A shadow is written after a sync round from the post-sync remote state, so a
crash between "apply remote-won locally" and "rewrite shadow" leaves a stale
shadow (a redundant merge next round, which is idempotent) rather than a lost
update.
"""

from __future__ import annotations

import json
from pathlib import Path

from rohrpost import paths


def _safe_ref(ref: str) -> str:
    """Make a remote ref safe to use as a filename (refs can be issue numbers or keys)."""
    return ref.replace("/", "_")


def shadow_path(rohrpost_dir: Path, remote: str, ref: str) -> Path:
    return paths.shadow_dir(rohrpost_dir) / remote / f"{_safe_ref(ref)}.json"


def read_shadow(rohrpost_dir: Path, remote: str, ref: str) -> dict[str, object] | None:
    """Read the merge base for ``(remote, ref)``. ``None`` if there is no shadow yet."""
    path = shadow_path(rohrpost_dir, remote, ref)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except OSError:
        return None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_shadow(rohrpost_dir: Path, remote: str, ref: str, fields: dict[str, object]) -> None:
    """Persist the post-sync remote field values as the new merge base."""
    path = shadow_path(rohrpost_dir, remote, ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields, ensure_ascii=False, sort_keys=True))


def all_shadowed(rohrpost_dir: Path) -> list[tuple[str, str]]:
    """List ``(remote, ref)`` pairs that currently have a shadow file."""
    root = paths.shadow_dir(rohrpost_dir)
    if not root.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for remote_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        out.extend((remote_dir.name, f.stem) for f in sorted(remote_dir.glob("*.json")))
    return out


__all__ = ["all_shadowed", "read_shadow", "shadow_path", "write_shadow"]
