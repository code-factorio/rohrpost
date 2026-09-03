// Python-compatible text primitives.
//
// The reference implementation leaks a handful of Python formatting rules into
// its byte-level contract: `str.strip()`'s whitespace set, `int()` parsing,
// `repr()` of strings and containers (sync conflict comments are written into
// the log with it), `round()`'s banker's rounding and float `repr`. These
// helpers reproduce those rules so output and log bytes stay identical.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace rp::py {

/// True for the code points `str.strip()` removes.
[[nodiscard]] bool is_space(char32_t cp);

/// `str.strip()` over a UTF-8 string (invalid bytes are treated as non-space).
[[nodiscard]] std::string_view strip(std::string_view text);

/// `int(text)` for base-10 text: whitespace, sign, single underscores between
/// digits. Unicode digits are not supported. Overflow yields nullopt.
[[nodiscard]] std::optional<std::int64_t> parse_int(std::string_view text);

/// `repr(float)` — shortest round-trip digits in Python's fixed/exponent layout.
[[nodiscard]] std::string float_repr(double value);

/// `round(x)` — half to even, as an integer.
[[nodiscard]] std::int64_t round_half_even(double value);

/// `round(x, ndigits)` — correctly rounded decimal rounding of the exact binary value.
[[nodiscard]] double round_digits(double value, int ndigits);

/// `repr(str)` — Python quote selection and escaping rules.
[[nodiscard]] std::string repr(std::string_view text);

/// Decode one UTF-8 sequence starting at `pos`. Returns the code point and
/// advances `pos`; returns nullopt (without advancing) on invalid input.
[[nodiscard]] std::optional<char32_t> decode_utf8(std::string_view text, std::size_t& pos);

/// Encode one code point as UTF-8 (appends to `out`).
void append_utf8(std::string& out, char32_t cp);

/// Validation outcome of `bytes.decode("utf-8")`: the Python-style reason
/// (`invalid start byte`, `invalid continuation byte`, `unexpected end of data`)
/// and the byte span it reports, or nullopt when the text is valid.
struct Utf8Error {
    std::size_t start;
    std::size_t end;  // inclusive
    std::string reason;
    /// The message Python's UnicodeDecodeError renders, for a given byte span.
    [[nodiscard]] std::string message(std::string_view text) const;
};
[[nodiscard]] std::optional<Utf8Error> validate_utf8(std::string_view text);

/// Split text into lines the way Python's text-mode file iteration does
/// (universal newlines: `\n`, `\r\n` and `\r` all end a line). The trailing
/// newline is not part of the returned line.
[[nodiscard]] std::vector<std::string_view> split_lines(std::string_view text);

/// `text.casefold()` restricted to ASCII (the reference uses full Unicode
/// casefolding; titles are matched with this for `rp list --match`).
[[nodiscard]] std::string casefold(std::string_view text);

}  // namespace rp::py
