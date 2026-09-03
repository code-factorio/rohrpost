// JSON value type and Python-exact serialisation.
//
// nlohmann::ordered_json is the in-memory representation (insertion-ordered
// objects, like Python dicts). Serialisation is our own: the reference emits
// three distinct byte layouts — msgspec's compact event lines, `json.dumps`
// with default separators (snapshot, shadow, `rp init --json`) and
// `json.dump(indent=2, ensure_ascii=False)` (every other `--json`) — and the
// log format in particular must stay byte-identical across implementations.
#pragma once

#include <nlohmann/json.hpp>

#include <expected>
#include <string>
#include <string_view>

namespace rp {

using Json = nlohmann::ordered_json;

namespace json {

/// Serialisation flavour.
struct Style {
    /// Python `json.dumps` default separators (", " and ": ") versus compact.
    bool spaces = true;
    /// Indentation width for pretty output; 0 means single-line.
    int indent = 0;
    /// Escape non-ASCII as \uXXXX (Python's `ensure_ascii=True`).
    bool ensure_ascii = true;
    /// Sort object keys (Python's `sort_keys=True`).
    bool sort_keys = false;
};

/// msgspec's compact encoding: no spaces, raw UTF-8, `\u00XX` for controls.
inline constexpr Style kCompact{.spaces = false, .indent = 0, .ensure_ascii = false, .sort_keys = false};
/// `json.dumps(obj)` with defaults.
inline constexpr Style kPyDefault{.spaces = true, .indent = 0, .ensure_ascii = true, .sort_keys = false};
/// `json.dump(obj, indent=2, ensure_ascii=False)` — the `--json` output shape.
inline constexpr Style kPretty{.spaces = true, .indent = 2, .ensure_ascii = false, .sort_keys = false};
/// `json.dump(obj, ensure_ascii=False, sort_keys=True)` — shadow files.
inline constexpr Style kSortedRaw{.spaces = true, .indent = 0, .ensure_ascii = false, .sort_keys = true};

[[nodiscard]] std::string dumps(const Json& value, const Style& style);

/// Parse one JSON document. The error text is a short description.
[[nodiscard]] std::expected<Json, std::string> parse(std::string_view text);

/// Python-style equality with the numeric coercions `==` performs
/// (1 == 1.0, True == 1); everything else is structural.
[[nodiscard]] bool py_equal(const Json& a, const Json& b);

/// `str(value)`: strings verbatim, everything else via `repr`.
[[nodiscard]] std::string py_str(const Json& value);

/// `repr(value)` for the JSON subset of Python values.
[[nodiscard]] std::string py_repr(const Json& value);

}  // namespace json
}  // namespace rp
