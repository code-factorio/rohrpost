"""--body-file: a path or `-` for stdin, strict UTF-8, on new/comment/set."""

from __future__ import annotations

from pathlib import Path

from conformance.conftest import Pair


def test_new_body_file_path(pair: Pair, tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_bytes(b"## Context\r\n\r\nline two\n")
    pair.same("new", "t", "--body-file", str(body), "--json")


def test_new_body_file_stdin(pair: Pair) -> None:
    pair.same("new", "t", "--body-file", "-", "--json", stdin="café ☕\n".encode())


def test_new_body_file_empty(pair: Pair, tmp_path: Path) -> None:
    body = tmp_path / "empty.md"
    body.write_bytes(b"")
    pair.same("new", "t", "--body-file", str(body), "--json")


def test_new_body_file_errors(pair: Pair, tmp_path: Path) -> None:
    latin = tmp_path / "latin1.md"
    latin.write_bytes("caf\xe9".encode("latin-1"))
    pair.same("new", "t", "--body-file", str(latin))
    truncated = tmp_path / "trunc.md"
    truncated.write_bytes(b"ok \xf0\x9f\x8e")
    pair.same("new", "t", "--body-file", str(truncated))
    bad_start = tmp_path / "start.md"
    bad_start.write_bytes(b"\x80abc")
    pair.same("new", "t", "--body-file", str(bad_start))
    pair.same("new", "t", "--body-file", str(tmp_path / "missing.md"))
    pair.same("new", "t", "--body-file", str(tmp_path))
    pair.same("new", "t", "--body", "x", "--body-file", str(latin))


def test_comment_and_set_body_file(pair: Pair, tmp_path: Path) -> None:
    ids = pair.new("t", "--body", "old")
    note = tmp_path / "note.md"
    note.write_bytes(b"retried, still 429s\nwith detail")
    pair.each(ids, "comment", "--body-file", str(note))
    pair.each(ids, "comment", "--body-file", "-", stdin=b"piped note")
    pair.each(ids, "comment", "a note", "--body-file", str(note))
    pair.each(ids, "comments", "--json")
    body = tmp_path / "body.md"
    body.write_bytes(b"## Decision\n\nuse a flag\n")
    pair.each(ids, "set", "status=in_progress", "--body-file", str(body))
    pair.each(ids, "set", "body=inline", "--body-file", str(body))
    empty = tmp_path / "empty.md"
    empty.write_bytes(b"")
    pair.each(ids, "set", "--body-file", str(empty))
    pair.each(ids, "show", "--json")
    pair.each(ids, "set", "--body-file", "-", stdin=b"from stdin")
    pair.each(ids, "show")
