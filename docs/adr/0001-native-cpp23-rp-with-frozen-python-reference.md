---
status: accepted
date: 2026-09-03
---

# The shipped `rp` is a native C++23 binary; the Python package is the frozen reference

Rohrpost's original implementation is Python 3.14 driven through `uv`, which
means every agent invocation pays interpreter start-up and every bare container
needs a Python toolchain before `rp ready --json` can run. We rewrote the tool
in C++23 (`cpp/`, built with CMake) as a single self-contained binary for Linux,
macOS and Windows, and we keep the Python package in `src/rohrpost` **unchanged
and frozen** as the behavioural oracle: the conformance suite in
`tests/conformance` runs both against the same command sequences and asserts
byte-identical output, exit codes and event-log bytes. Compatibility is
bug-for-bug — the log format, the `tickets.jsonl` cache, shadow files, the
argparse help/usage/error text and the platform lock primitives (`flock` on
POSIX, the CRT byte-range lock on Windows) are all reproduced so a native `rp`
and a Python `rp` can share one repository.

## Considered options

- **Keep Python, ship it as a PyInstaller/Nuitka bundle.** Solves distribution
  but not start-up latency or the Python-only platform edge cases (locale
  codecs, text-mode newlines) that dominated the Windows work.
- **Rewrite and delete the Python package.** Removes the only executable
  specification of the contract; the differential suite would have to be
  replaced by hand-maintained golden files that drift.
- **Rewrite and keep the Python package as the frozen reference (chosen).** The
  reference costs nothing to keep, proves compatibility mechanically, and can
  be deleted in one commit once the native binary has carried real workloads.

## Consequences

- New behaviour goes into the C++ tree; the Python package changes only to fix
  a defect that the native side must then mirror. Its own quality gate keeps
  running in CI because a broken oracle proves nothing.
- Three header-only third-party libraries are vendored under `third_party/`
  (nlohmann/json for the value type, toml++ for `config.toml`, doctest for unit
  tests); the only system dependency is the platform HTTP stack used by the
  GitHub REST fallback (libcurl on POSIX, WinHTTP on Windows).
- Known, documented deviations are limited to the wording of errors that
  originate in third-party parsers (tomllib vs toml++, msgspec vs nlohmann)
  and to integers beyond 64 bits inside hand-authored event payloads; see
  `docs/maintainers/native.md`.
