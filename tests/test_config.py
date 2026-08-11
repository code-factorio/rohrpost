"""Focused tests for :mod:`rohrpost.config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from rohrpost.config import (
    DEFAULT_PREFIX,
    default_config,
    load_config,
    render_config_toml,
    validate_prefix,
)
from rohrpost.exceptions import ConfigError


def test_validate_prefix_uppercases_and_accepts_valid() -> None:
    assert validate_prefix("fac") == "FAC"
    assert validate_prefix("  ab  ") == "AB"
    assert validate_prefix("ABCDE") == "ABCDE"


@pytest.mark.parametrize("bad", ["A", "FACD6", "fac-", "ABCDEF", "", "F C"])
def test_validate_prefix_rejects_invalid(bad: str) -> None:
    with pytest.raises(ConfigError):
        validate_prefix(bad)


def test_missing_config_yields_default(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.prefix == DEFAULT_PREFIX
    assert cfg.remotes == {}


def test_default_config_uses_default_prefix() -> None:
    assert default_config().prefix == DEFAULT_PREFIX


def test_load_reads_prefix(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[project]\nprefix = "FAC"\n')
    assert load_config(tmp_path).prefix == "FAC"


def test_load_reads_remotes_table(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[project]\nprefix = "FAC"\n'
        '[remotes.github]\nurl = "https://api.github.com"\nrepo = "o/n"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.remotes["github"]["repo"] == "o/n"


def test_load_raises_on_malformed_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("this is = = not toml")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_load_raises_on_bad_prefix_value(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[project]\nprefix = "X"\n')  # 1 letter
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_render_config_toml_contains_prefix() -> None:
    text = render_config_toml("FAC")
    assert 'prefix = "FAC"' in text
    assert "[project]" in text
