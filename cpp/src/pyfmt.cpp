#include "rohrpost/pyfmt.hpp"

#include <algorithm>
#include <array>
#include <cfenv>
#include <charconv>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <format>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace rp::py {

bool is_space(char32_t cp) {
    // The exact set `str.strip()` treats as whitespace (Unicode White_Space
    // plus the ASCII separators 0x1c-0x1f), enumerated from CPython 3.14.
    switch (cp) {
        case 0x09: case 0x0a: case 0x0b: case 0x0c: case 0x0d:
        case 0x1c: case 0x1d: case 0x1e: case 0x1f: case 0x20:
        case 0x85: case 0xa0: case 0x1680:
        case 0x2000: case 0x2001: case 0x2002: case 0x2003: case 0x2004:
        case 0x2005: case 0x2006: case 0x2007: case 0x2008: case 0x2009: case 0x200a:
        case 0x2028: case 0x2029: case 0x202f: case 0x205f: case 0x3000:
            return true;
        default:
            return false;
    }
}

std::optional<char32_t> decode_utf8(std::string_view text, std::size_t& pos) {
    if (pos >= text.size()) return std::nullopt;
    const auto b0 = static_cast<unsigned char>(text[pos]);
    if (b0 < 0x80) {
        ++pos;
        return b0;
    }
    std::size_t len = 0;
    char32_t cp = 0;
    if (b0 >= 0xc2 && b0 <= 0xdf) { len = 2; cp = b0 & 0x1fu; }
    else if (b0 >= 0xe0 && b0 <= 0xef) { len = 3; cp = b0 & 0x0fu; }
    else if (b0 >= 0xf0 && b0 <= 0xf4) { len = 4; cp = b0 & 0x07u; }
    else return std::nullopt;
    if (pos + len > text.size()) return std::nullopt;
    for (std::size_t i = 1; i < len; ++i) {
        const auto b = static_cast<unsigned char>(text[pos + i]);
        if ((b & 0xc0u) != 0x80u) return std::nullopt;
        cp = (cp << 6) | (b & 0x3fu);
    }
    // Reject overlongs, surrogates and out-of-range values.
    if ((len == 3 && cp < 0x800) || (len == 4 && (cp < 0x10000 || cp > 0x10ffff)) ||
        (cp >= 0xd800 && cp <= 0xdfff)) {
        return std::nullopt;
    }
    pos += len;
    return cp;
}

void append_utf8(std::string& out, char32_t cp) {
    if (cp < 0x80) {
        out.push_back(static_cast<char>(cp));
    } else if (cp < 0x800) {
        out.push_back(static_cast<char>(0xc0 | (cp >> 6)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3f)));
    } else if (cp < 0x10000) {
        out.push_back(static_cast<char>(0xe0 | (cp >> 12)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3f)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3f)));
    } else {
        out.push_back(static_cast<char>(0xf0 | (cp >> 18)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3f)));
        out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3f)));
        out.push_back(static_cast<char>(0x80 | (cp & 0x3f)));
    }
}

std::string_view strip(std::string_view text) {
    // Leading: decode forward while whitespace.
    std::size_t start = 0;
    while (start < text.size()) {
        std::size_t next = start;
        const auto cp = decode_utf8(text, next);
        if (!cp || !is_space(*cp)) break;
        start = next;
    }
    // Trailing: scan backwards to sequence starts and test each.
    std::size_t end = text.size();
    while (end > start) {
        std::size_t seq_start = end - 1;
        while (seq_start > start && (static_cast<unsigned char>(text[seq_start]) & 0xc0u) == 0x80u) {
            --seq_start;
        }
        std::size_t probe = seq_start;
        const auto cp = decode_utf8(text, probe);
        if (!cp || probe != end || !is_space(*cp)) break;
        end = seq_start;
    }
    return text.substr(start, end - start);
}

std::optional<std::int64_t> parse_int(std::string_view text) {
    const std::string_view s = strip(text);
    std::size_t i = 0;
    bool negative = false;
    if (i < s.size() && (s[i] == '+' || s[i] == '-')) {
        negative = s[i] == '-';
        ++i;
    }
    if (i >= s.size()) return std::nullopt;
    std::uint64_t magnitude = 0;
    bool last_was_digit = false;
    for (; i < s.size(); ++i) {
        const char c = s[i];
        if (c >= '0' && c <= '9') {
            const auto digit = static_cast<std::uint64_t>(c - '0');
            if (magnitude > (std::numeric_limits<std::uint64_t>::max() - digit) / 10) return std::nullopt;
            magnitude = magnitude * 10 + digit;
            last_was_digit = true;
        } else if (c == '_' && last_was_digit && i + 1 < s.size() && s[i + 1] >= '0' && s[i + 1] <= '9') {
            last_was_digit = false;
        } else {
            return std::nullopt;
        }
    }
    if (!last_was_digit) return std::nullopt;
    const auto limit = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
    if (negative) {
        if (magnitude > limit + 1) return std::nullopt;
        return magnitude == limit + 1 ? std::numeric_limits<std::int64_t>::min()
                                      : -static_cast<std::int64_t>(magnitude);
    }
    if (magnitude > limit) return std::nullopt;
    return static_cast<std::int64_t>(magnitude);
}

