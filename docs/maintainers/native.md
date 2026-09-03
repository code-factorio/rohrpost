# Rohrpost — The native `rp` (C++23)

This guide is for people changing the C++ implementation under `cpp/`. It
complements [architecture.md](architecture.md), which describes the design the
native binary reproduces, and records how compatibility with the Python
reference is proven. The decision itself is
[ADR-0001](../adr/0001-native-cpp23-rp-with-frozen-python-reference.md).

---

## Layout

```
CMakeLists.txt           project(rohrpost VERSION ...), targets rp / rp_core / rp_tests
CMakePresets.json        linux | macos | windows (+ *-release: static runtime, -Werror)
cmake/                   Warnings.cmake, Runtime.cmake
cpp/include/rohrpost/    one header per module, same names as the Python modules
cpp/src/                 implementations (+ http_curl.cpp / http_winhttp.cpp)
cpp/tests/               doctest unit tests for the pure, byte-level modules
third_party/             vendored: nlohmann/json, toml++, doctest (header-only, MIT)
tests/conformance/       pytest: the native binary vs the Python reference
scripts/ci/plan.py       the dynamic CI matrix (build legs, release targets, shards)
```

Module map (mirrors `src/rohrpost`):

| Module | Purpose |
|---|---|
| `pyfmt` | Python-compatible primitives the contract leaks: `str.strip()` whitespace set, `int()` parsing, `repr()`, `round()` (banker's), float `repr`, UTF-8 validation with Python's error wording |
| `json` | `nlohmann::ordered_json` as the value type; **our own serialiser** with the three layouts the reference emits (msgspec compact for the log, `json.dumps` default, `json.dump(indent=2, ensure_ascii=False)`) |
| `ids`, `entropy` | ticket ids / ULIDs from the OS CSPRNG |
| `timeutil` | RFC 3339 ms timestamps, the monotonic per-process clock, ISO parsing for compaction |
| `events`, `store` | the envelope and the locked append; `flock` on POSIX, `_locking` (the CRT primitive behind `msvcrt.locking`) on Windows, same byte range |
| `fold`, `api` | the fold, derived state, the snapshot cache, the one write path |
| `argparse` | a faithful subset of CPython 3.14's argparse: usage wrapping, help columns, abbreviations, positional chunking, error wording |
| `cli`, `io` | handlers and rendering; UTF-8 output everywhere, text-mode newlines on Windows like Python's stdout |
| `doctor`, `compact`, `stats` | the maintenance commands |
| `shadow`, `merge`, `sync`, `providers/github` | spec §8; `gh` preferred, HTTP fallback |
| `subprocess`, `http` | argv-only child processes with timeouts; libcurl / WinHTTP behind one function |

The dependency direction is the same as in Python: `cli → api → {store, fold,
config}`; `fold → store → events → ids`. Nothing includes `cli.hpp`.

---

## Building

```bash
cmake --preset linux && cmake --build --preset linux && ctest --preset linux
```

Presets exist for `macos` and `windows` (run the latter from a Developer
Command Prompt or after `vcvarsall`). The `*-release` presets link the C++
runtime statically (Linux: `-static-libstdc++`; Windows: static CRT; macOS:
universal `arm64;x86_64`) and turn warnings into errors — that is what the
release workflow ships.

Requirements: a C++23 compiler (GCC 14+, Clang 18+ with libc++, Apple Clang
from Xcode 16, MSVC 19.40+), CMake 3.25+, Ninja, and on POSIX the libcurl
development headers. Everything else is vendored.

The conservative C++23 subset in use — `std::expected`, `std::format`,
`std::ranges::to`, monadic `optional`, `std::string::contains` — is what the
oldest supported Apple Clang provides; `<print>`, `flat_map`, deducing `this`
and `[[assume]]` are deliberately avoided.

---

## Compatibility: what is byte-exact and how it is proven

Byte-exact with the reference:

- every `log.jsonl` line (msgspec field order, separators, escaping, raw UTF-8);
- `tickets.jsonl`, shadow files, `config.toml` written by `init`, and the
  `.gitattributes` / `.gitignore` edits;
- every `--json` payload and every human-readable line, including
  `--help`/usage/error text at any `COLUMNS` width;
- exit codes and the precedence of validation errors (the handlers evaluate in
  the same order as the Python functions).

Proven by `tests/conformance` (`make conformance`, or in CI one shard per test
module per OS). The suite runs the *same* command sequences against both
implementations in two fresh repositories and compares stdout, stderr, exit
codes and the event log after normalising ticket ids, ULIDs, timestamps and
temp paths. `test_replay.py` folds this repository's own log with both and
compares read-only output without any normalisation; `test_interop.py` puts
both implementations on **one** repository, alternating writers, and checks the
lock primitives exclude each other.

Known deviations (all documented, none affect the data on disk):

- Error text originating in third-party parsers differs: `invalid config.toml:
  ...` / `invalid template ...` carry toml++'s message instead of tomllib's,
  and `doctor`'s `log_parses` detail carries nlohmann's message for malformed
  JSON (schema-level messages such as ``Expected `str`, got `int` - at `$.id` ``
  are reproduced).
- A malformed log stops `read_events` on an invalid UTF-8 byte with an error
  instead of the reference's traceback.
- Integers wider than 64 bits inside a hand-authored `set` payload are parsed
  as doubles. `rp` never writes such values.
- `str.isprintable()` is approximated for the `repr()` used in sync-conflict
  comments (control, separator and format code points are escaped; unassigned
  code points are kept).

If you change behaviour in one implementation, change the other and add a
conformance case; the reference's own test suite must keep passing too.

---

## Adding things

**A new field / status / op.** Same recipe as
[architecture.md](architecture.md#adding-things), in both trees; the fold's
`kScalarFields` / `kSetFields` / `kStatuses` mirror the Python constants.

**A new CLI flag.** Declare it in `cli.cpp::build_parser` with the same
`help` text as the Python parser — the help output is compared byte for byte.
`argparse.hpp` supports store / store_true / append / version / help actions,
`nargs` None / `?` / `*`, `type=int`, `choices` and one level of subparsers,
which is everything `rp` uses.

**A sync provider.** Implement `providers::Provider` (`fetch` / `push`
returning local-vocabulary JSON objects) and register it in
`cli.cpp::build_provider`.

**A new build target.** Add a leg to `BUILD_LEGS` in `scripts/ci/plan.py`; the
CI and release workflows fan out from that catalogue, so no workflow edit is
needed.

---

## Quality gate

- `ctest` runs the doctest suite (`cpp/tests`): focused tests for the
  byte-level primitives (JSON layouts, float repr, rounding, repr, UTF-8
  errors, universal newlines).
- `make conformance` runs the differential suite against a built binary
  (`RP_NATIVE` selects it; the newest `build/**/rp` is the default).
- The release presets build with `-Wall -Wextra -Wpedantic -Wshadow
  -Wconversion ...` and `-Werror` (`/W4 /WX` on MSVC).
