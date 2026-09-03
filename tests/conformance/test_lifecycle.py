"""The ticket lifecycle through every command, text and JSON, on both implementations."""

from __future__ import annotations

from conformance.conftest import Normalizer, Pair


def test_init_json_and_idempotent(bare_pair: Pair) -> None:
    bare_pair.same("init", "--prefix", "TST", "--json")
    bare_pair.same("init", "--json")  # nothing left to create
    bare_pair.same("init")
    for impl in bare_pair.both:
        assert (impl.repo / ".gitattributes").read_bytes() == (
            b".rohrpost/log.jsonl          merge=union text eol=lf\n"
            b".rohrpost/archive/*.jsonl    merge=union text eol=lf\n"
            b".rohrpost/shadow/**/*.json   merge=ours\n"
            b".rohrpost/tickets.jsonl      linguist-generated\n"
        )
        assert (impl.repo / ".gitignore").read_bytes() == b".rohrpost/tickets.jsonl\n"
    config_ref = (bare_pair.reference.rohrpost_dir / "config.toml").read_bytes()
    config_nat = (bare_pair.native.rohrpost_dir / "config.toml").read_bytes()
    assert config_ref == config_nat


def test_init_text_proposes_prefix_and_upgrades_existing_files(bare_pair: Pair) -> None:
    for impl in bare_pair.both:
        (impl.repo / ".gitattributes").write_bytes(b"*.md text\n.rohrpost/log.jsonl merge=union")
        (impl.repo / ".gitignore").write_bytes(b"build/")
    bare_pair.same("init")
    bare_pair.same("init", "--prefix", "ab")  # existing config wins; no error
    bare_pair.same("init", "--prefix", "toolong")  # existing config wins; no error either


def test_init_prefix_validation(bare_pair: Pair) -> None:
    bare_pair.same("init", "--prefix", "toolong", check_log=False)
    bare_pair.same("init", "--prefix", "a", check_log=False)
    bare_pair.same("init", "--prefix", "a1", check_log=False)


def test_create_variants(pair: Pair) -> None:
    pair.same("new", "plain")
    pair.same("new", "json", "--json")
    pair.same(
        "new",
        "typed",
        "--type",
        "bug",
        "-p",
        "1",
        "--label",
        "auth",
        "--label",
        "ui",
        "--label",
        "auth",
        "--json",
    )
    pair.same(
        "new",
        "typed epic",
        "--type",
        "epic",
        "--assignee",
        "runner/x",
        "--body",
        "some body",
        "--json",
    )
    pair.same("new", "  trimmed title  ", "--json")
    pair.same("new", "whitespace body", "--body", "   ", "--json")
    pair.same(
        "new",
        "unicode: café \U0001f389 \u2028",
        "--body",
        'line1\nline2\ttab "quoted" \\ back',
        "--json",
    )
    pair.same("new", "priority zero", "-p0", "--json")
    pair.same("new", "priority eq", "--priority=4", "--json")
    pair.same("new", "prio abbrev", "--prio", "3", "--jso")
    pair.same("new", "-x y", "--json")  # an arg with a space is positional
    pair.same("new", "-1", "--json")  # negative-number-like title is positional
    pair.same("new", "explicit actor", "--actor", "runner/claude@b-1", "--json")
    pair.same(
        "new", "env actor", "--json", env={"ROHRPOST_RUNNER": "agent", "ROHRPOST_BATCH": "b-7"}
    )
    pair.same("new", "env actor no batch", "--json", env={"ROHRPOST_RUNNER": "agent"})
    pair.same(
        "new",
        "env actor override",
        "--json",
        env={"ROHRPOST_ACTOR": "user/override", "ROHRPOST_RUNNER": "agent"},
    )
    pair.same("list", "--json")
    pair.same("list")


