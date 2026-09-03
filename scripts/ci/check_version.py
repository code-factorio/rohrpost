#!/usr/bin/env python3
"""Assert the CMake project version equals the Python package version.

The version is declared twice — `project(rohrpost VERSION ...)` in
CMakeLists.txt for the native binary and `[project].version` in pyproject.toml
for the reference — and `rp --version` must agree between them. `--print`
emits the shared version for release tagging.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def cmake_version() -> str:
    text = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    match = re.search(r"project\(rohrpost\s+VERSION\s+([0-9][0-9A-Za-z.\-]*)", text)
    if not match:
        raise SystemExit("CMakeLists.txt: project VERSION not found")
    return match.group(1)


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("pyproject.toml: version not found")
    return match.group(1)


def main(argv: list[str]) -> int:
    cmake, py = cmake_version(), pyproject_version()
    if cmake != py:
        print(
            f"version mismatch: CMakeLists.txt says {cmake}, pyproject.toml says {py}",
            file=sys.stderr,
        )
        return 1
    if "--print" in argv:
        print(cmake)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
