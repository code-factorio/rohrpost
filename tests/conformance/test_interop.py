"""Both implementations on one repository: alternating writers must agree."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conformance.conftest import Pair, Result


def _id(result: Result) -> str:
    assert result.code == 0, result.err
    rendered = result.json()["id"]
    assert isinstance(rendered, str)
    return rendered.split("-")[1]


def test_alternating_writers_share_one_log(shared_repo: Pair) -> None:
    ref, nat = shared_repo.both
    a = _id(ref.run("new", "A", "--json"))
    b = _id(nat.run("new", "B", "--blocked-by", a, "--json"))
    c = _id(
        ref.run(
            "new", "C", "--blocked-by", a, "--blocked-by", b, "--type", "bug", "-p", "0", "--json"
        )
    )
    assert nat.run("set", a, "labels+=x,y", "status=in_progress").code == 0
    assert ref.run("set", a, "labels-=x", "priority=1").code == 0
    assert nat.run("comment", a, "native note").code == 0
    assert ref.run("comment", a, "reference note").code == 0
    assert nat.run("close", a, "--reason", "native closed").code == 0
    assert ref.run("close", a).out.startswith("Already closed")
    assert nat.run("link", b, "github", "9").code == 0
    assert ref.run("unlink", b, "github").code == 0
    for args in (
        ("list", "--json"),
        ("ready", "--json"),
        ("show", a, "--json"),
        ("show", b, "--json"),
        ("show", c, "--json"),
        ("show", c, "--include", "body,deps,notes,fieldts"),
        ("log", "--json"),
        ("log", a),
        ("doctor", "--json"),
        ("comments", a, "--json"),
        ("tree", a, "--json"),
    ):
        r, n = ref.run(*args), nat.run(*args)
        assert n.code == r.code
        assert n.stdout == r.stdout, args
        assert n.stderr == r.stderr, args
    ready = [t["id"] for t in json.loads(nat.run("ready", "--json").stdout)]
    assert ready == [f"TST-{b}"]  # C still blocked by B


def test_snapshot_from_either_writer_is_trusted_by_the_other(shared_repo: Pair) -> None:
    ref, nat = shared_repo.both
    _id(ref.run("new", "A", "--json"))
    snapshot = ref.rohrpost_dir / "tickets.jsonl"
    assert snapshot.is_file()
    first = snapshot.read_bytes()
    assert nat.run("list", "--json").code == 0
    assert snapshot.read_bytes() == first  # fresh cache reused, not rewritten
    _id(nat.run("new", "B", "--json"))
    assert ref.run("doctor", "--json").code == 0


def test_dedupe_and_union_merge_duplicates(shared_repo: Pair) -> None:
    ref, nat = shared_repo.both
    a = _id(ref.run("new", "A", "--json"))
    log = ref.rohrpost_dir / "log.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(b"".join(lines + lines))  # a union merge that duplicated every line
    r, n = ref.run("show", a, "--json"), nat.run("show", a, "--json")
    assert n.stdout == r.stdout
    r, n = ref.run("doctor", "--json"), nat.run("doctor", "--json")
    assert n.stdout == r.stdout
    assert n.code == r.code == 1


def test_malformed_log_lines_reported_the_same(shared_repo: Pair) -> None:
    ref, nat = shared_repo.both
    _id(ref.run("new", "A", "--json"))
    log = ref.rohrpost_dir / "log.jsonl"
    log.write_bytes(
        log.read_bytes() + b'\n\n{"id":"x","ts":"t","ticket":"a","op":"nope","actor":"u"}\n  \n'
    )
    for args in (("list",), ("doctor",), ("doctor", "--json"), ("log",)):
        r, n = ref.run(*args), nat.run(*args)
        assert n.code == r.code, args
        assert n.stdout == r.stdout, args
        assert n.stderr == r.stderr, args


def test_hand_authored_rendered_ids_and_crlf_fold_the_same(shared_repo: Pair) -> None:
    ref, nat = shared_repo.both
    a = _id(ref.run("new", "A", "--json"))
    log = ref.rohrpost_dir / "log.jsonl"
    extra = (
        '{"id":"01KZV11MVQBMAQ0ZZ105RABP4T","ts":"2099-01-01T00:00:00.000Z","ticket":"TST-'
        + a
        + '","op":"set","actor":"u",'
        '"set":{"parent":"RP-abcdef","labels+":"solo","priority":"3","blocked_by+":["XX-zzzzzz"],"unknown":1,"body":""}}\r\n'
        '{"id":"01KZV11MVQBMAQ0ZZ105RABP4U","ts":"2099-01-01T00:00:01.000Z","ticket":"__sync__","op":"synced","actor":"remote/github","remote":"github","at":"2099-01-01T00:00:01.000Z"}\r\n'
    )
    log.write_bytes(log.read_bytes() + extra.encode())
    for args in (
        ("show", a, "--json"),
        ("show", a, "--include", "deps,fieldts"),
        ("list", "--json"),
        ("doctor", "--json"),
        ("log", "--json"),
        ("log", a, "--json"),
    ):
        r, n = ref.run(*args), nat.run(*args)
        assert n.code == r.code, args
        assert n.stdout == r.stdout, args


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock interop is a POSIX contract; Windows uses the CRT range lock",
)
def test_native_append_waits_for_python_lock(shared_repo: Pair) -> None:
    """A native writer must block on the reference's advisory lock, not corrupt the log."""
    ref, nat = shared_repo.both
    a = _id(ref.run("new", "A", "--json"))
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path; from rohrpost.store import file_lock\n"
            "with file_lock(Path(sys.argv[1])):\n    print('locked', flush=True)\n    time.sleep(1.5)",
            str(ref.rohrpost_dir),
        ],
        stdout=subprocess.PIPE,
        text=True,
        cwd=ref.repo,
    ) as holder:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        start = time.monotonic()
        result = nat.run("comment", a, "waited")
        elapsed = time.monotonic() - start
        holder.wait()
    assert result.code == 0, result.err
    assert elapsed >= 1.0, f"native append did not wait for the lock ({elapsed:.2f}s)"
    assert ref.run("doctor").code == 0


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock interop is a POSIX contract; Windows uses the CRT range lock",
)
def test_python_append_waits_for_flock_on_the_lock_file(shared_repo: Pair, tmp_path: Path) -> None:
    """The reference blocks on a plain flock of `.lock` — the primitive the native binary uses."""
    ref, nat = shared_repo.both
    a = _id(ref.run("new", "A", "--json"))
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time, os\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | os.O_APPEND)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(1.5); fcntl.flock(fd, fcntl.LOCK_UN)",
            str(nat.rohrpost_dir / ".lock"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        start = time.monotonic()
        result = ref.run("comment", a, "reference waited")
        elapsed = time.monotonic() - start
        holder.wait()
    assert elapsed >= 1.0, f"reference append did not wait for the lock ({elapsed:.2f}s)"
    assert result.code == 0, result.err