std::string float_repr(double value) {
    if (std::isnan(value)) return "nan";
    if (std::isinf(value)) return value < 0 ? "-inf" : "inf";
    // Shortest round-trip digits via scientific to_chars, then Python's layout:
    // fixed notation when -4 < decpt <= 16, exponent notation otherwise.
    std::array<char, 64> buf{};
    const auto res = std::to_chars(buf.data(), buf.data() + buf.size(), value, std::chars_format::scientific);
    std::string_view sci(buf.data(), static_cast<std::size_t>(res.ptr - buf.data()));
    bool negative = false;
    if (!sci.empty() && sci.front() == '-') {
        negative = true;
        sci.remove_prefix(1);
    }
    const auto epos = sci.find('e');
    std::string mantissa(sci.substr(0, epos));
    int exponent = 0;
    std::string_view exp_text = sci.substr(epos + 1);
    if (!exp_text.empty() && exp_text.front() == '+') exp_text.remove_prefix(1);  // from_chars rejects '+'
    std::from_chars(exp_text.data(), exp_text.data() + exp_text.size(), exponent);
    std::string digits;
    for (const char c : mantissa) {
        if (c != '.') digits.push_back(c);
    }
    // Strip trailing zeros (keep at least one digit).
    while (digits.size() > 1 && digits.back() == '0') digits.pop_back();
    const int decpt = exponent + 1;  // value = 0.<digits> * 10^decpt
    std::string out;
    if (negative) out.push_back('-');
    if (decpt > -4 && decpt <= 16) {
        if (decpt <= 0) {
            out += "0.";
            out.append(static_cast<std::size_t>(-decpt), '0');
            out += digits;
        } else if (static_cast<std::size_t>(decpt) >= digits.size()) {
            out += digits;
            out.append(static_cast<std::size_t>(decpt) - digits.size(), '0');
            out += ".0";
        } else {
            out += digits.substr(0, static_cast<std::size_t>(decpt));
            out.push_back('.');
            out += digits.substr(static_cast<std::size_t>(decpt));
        }
    } else {
        out.push_back(digits[0]);
        if (digits.size() > 1) {
            out.push_back('.');
            out += digits.substr(1);
        }
        const int e = decpt - 1;
        out += std::format("e{}{:02d}", e < 0 ? '-' : '+', std::abs(e));
    }
    return out;
}

std::int64_t round_half_even(double value) {
    // nearbyint honours the current rounding mode; the default is to-nearest-even.
    return static_cast<std::int64_t>(std::nearbyint(value));
}

double round_digits(double value, int ndigits) {
    if (!std::isfinite(value)) return value;
    // Python rounds the exact binary value to `ndigits` decimals (correctly
    // rounded, ties to even) and parses the result back. A correctly rounded
    // printf does exactly that on every supported libc.
    std::array<char, 512> buf{};
    const int n = std::snprintf(buf.data(), buf.size(), "%.*f", ndigits, value);
    if (n <= 0) return value;
    return std::strtod(buf.data(), nullptr);
}

namespace {

// Approximation of `str.isprintable()` for non-ASCII code points: everything
// is printable except the separator and format/control categories that occur
// in practice. Unassigned and private-use code points are treated as printable.
bool is_printable_non_ascii(char32_t cp) {
    if (cp >= 0x80 && cp <= 0x9f) return false;       // C1 controls
    if (is_space(cp)) return false;                    // Zs/Zl/Zp
    if (cp == 0xad) return false;                      // soft hyphen (Cf)
    if (cp >= 0x600 && cp <= 0x605) return false;
    if (cp == 0x61c || cp == 0x6dd || cp == 0x70f || cp == 0x180e) return false;
    if (cp >= 0x200b && cp <= 0x200f) return false;
    if (cp >= 0x202a && cp <= 0x202e) return false;
    if (cp >= 0x2060 && cp <= 0x2064) return false;
    if (cp >= 0x2066 && cp <= 0x206f) return false;
    if (cp == 0xfeff) return false;
    if (cp >= 0xfff9 && cp <= 0xfffb) return false;
    if (cp >= 0xd800 && cp <= 0xdfff) return false;    // surrogates
    if (cp >= 0xe000 && cp <= 0xf8ff) return false;    // private use
    if (cp == 0x110bd || (cp >= 0x1d173 && cp <= 0x1d17a)) return false;
    if (cp == 0xe0001 || (cp >= 0xe0020 && cp <= 0xe007f)) return false;
    if (cp >= 0xf0000) return false;                   // supplementary private use
    return true;
}

}  // namespace

