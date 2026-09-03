"""Differential conformance suite: the native `rp` against the Python reference.

Every test runs the same command sequence against two implementations in two
freshly initialised repositories and compares exit codes, stdout, stderr and
the resulting event log after normalising the values that are random or
clock-driven (ticket ids, ULIDs, timestamps, temp paths). The Python package in
``src/rohrpost`` is the frozen oracle; the native binary is the tool under test.

Configuration:

* ``RP_NATIVE`` — path to the native binary (default: the newest ``build/**/rp``).
* ``RP_REFERENCE`` — optional command prefix for the reference; defaults to the
  current interpreter running ``rohrpost.cli.main``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

_ULID_RE = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{26}\b")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
_TICKET_RE = re.compile(r"\b[0-9a-hjkmnp-tv-z]{6}\b")


def _find_native() -> str:
    explicit = os.environ.get("RP_NATIVE")
    if explicit:
        return explicit
    candidates = sorted(ROOT.glob("build/**/rp"), key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = [c for c in candidates if c.is_file() and os.access(c, os.X_OK)]
    if sys.platform == "win32":
        candidates = sorted(
            ROOT.glob("build/**/rp.exe"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    if not candidates:
        pytest.skip(
            "no native rp binary found; set RP_NATIVE or build with cmake", allow_module_level=True
        )
    return str(candidates[0])


def _reference_command() -> list[str]:
    explicit = os.environ.get("RP_REFERENCE")
    if explicit:
        return explicit.split()
    return [sys.executable, "-c", "import sys; from rohrpost.cli import main; sys.exit(main())"]


@dataclass(frozen=True)
class Result:
    code: int
    stdout: bytes
    stderr: bytes

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.stdout)


@dataclass
class Impl:
    """One implementation bound to one repository."""

    name: str
    command: list[str]
    repo: Path
    env_extra: dict[str, str] = field(default_factory=dict)

    @property
    def rohrpost_dir(self) -> Path:
        return self.repo / ".rohrpost"

    def run(
        self,
        *args: str,
        stdin: bytes | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> Result:
        environment: dict[str, str] = dict(os.environ)
        environment.update(self.env_extra)
        environment.update(env or {})
        environment.setdefault("COLUMNS", "80")
        environment.pop("ROHRPOST_ACTOR", None)
        environment.pop("ROHRPOST_RUNNER", None)
        environment.pop("ROHRPOST_BATCH", None)
        environment.pop("NO_COLOR", None)
        environment.pop("CLICOLOR", None)
        command: list[str] = [*self.command, *args]
        workdir: Path = cwd if cwd is not None else self.repo
        payload: bytes = stdin if stdin is not None else b""
        proc = subprocess.run(
            command,
            cwd=workdir,
            input=payload,
            capture_output=True,
            env=environment,
            check=False,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def ticket_ids(self) -> list[str]:
        """Bare ticket ids in order of first appearance in the log."""
        seen: list[str] = []
        log = self.rohrpost_dir / "log.jsonl"
        if not log.is_file():
            return seen
        for line in log.read_bytes().decode("utf-8").splitlines():
            if not line.strip():
                continue
            tid = json.loads(line)["ticket"]
            if tid not in seen and tid != "__sync__":
                seen.append(tid)
        return seen

    def log_lines(self) -> list[str]:
        log = self.rohrpost_dir / "log.jsonl"
        return log.read_bytes().decode("utf-8").splitlines() if log.is_file() else []


class Normalizer:
    """Replace random / clock-driven values with stable placeholders."""

    def __init__(self, impl: Impl) -> None:
        self.impl = impl
        self.ulids: dict[str, str] = {}
        self.timestamps: dict[str, str] = {}

    def _token(self, table: dict[str, str], key: str, prefix: str) -> str:
        if key not in table:
            table[key] = f"<{prefix}{len(table) + 1}>"
        return table[key]

    def __call__(self, text: str) -> str:
        repo = str(self.impl.repo)
        text = text.replace(json.dumps(repo)[1:-1], "<REPO>").replace(repo, "<REPO>")
        text = _ULID_RE.sub(lambda m: self._token(self.ulids, m.group(0), "U"), text)
        text = _TS_RE.sub(lambda m: self._token(self.timestamps, m.group(0), "TS"), text)
        for index, tid in enumerate(self.impl.ticket_ids(), start=1):
            text = re.sub(rf"\b{tid}\b", f"<T{index}>", text)
        text = re.sub(r'("fold_ms": )[0-9.]+', r"\1<MS>", text)
        text = re.sub(r"cold fold: [0-9.]+ ms", "cold fold: <MS> ms", text)
        return text


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "conformance@rohrpost.local"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Conformance"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)


@dataclass
class Pair:
    """The reference and the native implementation, each on its own repo."""

    reference: Impl
    native: Impl

    @property
    def both(self) -> tuple[Impl, Impl]:
        return (self.reference, self.native)

    def run(
        self, *args: str, stdin: bytes | None = None, env: dict[str, str] | None = None
    ) -> tuple[Result, Result]:
        return (
            self.reference.run(*args, stdin=stdin, env=env),
            self.native.run(*args, stdin=stdin, env=env),
        )

    def same(
        self,
        *args: str,
        stdin: bytes | None = None,
        env: dict[str, str] | None = None,
        check_log: bool = True,
    ) -> Result:
        """Run on both, assert identical (normalised) behaviour, return the native result."""
        ref, nat = self.run(*args, stdin=stdin, env=env)
        norm_ref = Normalizer(self.reference)
        norm_nat = Normalizer(self.native)
        label = " ".join(args)
        assert nat.code == ref.code, (
            f"exit code differs for `rp {label}`:\nreference stderr: {ref.err}\nnative stderr: {nat.err}"
        )
        assert norm_nat(nat.out) == norm_ref(ref.out), f"stdout differs for `rp {label}`"
        assert norm_nat(nat.err) == norm_ref(ref.err), f"stderr differs for `rp {label}`"
        if check_log:
            assert [norm_nat(line) for line in self.native.log_lines()] == [
                norm_ref(line) for line in self.reference.log_lines()
            ], f"event log differs after `rp {label}`"
        return nat

    def new(self, *args: str) -> tuple[str, str]:
        """Create a ticket on both; return the two bare ids (reference, native)."""
        ref, nat = self.run("new", *args, "--json")
        assert ref.code == 0, ref.err
        assert nat.code == 0, nat.err
        return ref.json()["id"].split("-")[1], nat.json()["id"].split("-")[1]

    def each(
        self, ids: tuple[str, str], *args: str, stdin: bytes | None = None, check_log: bool = True
    ) -> Result:
        """Run a command whose first argument is a ticket id, per implementation."""
        ref = self.reference.run(args[0], ids[0], *args[1:], stdin=stdin)
        nat = self.native.run(args[0], ids[1], *args[1:], stdin=stdin)
        norm_ref = Normalizer(self.reference)
        norm_nat = Normalizer(self.native)
        label = " ".join(args)
        assert nat.code == ref.code, (
            f"exit code differs for `rp {label}`:\nreference stderr: {ref.err}\nnative stderr: {nat.err}"
        )
        assert norm_nat(nat.out) == norm_ref(ref.out), f"stdout differs for `rp {label}`"
        assert norm_nat(nat.err) == norm_ref(ref.err), f"stderr differs for `rp {label}`"
        if check_log:
            assert [norm_nat(line) for line in self.native.log_lines()] == [
                norm_ref(line) for line in self.reference.log_lines()
            ], f"event log differs after `rp {label}`"
        return nat


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test here `conformance` so the default gate can deselect the suite."""
    for item in items:
        if "conformance" in str(item.path):
            item.add_marker(pytest.mark.conformance)