def test_parent_blocked_and_readiness(pair: Pair) -> None:
    epic = pair.new("Epic", "--type", "epic")
    dep = pair.new("Dependency")
    child = (
        pair.each(
            dep, "new", "Child", "--parent", "", "--label", "auth", "--body", "prose", "--json"
        )
        if False
        else None
    )
    # Build the child per implementation with the right parent/dep ids.
    child_ids = []
    for impl, e, d in zip(pair.both, epic, dep, strict=True):
        result = impl.run(
            "new",
            "Child",
            "--parent",
            e,
            "--blocked-by",
            d,
            "--label",
            "auth",
            "--body",
            "some prose body",
            "--assignee",
            "user/x",
            "--json",
        )
        assert result.code == 0, result.err
        child_ids.append(result.json()["id"].split("-")[1])
    child = tuple(child_ids)
    pair.same("ready")
    pair.same("ready", "--json")
    pair.same("ready", "--limit", "1", "--json")
    pair.same("ready", "--limit", "0", "--json")
    pair.same("ready", "--limit", "-1", "--json")
    pair.each(child, "show")
    pair.each(child, "show", "--include", "body,deps,notes,fieldts")
    pair.each(child, "show", "--include", "deps")
    pair.each(child, "show", "--include", "")
    pair.each(child, "show", "--json")
    pair.each(epic, "tree")
    pair.each(epic, "tree", "--json")
    pair.each(epic, "show")  # epic status derived from children
    pair.same("list", "--status", "open")
    pair.same("list", "--status", "ready", "--json")
    pair.same("list", "--label", "auth", "--json")
    pair.same("list", "--type", "epic", "--json")
    pair.same("list", "--match", "CHILD", "--json")
    pair.same("list", "--match", "nomatch")
    pair.same("list", "--label", "nonexistent")
    for impl, e in zip(pair.both, epic, strict=True):
        r = impl.run("list", "--parent", e, "--json")
        assert r.code == 0
    pair.each(dep, "close", "--reason", "done")
    pair.same("ready")
    pair.same("ready", "--json")
    pair.each(epic, "show", "--json")
    pair.each(child, "claim")
    pair.each(child, "claim")  # idempotent
    pair.each(child, "claim", "--json")
    pair.each(epic, "tree")
    pair.each(child, "drop", "--reason", "wontfix")
    pair.each(child, "drop")
    pair.each(epic, "show")
    pair.same("list", "--json")


def test_set_semantics(pair: Pair) -> None:
    ids = pair.new("t1", "--body", "old")
    pair.each(ids, "set", "status=review", "priority=1")
    pair.each(ids, "set", "status=review")  # no change
    pair.each(ids, "set", "labels+=auth,bug", "labels-=spike")
    pair.each(ids, "set", "labels+=auth")  # no change
    pair.each(ids, "set", "labels-=auth,bug,zzz", "--json")
    pair.each(
        ids, "set", "title=renamed", "type=spike", "assignee=user/y", "body=new body", "--json"
    )
    pair.each(ids, "set", "body=")  # empty body clears
    pair.each(ids, "show", "--json")
    pair.each(ids, "set", "--json", "status=waiting")  # option before positional chunk
    pair.each(ids, "set", "status=done")
    pair.each(ids, "show")
    pair.each(ids, "set", "foo=bar")
    pair.each(ids, "set", "nofield")
    pair.each(ids, "set", "priority=9")
    pair.each(ids, "set", "priority=x")
    pair.each(ids, "set", "labels=x")
    pair.each(ids, "set", "labels+=")
    pair.each(ids, "set", "status=bogus")
    pair.each(ids, "set", "type=bogus")
    pair.each(ids, "set", "parent=zz")
    pair.each(ids, "set", "=x")
    pair.each(ids, "set", " priority = 2")
    pair.each(ids, "set")
    pair.each(ids, "set", "priority=1_0")
    pair.each(ids, "set", "blocked_by+=RP-zzzzzz", "--json")
    pair.each(ids, "set", "blocked_by-=zzzzzz", "--json")


def test_close_drop_reasons_and_reopen(pair: Pair) -> None:
    ids = pair.new("t")
    pair.each(ids, "close", "--reason", "  shipped  ")
    pair.each(ids, "close", "--reason", "again")  # already closed, no event
    pair.each(ids, "show")
    pair.each(ids, "set", "status=open")
    pair.each(ids, "show", "--json")  # last_close_reason survives reopen
    pair.each(ids, "drop", "--reason", "   ")  # blank reason is dropped
    pair.each(ids, "log", "--json")
    pair.each(ids, "log")
    pair.same("log")
    pair.same("log", "--json")


def test_comments(pair: Pair) -> None:
    ids = pair.new("t")
    pair.each(ids, "comments")
    pair.each(ids, "comment", "first note")
    pair.each(ids, "comment", "--json", "second note")
    pair.each(ids, "comment", "  padded  ", "--json")
    pair.each(ids, "comment", "   ")
    pair.each(ids, "comment")
    pair.each(ids, "comments")
    pair.each(ids, "comments", "--json")
    for _ in range(12):
        pair.each(ids, "comment", "bulk")
    pair.each(ids, "show", "--include", "notes")


