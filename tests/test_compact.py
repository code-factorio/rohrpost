"""Focused tests for :mod:`rohrpost.compact`."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

from rohrpost import api, compact, paths
from rohrpost.events import decode_line, encode


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _commit(repo: Path, msg: str = "snap") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg, "--allow-empty")


def _on_clean_main(repo_root: Path) -> None:
    """Put the repo on branch ``main`` with the current tree committed (clean)."""
    _git(repo_root, "symbolic-ref", "HEAD", "refs/heads/main")
    _commit(repo_root, "initial")


def _new(repo: Path, title: str = "t") -> str:
    return api.create_ticket(repo, title, actor="user/x").ticket.id


def test_compact_refuses_on_non_main_branch(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    _on_clean_main(repo_root)
    _git(repo_root, "checkout", "-q", "-b", "feature")
    assert compact.run(tmp_repo) == 1


def test_compact_honors_configured_default_branch(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    paths.config_path(tmp_repo).write_text(
        '[project]\nprefix = "TST"\ndefault_branch = "trunk"\n'
    )
    _git(repo_root, "symbolic-ref", "HEAD", "refs/heads/trunk")
    _commit(repo_root, "initial")
    assert compact.run(tmp_repo) == 0


def test_compact_refuses_on_dirty_tree(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    _on_clean_main(repo_root)
    _new(tmp_repo, "uncommitted")  # modifies the tracked log -> dirty
    _ = _git  # keep helper referenced
    assert compact.run(tmp_repo) == 1


def test_compact_force_bypasses_guard(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    _on_clean_main(repo_root)
    _git(repo_root, "checkout", "-q", "-b", "feature")
    assert compact.run(tmp_repo, force=True) == 0


def test_compact_archives_old_terminal_events(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    _on_clean_main(repo_root)
    old_id = _new(tmp_repo, "old")
    young_id = _new(tmp_repo, "young")
    api.close(tmp_repo, old_id, actor="user/x")
    _commit(repo_root, "tickets")  # clean tree so the guard passes

    _backdate_all(tmp_repo, days_ago=120)
    _commit(repo_root, "backdated")

    assert compact.run(tmp_repo, now=dt.datetime.now(dt.UTC)) == 0
    assert paths.archive_files(tmp_repo)
    remaining = _read_log_tickets(tmp_repo)
    assert old_id not in remaining
    assert young_id in remaining


def test_compact_keeps_recently_terminal(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    _on_clean_main(repo_root)
    tid = _new(tmp_repo, "recent")
    api.close(tmp_repo, tid, actor="user/x")  # closed just now -> not archivable
    _commit(repo_root, "tickets")
    n_before = len(_read_log_tickets(tmp_repo))
    compact.run(tmp_repo, now=dt.datetime.now(dt.UTC))
    assert len(_read_log_tickets(tmp_repo)) == n_before


def test_compact_invalidates_snapshot(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    _on_clean_main(repo_root)
    _new(tmp_repo)
    _new(tmp_repo, "second")
    _commit(repo_root, "tickets")
    api.show_ticket(tmp_repo, api.create_ticket(tmp_repo, "x", actor="u").ticket.id)
    assert paths.snapshot_path(tmp_repo).is_file()
    _commit(repo_root, "snap")
    compact.run(tmp_repo, force=True)
    assert not paths.snapshot_path(tmp_repo).is_file()


def test_compact_json_output(tmp_repo: Path) -> None:
    repo_root = tmp_repo.parent
    _on_clean_main(repo_root)
    _new(tmp_repo)
    _commit(repo_root, "x")
    import io
    import json
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = compact.run(tmp_repo, force=True, json_output=True)
    finally:
        sys.stdout = old
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert "archived" in payload
    assert "remaining" in payload


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _read_log_tickets(repo: Path) -> list[str]:
    lines = [ln for ln in paths.log_path(repo).read_text().splitlines() if ln.strip()]
    return [decode_line(ln).ticket for ln in lines]


def _backdate_all(repo: Path, *, days_ago: int) -> None:
    """Shift every event timestamp back by ``days_ago``, preserving relative order."""
    delta = dt.timedelta(days=days_ago)
    log = paths.log_path(repo)
    out: list[str] = []
    for ln in log.read_text().splitlines():
        if not ln.strip():
            continue
        ev = decode_line(ln)
        shifted = (
            (datetime_from_ts(ev.ts) - delta)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        ev = ev.__class__(
            id=ev.id,
            ts=shifted,
            ticket=ev.ticket,
            op=ev.op,
            actor=ev.actor,
            set=ev.set,
            text=ev.text,
            remote=ev.remote,
            ref=ev.ref,
            reason=ev.reason,
        )
        out.append(encode(ev).decode())
    log.write_text("\n".join(out) + "\n")


def datetime_from_ts(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
