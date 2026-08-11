"""Three-way field merge for sync (spec §8.2, §8.3).

Sync is a three-way merge and a three-way merge needs a base. Given the shadow
snapshot (``base``, the remote's field values as of the last successful sync),
the folded ticket (``local``), and the live remote (``remote``), resolve each
mapped field:

| Condition                | Action                                  |
|--------------------------|-----------------------------------------|
| ``local == remote``      | nothing                                 |
| ``local == base``        | remote changed → take remote            |
| ``remote == base``       | local changed → push local              |
| all three differ         | conflict → apply policy                 |

Prose bodies are the one field where per-field LWW is unacceptable — it throws
away real human writing — so the ``body`` field is merged with a genuine
three-way text merge via ``git merge-file`` (§8.3), keeping conflict
markers on collision.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

#: Conflict resolution policies (spec §8.2). ``flag`` is the default.
Policy = Literal["flag", "local", "remote"]

#: The field name that holds free-form prose and therefore gets a real text merge.
BODY_FIELD: str = "body"


@dataclass(frozen=True, slots=True)
class FieldConflict:
    """One field where local, remote and base all differ."""

    field: str
    local: object
    remote: object


@dataclass(frozen=True, slots=True)
class MergeResult:
    """The outcome of a three-way merge over all mapped fields.

    ``remote_won`` are field values to apply locally (append ``set`` events with
    actor ``remote/<name>``); ``local_won`` are field values to push to the
    remote; ``conflicts`` are fields that need human resolution under ``flag``.
    The merged body (if any) lands in ``remote_won`` or ``local_won`` exactly
    like any other field.
    """

    remote_won: dict[str, object] = field(default_factory=dict)
    local_won: dict[str, object] = field(default_factory=dict)
    conflicts: list[FieldConflict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.conflicts


def merge_text(base: str, local: str, remote: str) -> tuple[str, bool]:
    """Three-way text merge via ``git merge-file``.

    Returns ``(merged_text, had_conflict)``. On conflict the markers (``<<<<<<<``
    … ``>>>>>>>``) are left in the text for the caller to flag. Falls back to the
    local text if git is unavailable, so sync degrades rather than crashes.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base_p, local_p, remote_p = (Path(tmp) / n for n in ("base", "local", "remote"))
            base_p.write_text(base)
            local_p.write_text(local)
            remote_p.write_text(remote)
            proc = subprocess.run(
                ["git", "merge-file", "-p", str(local_p), str(base_p), str(remote_p)],
                capture_output=True,
                check=False,
            )
    except FileNotFoundError:
        return local, local != remote
    # git merge-file: 0 = clean, 1 = conflicts, >1 = error/usage.
    if proc.returncode > 1:
        return local, local != remote
    merged = proc.stdout.decode("utf-8", errors="replace")
    return merged, proc.returncode == 1


def three_way(
    base: dict[str, object],
    local: dict[str, object],
    remote: dict[str, object],
    *,
    policy: Policy = "flag",
    body_field: str = BODY_FIELD,
) -> MergeResult:
    """Resolve every mapped field three-way.

    ``base``/``local``/``remote`` are dicts of the mapped field values (already
    translated to the local vocabulary). Fields present on only one side are
    treated as ``base``-absent changes.
    """
    result = MergeResult()
    for name in sorted(base.keys() | local.keys() | remote.keys()):
        b = base.get(name)
        lv = local.get(name)
        rv = remote.get(name)
        if lv == rv:
            continue
        if name == body_field and _all_str(b, lv, rv):
            _merge_body(result, name, b, lv, rv, policy)
        else:
            _merge_scalar(result, name, b, lv, rv, policy)
    return result


def _merge_body(
    result: MergeResult, name: str, b: object, lv: object, rv: object, policy: Policy
) -> None:
    """Three-way text-merge a prose body field; adopt or flag per policy."""
    merged, conflict = merge_text(str(b or ""), str(lv or ""), str(rv or ""))
    if conflict:
        if policy == "flag":
            result.conflicts.append(FieldConflict(name, lv, rv))
            return
        _apply_conflict_policy(result, name, lv, rv, policy)
        return
    if merged != lv:  # the combined text differs from local -> adopt locally
        result.remote_won[name] = merged
    if merged != rv:  # ...and from remote -> push
        result.local_won[name] = merged


def _merge_scalar(
    result: MergeResult, name: str, b: object, lv: object, rv: object, policy: Policy
) -> None:
    """Resolve a non-prose field by per-field LWW against the base."""
    if lv == b:  # only remote changed
        result.remote_won[name] = rv
    elif rv == b:  # only local changed
        result.local_won[name] = lv
    else:  # all three differ
        _apply_conflict_policy(result, name, lv, rv, policy)


def _all_str(*values: object) -> bool:
    return all(v is None or isinstance(v, str) for v in values)


def _apply_conflict_policy(
    result: MergeResult, name: str, local: object, remote: object, policy: Policy
) -> None:
    """Resolve a conflicting scalar field per the configured policy (§8.2)."""
    if policy == "local":
        result.local_won[name] = local
    elif policy == "remote":
        result.remote_won[name] = remote
    else:  # flag — leave for a human; `rp resolve` clears it
        result.conflicts.append(FieldConflict(name, local, remote))


__all__ = ["BODY_FIELD", "FieldConflict", "MergeResult", "Policy", "merge_text", "three_way"]
