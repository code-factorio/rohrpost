"""``text eol=lf`` protection for the JSONL event store (spec §4, RP-x3f018).

Per docs/research/windows-git-jsonl.md: Git for Windows ships ``core.autocrlf=true``
by default, which checks a committed LF log out as CRLF, and ``os.write`` appends
put CRLF lines into the working tree on Windows. ``text eol=lf`` normalises every
checkin to an LF blob and checks the file out with the blob's exact bytes on any
platform, so cross-machine clones are byte-identical and ``merge=union`` always
compares LF lines. The tests drive a real git repo; git's attribute semantics are
platform-independent, so pinning them on Linux pins them for Windows too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rohrpost import api, paths, store
from rohrpost.events import Event, encode

# A stable ULID and timestamp so the round-trip asserts exact bytes.
_EID: str = "01K2X8P4RQ7YFZ3M9NVB6TDHW1"
_TS: str = "2026-08-11T09:00:00.000Z"


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, check=True, capture_output=True, text=True
    )
    return (result.stdout or "").strip()


def test_jsonl_paths_carry_merge_union_and_text_eol_lf(tmp_repo: Path) -> None:
    """The attribute set git actually resolves for both JSONL patterns."""
    repo_root = tmp_repo.parent
    (paths.archive_dir(tmp_repo) / "2026.jsonl").touch()
    for rel in (".rohrpost/log.jsonl", ".rohrpost/archive/2026.jsonl"):
        out = _git(repo_root, "check-attr", "merge", "text", "eol", "--", rel)
        attrs = {parts[1]: parts[2] for parts in (line.split(": ", 2) for line in out.splitlines())}
        assert attrs == {"merge": "union", "text": "set", "eol": "lf"}


def test_log_round_trips_byte_identically_under_autocrlf_true(tmp_repo: Path) -> None:
    """CRLF in → LF blob → LF checkout, with ``core.autocrlf=true`` set.

    This is the Windows default: without the attribute the checkout step would
    hand back CRLF and a Linux clone would hold different bytes.
    """
    repo_root = tmp_repo.parent
    _git(repo_root, "config", "core.autocrlf", "true")
    log = paths.log_path(tmp_repo)

    ev = Event(id=_EID, ts=_TS, ticket="aaaaaa", op="set", actor="user/x", set={"status": "open"})
    # Simulate a foreign writer that left CRLF endings in the working tree.
    log.write_bytes(encode(ev) + b"\r\n")

    # Checkin: the `text` part normalises the staged/committed blob to LF.
    _git(repo_root, "add", ".rohrpost/log.jsonl")
    _git(repo_root, "commit", "-qm", "crlf log")
    blob = subprocess.run(
        ["git", "cat-file", "-p", "HEAD:.rohrpost/log.jsonl"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout
    assert blob == encode(ev) + b"\n"

    # Checkout: the eol=lf part reproduces the blob's exact bytes in the worktree.
    log.unlink()
    _git(repo_root, "checkout", "--", ".rohrpost/log.jsonl")
    assert log.read_bytes() == encode(ev) + b"\n"

    # The fold over the checked-out bytes is identical to folding the event itself.
    assert store.read_events(tmp_repo) == [ev]


def test_init_appends_eol_rules_to_a_pre_eol_gitattributes(tmp_path: Path) -> None:
    """A repo initialised before the eol rules gets them appended, idempotently.

    The stale ``merge=union``-only lines stay; gitattributes resolves overlapping
    matches per attribute with later lines winning, so the appended rules upgrade
    the file without touching user content.
    """
    pre_eol = [rule.replace(" text eol=lf", "") for rule in paths.GITATTRIBUTES_RULES]
    (tmp_path / ".gitattributes").write_text("\n".join(pre_eol) + "\n", encoding="utf-8")

    result = api.init_repo(tmp_path, prefix="TST")
    assert result.updated_gitattributes
    text = (tmp_path / ".gitattributes").read_text(encoding="utf-8")
    for line in (*pre_eol, *paths.GITATTRIBUTES_RULES):
        assert line in text
    assert paths.write_gitattributes(tmp_path) is False  # second run: no-op


def test_this_repo_ships_every_rule_in_its_own_gitattributes() -> None:
    """paths.py and this repo's .gitattributes cannot drift apart."""
    repo_root = Path(__file__).resolve().parent.parent
    text = (repo_root / ".gitattributes").read_text(encoding="utf-8")
    for rule in paths.GITATTRIBUTES_RULES:
        assert rule in text
