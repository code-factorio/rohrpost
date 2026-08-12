"""``rp doctor`` — integrity and configuration checks (spec §10.1).

Doctor is the one place the spec lets the pneumatic metaphor into prose, so the
human report uses it ("stuck in the tube"). Under the hood it is a list of
independent checks, each returning a finding; a non-empty findings list is a
non-zero exit. ``--json`` returns the findings as a list.

Phase-0 scope: everything except the remote-credential and shadow checks (those
belong to the sync layer, spec §8, and are reported as *not applicable* when no
remotes are configured).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rohrpost import paths, shadow, store
from rohrpost.config import ConfigError, load_config
from rohrpost.events import Event
from rohrpost.fold import _read_snapshot, find_cycle, fold_all


@dataclass(frozen=True, slots=True)
class Finding:
    """One doctor result. ``ok`` is True for a passing check, False for a problem."""

    check: str
    ok: bool
    detail: str

    def to_mapping(self) -> dict[str, object]:
        return {"check": self.check, "ok": self.ok, "detail": self.detail}


def run(rohrpost_dir: Path, *, json_output: bool = False) -> int:
    """Run all checks. Returns 0 if every check passes, 1 otherwise.

    Each check isolates its failures: an unparseable log is reported by
    ``log_parses`` but does not prevent the rest from running (they degrade to a
    clear *skipped* finding rather than crashing).
    """
    log_ok, events = _safe_events(rohrpost_dir)
    checks: list[Finding] = [
        _check_log_parses(rohrpost_dir, log_ok, events),
        _check_no_duplicate_ids(log_ok, events),
        _check_references_resolve(rohrpost_dir, log_ok),
        _check_no_cycles(rohrpost_dir, log_ok),
        _check_gitattributes(rohrpost_dir),
        _check_snapshot_matches(rohrpost_dir, log_ok),
        _check_shadow_files(rohrpost_dir, log_ok),
        _check_remote_credentials(rohrpost_dir),
    ]

    if json_output:
        json.dump([f.to_mapping() for f in checks], sys.stdout, indent=2, ensure_ascii=False)
        print(file=sys.stdout)
        return 0 if all(f.ok for f in checks) else 1

    _print_report(checks)
    return 0 if all(f.ok for f in checks) else 1


def _safe_events(rohrpost_dir: Path) -> tuple[bool, list[Event]]:
    """Return ``(log_ok, events)``; events is empty if the log does not parse."""
    events, errors = store.read_events_lenient(rohrpost_dir)
    return (not errors, events)


def _check_log_parses(rohrpost_dir: Path, log_ok: bool, events: list[Event]) -> Finding:
    _, errors = store.read_events_lenient(rohrpost_dir)
    if errors:
        return Finding("log_parses", False, f"{len(errors)} malformed line(s); first: {errors[0]}")
    return Finding("log_parses", True, f"{len(events)} event(s) parsed cleanly")


def _check_no_duplicate_ids(log_ok: bool, events: list[Event]) -> Finding:
    if not log_ok:
        return Finding("no_duplicate_ids", True, "skipped (log unparseable)")
    seen: set[str] = set()
    dupes: set[str] = set()
    for ev in events:
        if ev.id in seen:
            dupes.add(ev.id)
        seen.add(ev.id)
    if dupes:
        return Finding("no_duplicate_ids", False, f"{len(dupes)} duplicate event id(s) after merge")
    return Finding("no_duplicate_ids", True, f"{len(seen)} unique event id(s)")


def _check_references_resolve(rohrpost_dir: Path, log_ok: bool) -> Finding:
    if not log_ok:
        return Finding("references_resolve", True, "skipped (log unparseable)")
    by_id = fold_all(rohrpost_dir)
    missing: list[str] = []
    for ticket in by_id.values():
        if ticket.parent and ticket.parent not in by_id:
            missing.append(f"{ticket.id} -> parent {ticket.parent}")
        for dep in ticket.blocked_by:
            if dep not in by_id:
                missing.append(f"{ticket.id} -> blocked_by {dep}")  # noqa: PERF401 (conditional append)
    if missing:
        return Finding(
            "references_resolve", False, f"{len(missing)} dangling reference(s): {missing[:3]}"
        )
    return Finding("references_resolve", True, "all parent/blocked_by references resolve")


def _check_no_cycles(rohrpost_dir: Path, log_ok: bool) -> Finding:
    if not log_ok:
        return Finding("no_cycles", True, "skipped (log unparseable)")
    by_id = fold_all(rohrpost_dir)
    cycle = find_cycle(by_id)
    if cycle:
        return Finding("no_cycles", False, f"dependency cycle: {' -> '.join(cycle)}")
    return Finding("no_cycles", True, "no dependency cycles")


def _check_gitattributes(rohrpost_dir: Path) -> Finding:
    repo_root = rohrpost_dir.parent
    path = repo_root / ".gitattributes"
    if not path.is_file():
        return Finding("gitattributes", False, ".gitattributes missing the union-merge rules")
    text = path.read_text()
    missing = [rule for rule in paths.GITATTRIBUTES_RULES if rule not in text]
    if missing:
        return Finding("gitattributes", False, f"missing rule(s): {missing}")
    return Finding("gitattributes", True, "union-merge rules present")


def _check_snapshot_matches(rohrpost_dir: Path, log_ok: bool) -> Finding:
    if not log_ok:
        return Finding("snapshot_matches", True, "skipped (log unparseable)")
    snap = paths.snapshot_path(rohrpost_dir)
    fresh = fold_all(rohrpost_dir)
    if not snap.is_file():
        return Finding(
            "snapshot_matches", True, "no snapshot on disk (will be generated on next read)"
        )
    cached = _read_snapshot(snap)
    if cached is None:
        return Finding("snapshot_matches", False, "snapshot on disk is unreadable")
    if fresh != cached:
        return Finding("snapshot_matches", False, "tickets.jsonl is stale relative to the log")
    return Finding(
        "snapshot_matches", True, f"snapshot matches a fresh fold ({len(fresh)} ticket(s))"
    )


def _check_shadow_files(rohrpost_dir: Path, log_ok: bool) -> Finding:
    """Every linked remote needs a shadow file (spec §10.1). NA until remotes are used."""
    if not log_ok:
        return Finding("shadow_files", True, "skipped (log unparseable)")
    by_id = fold_all(rohrpost_dir)
    remotes_in_use = {r for t in by_id.values() for r in t.remotes}
    if not remotes_in_use:
        return Finding("shadow_files", True, "no remotes in use (sync not configured)")
    missing = [
        f"{t.id} -> {name}/{ref}"
        for t in by_id.values()
        for name, ref in t.remotes.items()
        if not shadow.shadow_path(rohrpost_dir, name, ref).is_file()
    ]
    if missing:
        return Finding(
            "shadow_files", False, f"{len(missing)} missing shadow file(s): {missing[:3]}"
        )
    return Finding("shadow_files", True, "all linked remotes have shadow files")


def _check_remote_credentials(rohrpost_dir: Path) -> Finding:
    """Check that configured remotes have a usable local authentication source."""
    try:
        config = load_config(rohrpost_dir)
    except ConfigError as exc:
        return Finding("remote_credentials", False, f"cannot load remotes: {exc}")
    if not config.remotes:
        return Finding("remote_credentials", True, "no remotes configured")

    missing = [
        name
        for name, remote_config in config.remotes.items()
        if not _remote_authenticated(name, remote_config)
    ]
    if missing:
        return Finding(
            "remote_credentials",
            False,
            f"no authenticated credential source for: {', '.join(sorted(missing))}",
        )
    return Finding(
        "remote_credentials",
        True,
        f"authenticated credential source present for {len(config.remotes)} remote(s)",
    )


def _remote_authenticated(name: str, remote_config: dict[str, object]) -> bool:
    """Return whether a remote has credentials available without exposing them."""
    remote_type = str(remote_config.get("type", name)).lower()
    if remote_type == "github":
        return _github_authenticated()

    token_env = remote_config.get("token_env") or remote_config.get("credential_env")
    if isinstance(token_env, str) and token_env.strip():
        return bool(os.environ.get(token_env.strip()))
    return False


def _github_authenticated() -> bool:
    """Check GitHub token variables or the local ``gh`` authentication state."""
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("ROHRPOST_GITHUB_TOKEN"):
        return True
    if shutil.which("gh") is None:
        return False
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
        return False
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _print_report(findings: list[Finding]) -> None:
    all_ok = all(f.ok for f in findings)
    banner = "rp doctor: all clear" if all_ok else "rp doctor: problems found"
    print(banner)
    for f in findings:
        mark = "ok " if f.ok else "XX "
        print(f"  [{mark}] {f.check}: {f.detail}")
    if all_ok:
        print("Nothing stuck in the tube.")
    else:
        bad = [f for f in findings if not f.ok]
        plural = "checks" if len(bad) != 1 else "check"
        print(f"{len(bad)} {plural} need attention.")


__all__ = ["Finding", "run"]