std::string repr(std::string_view text) {
    const bool has_single = text.find('\'') != std::string_view::npos;
    const bool has_double = text.find('"') != std::string_view::npos;
    const char quote = (has_single && !has_double) ? '"' : '\'';
    std::string out;
    out.push_back(quote);
    std::size_t pos = 0;
    while (pos < text.size()) {
        const std::size_t seq_start = pos;
        const auto decoded = decode_utf8(text, pos);
        if (!decoded) {
            // Invalid byte: Python could not have produced it; escape as \x.
            out += std::format("\\x{:02x}", static_cast<unsigned char>(text[seq_start]));
            pos = seq_start + 1;
            continue;
        }
        const char32_t cp = *decoded;
        if (cp == static_cast<char32_t>(quote) || cp == '\\') {
            out.push_back('\\');
            out.push_back(static_cast<char>(cp));
        } else if (cp == '\n') out += "\\n";
        else if (cp == '\r') out += "\\r";
        else if (cp == '\t') out += "\\t";
        else if (cp < 0x20 || cp == 0x7f) out += std::format("\\x{:02x}", static_cast<unsigned>(cp));
        else if (cp < 0x80) out.push_back(static_cast<char>(cp));
        else if (is_printable_non_ascii(cp)) out.append(text.substr(seq_start, pos - seq_start));
        else if (cp < 0x100) out += std::format("\\x{:02x}", static_cast<unsigned>(cp));
        else if (cp < 0x10000) out += std::format("\\u{:04x}", static_cast<unsigned>(cp));
        else out += std::format("\\U{:08x}", static_cast<unsigned>(cp));
    }
    out.push_back(quote);
    return out;
}

std::string Utf8Error::message(std::string_view text) const {
    if (start == end) {
        return std::format("'utf-8' codec can't decode byte 0x{:02x} in position {}: {}",
                           static_cast<unsigned char>(text[start]), start, reason);
    }
    return std::format("'utf-8' codec can't decode bytes in position {}-{}: {}", start, end, reason);
}

std::optional<Utf8Error> validate_utf8(std::string_view text) {
    std::size_t pos = 0;
    while (pos < text.size()) {
        std::size_t next = pos;
        if (decode_utf8(text, next)) {
            pos = next;
            continue;
        }
        const auto b0 = static_cast<unsigned char>(text[pos]);
        std::size_t len = 0;
        if (b0 >= 0xc2 && b0 <= 0xdf) len = 2;
        else if (b0 >= 0xe0 && b0 <= 0xef) len = 3;
        else if (b0 >= 0xf0 && b0 <= 0xf4) len = 4;
        else return Utf8Error{pos, pos, "invalid start byte"};
        // Count the valid continuation bytes that follow (Python reports the
        // span it managed to read before the bad or missing byte).
        std::size_t valid = 1;
        while (valid < len && pos + valid < text.size()) {
            const auto b = static_cast<unsigned char>(text[pos + valid]);
            bool ok = (b & 0xc0u) == 0x80u;
            // Second-byte range restrictions (overlongs, surrogates, > U+10FFFF).
            if (ok && valid == 1) {
                if (b0 == 0xe0) ok = b >= 0xa0;
                else if (b0 == 0xed) ok = b <= 0x9f;
                else if (b0 == 0xf0) ok = b >= 0x90;
                else if (b0 == 0xf4) ok = b <= 0x8f;
            }
            if (!ok) return Utf8Error{pos, pos + valid - 1, "invalid continuation byte"};
            ++valid;
        }
        return Utf8Error{pos, pos + valid - 1, "unexpected end of data"};
    }
    return std::nullopt;
}

std::vector<std::string_view> split_lines(std::string_view text) {
    std::vector<std::string_view> lines;
    std::size_t start = 0;
    for (std::size_t i = 0; i < text.size(); ++i) {
        const char c = text[i];
        if (c == '\n' || c == '\r') {
            lines.push_back(text.substr(start, i - start));
            if (c == '\r' && i + 1 < text.size() && text[i + 1] == '\n') ++i;
            start = i + 1;
        }
    }
    if (start < text.size()) lines.push_back(text.substr(start));
    return lines;
}

std::string casefold(std::string_view text) {
    std::string out(text);
    for (char& c : out) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return out;
}

}  // namespace rp::py
