"""Windows wrapper templates behave as documented (RP-rf1841).

The ``.ps1`` and ``.cmd`` templates are the Windows entry points taught by the
rohrpost skill; the skill also routes multi-line bodies through ``--body-file``.
Structural invariants are pinned on every platform. The runtime tests execute
the materialised wrappers for real wherever an interpreter is available: pwsh
is preinstalled on all three CI operating systems, cmd only on Windows. The
POSIX runtime variants use a fake ``rp.exe`` (a bash script the wrapper execs
directly); the Windows variants copy the real trampoline ``rp.exe`` that
``uv sync`` builds next to the interpreter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "rohrpost"
PS1_TEMPLATE = SKILL_DIR / "scripts" / "rohrpost.ps1.template"
CMD_TEMPLATE = SKILL_DIR / "scripts" / "rohrpost.cmd.template"
POINTER = "Load .agents/skills/rohrpost/playbooks/install-local.md to provision it"

PWSH: str | None = shutil.which("pwsh") or shutil.which("powershell")

needs_pwsh = pytest.mark.skipif(PWSH is None, reason="no PowerShell interpreter on PATH")
needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="the fake rp.exe is a bash script")
needs_windows = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")


def test_ps1_template_keeps_its_contract() -> None:
    text = PS1_TEMPLATE.read_text(encoding="utf-8")
    assert "$env:ROHRPOST_HOME" in text
    assert "Join-Path $env:LOCALAPPDATA 'rohrpost'" in text
    assert text.index("'.venv\\Scripts\\rp.exe'") < text.index("'.venv\\Scripts\\rp'")
    assert POINTER in text
    assert "uvx" not in text
    assert "uv run" not in text


def test_cmd_template_keeps_its_contract() -> None:
    text = CMD_TEMPLATE.read_text(encoding="utf-8")
    assert "ROHRPOST_HOME" in text
    assert "%LOCALAPPDATA%\\rohrpost" in text
    assert text.index("Scripts\\rp.exe") < text.index('Scripts\\rp"')
    assert POINTER in text
    assert "uvx" not in text
    assert "uv run" not in text


def test_skill_and_playbooks_document_the_windows_route() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "rohrpost.ps1" in skill
    assert "rohrpost.cmd" in skill
    assert "--body-file" in skill
    install_local = (SKILL_DIR / "playbooks" / "install-local.md").read_text(encoding="utf-8")
    assert "playbooks/windows.md" in install_local


def _fake_rp(scripts: Path, exit_code: int = 0) -> None:
    """A POSIX rp.exe stand-in that reports cwd and args, then exits."""
    fake = scripts / "rp.exe"
    fake.write_text(
        f'#!/usr/bin/env bash\nprintf "cwd=%s args=%s\\n" "$PWD" "$*"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _real_rp_exe() -> Path:
    """The trampoline rp.exe that uv sync installs beside the interpreter."""
    real = Path(sys.executable).with_name("rp.exe")
    if not real.is_file():
        pytest.skip("the project's rp.exe is not built next to the interpreter")
    return real


def _materialise(template: Path, workdir: Path) -> Path:
    """Copy a template to its generated name, as the install playbooks do."""
    generated = workdir / template.name.replace(".template", "")
    shutil.copyfile(template, generated)
    return generated


def _env(
    tmp_path: Path, rohrpost_home: Path | None, localappdata: Path | None = None
) -> dict[str, str]:
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(localappdata or tmp_path / "no-install-here")
    if rohrpost_home is None:
        env.pop("ROHRPOST_HOME", None)
    else:
        env["ROHRPOST_HOME"] = str(rohrpost_home)
    return env


def _run_pwsh(
    script: Path, cwd: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    assert PWSH is not None
    return subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _run_cmd(
    script: Path, cwd: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["cmd", "/d", "/c", str(script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


@needs_pwsh
@needs_posix
def test_ps1_wrapper_runs_the_install_from_rohrpost_home(tmp_path: Path) -> None:
    scripts = tmp_path / "home" / "src" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    _fake_rp(scripts)
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    wrapper = _materialise(PS1_TEMPLATE, tmp_path)

    result = _run_pwsh(
        wrapper, caller, _env(tmp_path, rohrpost_home=tmp_path / "home"), "doctor", "--json"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"cwd={caller} args=doctor --json\n"


@needs_pwsh
@needs_posix
def test_ps1_wrapper_defaults_to_localappdata_home(tmp_path: Path) -> None:
    local = tmp_path / "AppData" / "Local"
    scripts = local / "rohrpost" / "src" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    _fake_rp(scripts)
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    wrapper = _materialise(PS1_TEMPLATE, tmp_path)

    result = _run_pwsh(
        wrapper, caller, _env(tmp_path, rohrpost_home=None, localappdata=local), "doctor", "--json"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"cwd={caller} args=doctor --json\n"


@needs_windows
def test_ps1_wrapper_runs_the_real_rp_from_rohrpost_home(tmp_path: Path) -> None:
    scripts = tmp_path / "home" / "src" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(_real_rp_exe(), scripts / "rp.exe")
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    wrapper = _materialise(PS1_TEMPLATE, tmp_path)

    result = _run_pwsh(
        wrapper, caller, _env(tmp_path, rohrpost_home=tmp_path / "home"), "--version"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("rp")


@needs_pwsh
def test_ps1_wrapper_reports_missing_source(tmp_path: Path) -> None:
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    wrapper = _materialise(PS1_TEMPLATE, tmp_path)

    result = _run_pwsh(
        wrapper, caller, _env(tmp_path, rohrpost_home=tmp_path / "missing"), "doctor"
    )

    assert result.returncode == 1
    assert "source checkout is missing" in result.stderr
    assert POINTER in result.stderr


@needs_pwsh
def test_ps1_wrapper_reports_missing_executable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "src").mkdir(parents=True)
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    wrapper = _materialise(PS1_TEMPLATE, tmp_path)

    result = _run_pwsh(wrapper, caller, _env(tmp_path, rohrpost_home=home), "doctor")

    assert result.returncode == 1
    assert "local executable is missing" in result.stderr
    assert POINTER in result.stderr


@needs_pwsh
def test_ps1_wrapper_passes_exit_code_through(tmp_path: Path) -> None:
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    wrapper = _materialise(PS1_TEMPLATE, tmp_path)
    if sys.platform == "win32":
        scripts = tmp_path / "home" / "src" / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(_real_rp_exe(), scripts / "rp.exe")
        args: tuple[str, ...] = ("--no-such-flag",)
        expected = 2
    else:
        scripts = tmp_path / "home" / "src" / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        _fake_rp(scripts, exit_code=7)
        args = ("doctor",)
        expected = 7

    result = _run_pwsh(wrapper, caller, _env(tmp_path, rohrpost_home=tmp_path / "home"), *args)

    assert result.returncode == expected


@needs_windows
def test_cmd_wrapper_runs_the_real_rp_from_rohrpost_home(tmp_path: Path) -> None:
    scripts = tmp_path / "home" / "src" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(_real_rp_exe(), scripts / "rp.exe")
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    wrapper = _materialise(CMD_TEMPLATE, tmp_path)

    result = _run_cmd(wrapper, caller, _env(tmp_path, rohrpost_home=tmp_path / "home"), "--version")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("rp")


@needs_windows
def test_cmd_wrapper_reports_missing_source(tmp_path: Path) -> None:
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    wrapper = _materialise(CMD_TEMPLATE, tmp_path)

    result = _run_cmd(wrapper, caller, _env(tmp_path, rohrpost_home=tmp_path / "missing"), "doctor")

    assert result.returncode == 1
    assert "source checkout is missing" in result.stderr
    assert POINTER in result.stderr
