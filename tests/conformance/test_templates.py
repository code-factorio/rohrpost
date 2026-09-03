"""Ticket templates: `.rohrpost/templates/<name>.toml` defaults and validation."""

from __future__ import annotations

import pytest

from conformance.conftest import Pair


def _write_template(pair: Pair, name: str, text: str) -> None:
    for impl in pair.both:
        (impl.rohrpost_dir / "templates" / name).write_text(text, encoding="utf-8", newline="\n")


def test_template_sections_and_overrides(pair: Pair) -> None:
    _write_template(
        pair,
        "bug.toml",
        '[defaults]\ntype = "bug"\npriority = 1\nlabels = ["auth", " ui "]\nbody = "template body"\n',
    )
    pair.same("new", "A bug", "--template", "bug", "--json")
    pair.same(
        "new",
        "Override",
        "--template",
        "bug",
        "--type",
        "spike",
        "-p",
        "3",
        "--label",
        "x",
        "--body",
        "explicit",
        "--json",
    )
    pair.same("new", "By filename", "--template", "bug.toml", "--json")
    _write_template(
        pair,
        "top.toml",
        'type = "epic"\nassignee = "runner/t"\nlabels = "single"\n[fields]\npriority = 0\n[ticket]\ntitle = "ignored"\n',
    )
    pair.same("new", "Top level", "--template", "top", "--json")
    pair.same(
        "new",
        "Body file wins",
        "--template",
        "bug",
        "--body-file",
        "-",
        "--json",
        stdin=b"explicit body",
    )


@pytest.mark.parametrize(
    "text",
    [
        "unknown = 1\n",
        '[defaults]\npriority = "high"\n',
        "[defaults]\npriority = true\n",
        "[defaults]\nlabels = [1]\n",
        '[defaults]\nlabels = ["  "]\n',
        "[defaults]\ntitle = 3\n",
        "[defaults]\nparent = 5\n",
        '[defaults]\nparent = "bogus"\n',
        '[defaults]\nblocked_by = ["bogus"]\n',
        "defaults = 3\n",
        "this is not toml\n",
    ],
    ids=lambda t: t.strip().replace("\n", ";")[:40],
)
def test_template_validation_errors(pair: Pair, text: str) -> None:
    _write_template(pair, "t.toml", text)
    ref, nat = pair.run("new", "x", "--template", "t")
    assert nat.code == ref.code == 1
    # Parser-originated messages (tomllib vs toml++) may differ in wording; the
    # rp-originated ones must not.
    if "not toml" not in text:
        assert nat.err == ref.err


def test_template_name_errors(pair: Pair) -> None:
    pair.same("new", "x", "--template", "missing")
    pair.same("new", "x", "--template", " ")
    pair.same("new", "x", "--template", "../config")
    pair.same("new", "x", "--template", "sub/dir")