@pytest.fixture(scope="session")
def native_command() -> list[str]:
    return [_find_native()]


@pytest.fixture(scope="session")
def reference_command() -> list[str]:
    return _reference_command()


@pytest.fixture
def bare_pair(tmp_path: Path, native_command: list[str], reference_command: list[str]) -> Pair:
    """Two git repositories without `.rohrpost/`, one per implementation."""
    # Same basename on both sides: `rp init` proposes the prefix from it.
    ref_repo = tmp_path / "reference" / "repo"
    nat_repo = tmp_path / "native" / "repo"
    _git_repo(ref_repo)
    _git_repo(nat_repo)
    return Pair(
        Impl("reference", reference_command, ref_repo), Impl("native", native_command, nat_repo)
    )


@pytest.fixture
def pair(bare_pair: Pair) -> Pair:
    """Both repositories initialised by their own implementation with prefix TST."""
    for impl in bare_pair.both:
        result = impl.run("init", "--prefix", "TST")
        assert result.code == 0, result.err
    return bare_pair


@pytest.fixture
def shared_repo(tmp_path: Path, native_command: list[str], reference_command: list[str]) -> Pair:
    """Both implementations pointed at the SAME repository (interoperability)."""
    repo = tmp_path / "shared"
    _git_repo(repo)
    reference = Impl("reference", reference_command, repo)
    native = Impl("native", native_command, repo)
    result = reference.run("init", "--prefix", "TST")
    assert result.code == 0, result.err
    return Pair(reference, native)


