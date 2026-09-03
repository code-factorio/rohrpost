#!/usr/bin/env python3
"""Compute the CI job matrices at run time (dynamic workflows).

GitHub Actions cannot enumerate targets or test shards by itself, so every
workflow starts with a `plan` job that runs this script and publishes JSON
matrices through ``$GITHUB_OUTPUT``. Downstream jobs fan out with
``fromJSON(needs.plan.outputs.<name>)``, so parallelism follows the work:
adding a build target here, or a conformance test module under
``tests/conformance/``, changes the shape of the run without a workflow edit.

Outputs (all JSON):

* ``build``      — native build/test legs, one per OS + toolchain.
* ``release``    — release artifacts, one per (os, arch) target.
* ``conformance``— reference-vs-native shards: each test module on each OS.
* ``python``     — the reference implementation's own test legs.

Run locally with ``python3 scripts/ci/plan.py --pretty`` to inspect the plan.
Filters: ``--only-os linux``, ``--only-target linux-x86_64``, ``--event push``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# --- the target catalogue ------------------------------------------------------
# Each build leg names the runner, the CMake preset and the compiler setup step
# the workflow understands. `artifact` is the file name shipped from that leg.
BUILD_LEGS = [
    {
        "name": "linux-x86_64",
        "os": "linux",
        "runner": "ubuntu-24.04",
        "preset": "linux-release",
        "toolchain": "gcc-14",
        "artifact": "rp-linux-x86_64",
        "exe": "rp",
        "release": True,
    },
    {
        "name": "linux-aarch64",
        "os": "linux",
        "runner": "ubuntu-24.04-arm",
        "preset": "linux-release",
        "toolchain": "gcc-14",
        "artifact": "rp-linux-aarch64",
        "exe": "rp",
        "release": True,
    },
    {
        "name": "linux-clang",
        "os": "linux",
        "runner": "ubuntu-24.04",
        "preset": "linux",
        "toolchain": "clang-18",
        "artifact": "rp-linux-x86_64-clang",
        "exe": "rp",
        "release": False,
    },
    {
        "name": "macos-universal",
        "os": "macos",
        "runner": "macos-15",
        "preset": "macos-release",
        "toolchain": "apple-clang",
        "artifact": "rp-macos-universal",
        "exe": "rp",
        "release": True,
    },
    {
        "name": "windows-x86_64",
        "os": "windows",
        "runner": "windows-2022",
        "preset": "windows-release",
        "toolchain": "msvc",
        "msvc_arch": "amd64",
        "artifact": "rp-windows-x86_64",
        "exe": "rp.exe",
        "release": True,
    },
    {
        "name": "windows-arm64",
        "os": "windows",
        "runner": "windows-2022",
        "preset": "windows-release",
        "toolchain": "msvc",
        "msvc_arch": "amd64_arm64",
        "artifact": "rp-windows-arm64",
        "exe": "rp.exe",
        "release": True,
        "cross": True,  # built on x86_64; the binary cannot run on the builder
    },
]

# The reference implementation's own quality gate, one leg per OS.
PYTHON_LEGS = [
    {"name": "ubuntu", "runner": "ubuntu-latest"},
    {"name": "macos", "runner": "macos-latest"},
    {"name": "windows", "runner": "windows-latest"},
]

# Conformance shards run wherever the native binary can execute natively.
CONFORMANCE_HOSTS = [
    {"os": "linux", "runner": "ubuntu-24.04", "build": "linux-x86_64"},
    {"os": "macos", "runner": "macos-15", "build": "macos-universal"},
    {"os": "windows", "runner": "windows-2022", "build": "windows-x86_64"},
]


def conformance_modules() -> list[str]:
    """Every `tests/conformance/test_*.py`; each module becomes one shard."""
    return sorted(p.stem for p in (ROOT / "tests" / "conformance").glob("test_*.py"))


def build_plan(only_os: set[str] | None, only_target: set[str] | None, release_only: bool) -> dict:
    legs = BUILD_LEGS
    if only_os:
        legs = [leg for leg in legs if leg["os"] in only_os]
    if only_target:
        legs = [leg for leg in legs if leg["name"] in only_target]
    build = [leg for leg in legs if not release_only or leg["release"]]
    release = [leg for leg in legs if leg["release"]]
    hosts = [h for h in CONFORMANCE_HOSTS if any(leg["name"] == h["build"] for leg in build)]
    conformance = [{**host, "module": module} for host in hosts for module in conformance_modules()]
    python = [
        leg
        for leg in PYTHON_LEGS
        if not only_os or leg["name"] in {"ubuntu" if o == "linux" else o for o in only_os}
    ]
    return {
        "build": {"include": build},
        "release": {"include": release},
        "conformance": {"include": conformance},
        "python": {"include": python},
        "modules": conformance_modules(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only-os",
        action="append",
        default=None,
        help="restrict to an OS (linux|macos|windows); repeatable",
    )
    parser.add_argument(
        "--only-target",
        action="append",
        default=None,
        help="restrict to a build leg name; repeatable",
    )
    parser.add_argument(
        "--release-only", action="store_true", help="only legs that ship release artifacts"
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="print an indented plan instead of GITHUB_OUTPUT lines",
    )
    args = parser.parse_args(argv)

    plan = build_plan(
        set(args.only_os or []) or None, set(args.only_target or []) or None, args.release_only
    )
    if args.pretty:
        print(json.dumps(plan, indent=2))
        return 0
    output = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{key}={json.dumps(value, separators=(',', ':'))}" for key, value in plan.items()]
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## CI plan\n\n")
            fh.write(
                f"- build legs: {', '.join(leg['name'] for leg in plan['build']['include']) or 'none'}\n"
            )
            fh.write(
                f"- release artifacts: {', '.join(leg['artifact'] for leg in plan['release']['include']) or 'none'}\n"
            )
            fh.write(
                f"- conformance shards: {len(plan['conformance']['include'])} ({len(plan['modules'])} modules)\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
