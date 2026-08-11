"""Filesystem layout: locating the ``.rohrpost/`` directory and its members.

The on-disk layout is fixed by spec §4. Everything lives under ``.rohrpost/`` at
the repository root. This module is the single source of truth for those paths
so the rest of the code never concatenates path strings.

Discovery walks up from the current directory looking for a ``.rohrpost/``
folder (mirroring how git finds ``.git/``), so ``rp`` works from anywhere inside
a repo. Commands that mutate require an initialised repo; ``rp init`` creates
the scaffold at the git root (or the current directory outside a git repo).
"""

from __future__ import annotations

from pathlib import Path

from rohrpost.exceptions import StoreError

#: The magic directory name, committed at the repo root.
ROHRPOST_DIR_NAME: str = ".rohrpost"

#: Filenames within ``.rohrpost/``. See spec §4 for the full layout.
CONFIG_FILENAME: str = "config.toml"
LOG_FILENAME: str = "log.jsonl"
SNAPSHOT_FILENAME: str = "tickets.jsonl"
ARCHIVE_DIR_NAME: str = "archive"
SHADOW_DIR_NAME: str = "shadow"
TEMPLATES_DIR_NAME: str = "templates"
BODIES_DIR_NAME: str = "bodies"
LOCK_FILENAME: str = ".lock"

#: The committed ``.gitattributes`` merge rules from spec §4.
GITATTRIBUTES_RULES: tuple[str, ...] = (
    ".rohrpost/log.jsonl          merge=union",
    ".rohrpost/archive/*.jsonl    merge=union",
    ".rohrpost/shadow/**/*.json   merge=ours",
    ".rohrpost/tickets.jsonl      linguist-generated",
)

#: The gitignored snapshot (regenerable; spec §4 marks it GITIGNORED).
GITIGNORE_RULES: tuple[str, ...] = (".rohrpost/tickets.jsonl",)


def find_git_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default cwd) to the nearest directory containing ``.git``."""
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def find_rohrpost_dir(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` to the nearest directory containing ``.rohrpost/``."""
    here = (start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ROHRPOST_DIR_NAME).is_dir():
            return candidate / ROHRPOST_DIR_NAME
    return None


def require_rohrpost_dir(start: Path | None = None) -> Path:
    """Return the ``.rohrpost/`` dir or raise :class:`StoreError` if uninitialised."""
    found = find_rohrpost_dir(start)
    if found is None:
        raise StoreError("not a rohrpost repository (no .rohrpost/ found). Run `rp init` first.")
    return found


def rohrpost_root() -> Path:
    """The repository root that owns the ``.rohrpost/`` dir (its parent)."""
    return require_rohrpost_dir().parent


# ---------------------------------------------------------------------------
# Path accessors — keep them as functions so callers never build paths by hand.
# ---------------------------------------------------------------------------
def config_path(rohrpost_dir: Path) -> Path:
    return rohrpost_dir / CONFIG_FILENAME


def log_path(rohrpost_dir: Path) -> Path:
    return rohrpost_dir / LOG_FILENAME


def snapshot_path(rohrpost_dir: Path) -> Path:
    return rohrpost_dir / SNAPSHOT_FILENAME


def archive_dir(rohrpost_dir: Path) -> Path:
    return rohrpost_dir / ARCHIVE_DIR_NAME


def shadow_dir(rohrpost_dir: Path) -> Path:
    return rohrpost_dir / SHADOW_DIR_NAME


def templates_dir(rohrpost_dir: Path) -> Path:
    return rohrpost_dir / TEMPLATES_DIR_NAME


def bodies_dir(rohrpost_dir: Path) -> Path:
    return rohrpost_dir / BODIES_DIR_NAME


def lock_path(rohrpost_dir: Path) -> Path:
    return rohrpost_dir / LOCK_FILENAME


def archive_files(rohrpost_dir: Path) -> list[Path]:
    """Sorted list of ``archive/*.jsonl`` files (oldest first). Empty if none."""
    adir = archive_dir(rohrpost_dir)
    if not adir.is_dir():
        return []
    return sorted(adir.glob("*.jsonl"))


def ensure_layout(rohrpost_dir: Path) -> None:
    """Create the full directory scaffold if missing (idempotent)."""
    for d in (
        rohrpost_dir,
        archive_dir(rohrpost_dir),
        shadow_dir(rohrpost_dir),
        templates_dir(rohrpost_dir),
    ):
        d.mkdir(parents=True, exist_ok=True)
    log_path(rohrpost_dir).touch(exist_ok=True)


def _append_unique_lines(path: Path, lines: tuple[str, ...]) -> bool:
    """Append any of ``lines`` not already present to ``path``. Returns whether changed."""
    existing = path.read_text() if path.is_file() else ""
    new = [line for line in lines if line not in existing]
    if not new:
        return False
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(prefix + "\n".join(new) + "\n")
    return True


def write_gitattributes(repo_root: Path) -> bool:
    """Ensure the committed ``.gitattributes`` carries the union-merge rules. Idempotent."""
    return _append_unique_lines(repo_root / ".gitattributes", GITATTRIBUTES_RULES)


def write_gitignore(repo_root: Path) -> bool:
    """Ensure the snapshot is gitignored. Idempotent."""
    return _append_unique_lines(repo_root / ".gitignore", GITIGNORE_RULES)