def test_link_unlink(pair: Pair) -> None:
    ids = pair.new("t")
    pair.each(ids, "link", "github", "42")
    pair.each(ids, "link", "github", "42", "--json")  # idempotent
    pair.each(ids, "link", "github", " 43 ", "--json")
    pair.each(ids, "link", "jira", "PROJ-1")
    pair.each(ids, "link", " ", "1")
    pair.each(ids, "show")
    pair.each(ids, "show", "--json")
    pair.each(ids, "unlink", "github")
    pair.each(ids, "unlink", "github")
    pair.each(ids, "unlink", " ")
    pair.each(ids, "log")
    pair.same("doctor")
    pair.same("doctor", "--json")


def test_doctor_stats_conflicts_resolve_compact(pair: Pair) -> None:
    pair.same("doctor")
    pair.same("doctor", "--json")
    pair.same("stats")
    pair.same("stats", "--json")
    ids = pair.new("t", "--body", "x" * 5000)
    pair.new("blocked", "--blocked-by", "zzzzzz")
    pair.same("doctor")
    pair.same("doctor", "--json")
    pair.same("stats")
    pair.same("stats", "--json")
    pair.same("conflicts")
    pair.same("conflicts", "--json")
    pair.each(ids, "resolve", "--take", "local")  # nothing flagged: no event
    pair.each(ids, "resolve")
    pair.each(ids, "set", "labels+=conflict:github")
    pair.same("conflicts")
    pair.same("conflicts", "--json")
    pair.each(ids, "resolve", "--take", "remote")
    pair.each(ids, "resolve", "--take", "local", "--json")
    pair.each(ids, "show", "--json")
    pair.same("compact")  # refuses: dirty tree
    pair.same("compact", "--json")
    pair.same("compact", "--force")
    pair.same("compact", "--force", "--json")
    pair.same("compact", "--force", "--archive-after", "0", "--json")
    pair.same("list", "--json")
    pair.same("doctor")


def test_bare_and_rendered_ids(pair: Pair) -> None:
    ids = pair.new("t")
    for impl, tid in zip(pair.both, ids, strict=True):
        for form in (tid, f"TST-{tid}", f"XX-{tid}", f"a-b-{tid}"):
            r = impl.run("show", form, "--json")
            assert r.code == 0, r.err
            assert r.json()["id"] == f"TST-{tid}"
        r = impl.run("show", f"{tid}-x")
        assert r.code == 1


def test_prefix_is_display_only(pair: Pair) -> None:
    ids = pair.new("t")
    for impl in pair.both:
        cfg = impl.rohrpost_dir / "config.toml"
        cfg.write_text('[project]\nprefix = "NEW"\n', encoding="utf-8")
    pair.each(ids, "show", "--json")
    pair.same("list")
    for impl in pair.both:
        cfg = impl.rohrpost_dir / "config.toml"
        cfg.write_text('[project]\nprefix = "bad prefix"\n', encoding="utf-8")
    pair.same("list", check_log=False)
    for impl in pair.both:
        cfg = impl.rohrpost_dir / "config.toml"
        cfg.write_text("project = 3\n", encoding="utf-8")
    pair.same("list", check_log=False)


def test_snapshot_cache_roundtrip(pair: Pair) -> None:
    ids = pair.new("t", "--body", "prose", "--label", "a")
    pair.each(ids, "comment", "note")
    pair.each(ids, "link", "github", "1")
    pair.same("list", "--json")
    snap_ref = (pair.reference.rohrpost_dir / "tickets.jsonl").read_bytes()
    snap_nat = (pair.native.rohrpost_dir / "tickets.jsonl").read_bytes()
    assert Normalizer(pair.native)(snap_nat.decode()) == Normalizer(pair.reference)(
        snap_ref.decode()
    )
    pair.same("doctor", "--json")
    # A stale snapshot is detected by both.
    for impl in pair.both:
        snap = impl.rohrpost_dir / "tickets.jsonl"
        snap.write_bytes(snap.read_bytes().replace(b'"title": "t"', b'"title": "stale"'))
        import os
        import time

        future = time.time() + 5
        os.utime(snap, (future, future))
    pair.same("doctor", "--json")