@pytest.fixture
def fake_gh(tmp_path: Path) -> Path:
    """A fake `gh` on PATH that serves issues from a JSON file and records calls.

    ``GH_STATE`` points at a JSON file ``{"issues": {ref: issue}, "calls": []}``.
    """
    script = tmp_path / "fakebin" / ("gh.cmd" if sys.platform == "win32" else "gh")
    script.parent.mkdir(parents=True, exist_ok=True)
    driver = tmp_path / "fakebin" / "fake_gh.py"
    driver.write_text(
        """
import json, os, sys
state_path = os.environ["GH_STATE"]
with open(state_path, "r", encoding="utf-8") as fh:
    state = json.load(fh)
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def save():
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
if args[:2] == ["auth", "status"]:
    save(); sys.exit(0)
if args[0] != "api":
    save(); sys.exit(1)
method = "GET"
rest = args[1:]
if rest[:1] == ["-X"]:
    method = rest[1]; rest = rest[2:]
path = rest[0]
ref = path.rsplit("/", 1)[-1]
fields = rest[1:]
issue = state["issues"].get(ref)
if issue is None:
    save(); sys.stderr.write("gh: Not Found (HTTP 404)\\n"); sys.exit(1)
if method == "PATCH":
    i = 0
    labels = None
    while i < len(fields):
        assert fields[i] == "-f"
        key, value = fields[i + 1].split("=", 1)
        if key.endswith("[]"):
            labels = (labels or []) + [value]
        else:
            issue[key] = value
        i += 2
    if labels is not None:
        issue["labels"] = [{"name": n} for n in labels]
save()
sys.stdout.write(json.dumps(issue))
""",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        script.write_text(f'@echo off\r\n"{sys.executable}" "{driver}" %*\r\n', encoding="utf-8")
    else:
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{driver}" "$@"\n', encoding="utf-8")
        script.chmod(0o755)
    return script.parent


def gh_state(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(state, dict)
    return state


def write_gh_state(path: Path, issues: dict[str, Any]) -> None:
    path.write_text(json.dumps({"issues": issues, "calls": []}), encoding="utf-8")


def copy_fixture_repo(impl: Impl, source_log: Path, config_text: str) -> None:
    """Replace an implementation's store with a fixed log + config (for replay tests)."""
    shutil.copyfile(source_log, impl.rohrpost_dir / "log.jsonl")
    (impl.rohrpost_dir / "config.toml").write_text(config_text, encoding="utf-8", newline="\n")
    snapshot = impl.rohrpost_dir / "tickets.jsonl"
    if snapshot.exists():
        snapshot.unlink()
