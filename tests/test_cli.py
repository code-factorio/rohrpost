"""Tests for :mod:`rohrpost.cli`."""

from __future__ import annotations

import pytest

from rohrpost import __version__
from rohrpost.cli import _COMMANDS, main


def test_version_prints_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    out, _ = capsys.readouterr()
    assert f"rohrpost {__version__}" in out


def test_no_command_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out, _ = capsys.readouterr()
    assert "usage:" in out


def test_help_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out, _ = capsys.readouterr()
    assert "<command>" in out


@pytest.mark.parametrize("command", _COMMANDS)
def test_spec_commands_are_scaffolded_and_report_unimplemented(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every command from spec §10 is registered; none are implemented yet, so
    # each reports to stderr and exits non-zero rather than silently no-op'ing.
    assert main([command]) == 2
    _, err = capsys.readouterr()
    assert command in err
    assert "not implemented" in err
