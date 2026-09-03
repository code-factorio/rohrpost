#include "rohrpost/ids.hpp"

#include "rohrpost/entropy.hpp"
#include "rohrpost/errors.hpp"
#include "rohrpost/pyfmt.hpp"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <format>
#include <optional>
#include <string>
#include <string_view>

namespace rp::ids {
namespace {

constexpr std::string_view kTicketAlphabet = "0123456789abcdefghjkmnpqrstvwxyz";
constexpr std::string_view kCrockfordAlphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
constexpr int kTicketLength = 6;
constexpr int kTicketBits = kTicketLength * 5;
constexpr int kUlidLength = 26;
constexpr int kTimestampBits = 48;
constexpr int kRandomnessBits = 80;

/// Encode `value` (128-bit as hi/lo) as `length` base32 chars, most significant first.
std::string encode_base32(std::uint64_t hi, std::uint64_t lo, int length, std::string_view alphabet) {
    std::string out(static_cast<std::size_t>(length), '0');
    for (int i = 0; i < length; ++i) {
        const int shift = 5 * i;
        std::uint64_t chunk;
        if (shift >= 64) chunk = (hi >> (shift - 64)) & 0x1f;
        else if (shift > 59) chunk = ((lo >> shift) | (hi << (64 - shift))) & 0x1f;
        else chunk = (lo >> shift) & 0x1f;
        out[static_cast<std::size_t>(length - 1 - i)] = alphabet[chunk];
    }
    return out;
}

bool all_in(std::string_view value, std::string_view alphabet, std::size_t length) {
    if (value.size() != length) return false;
    for (const char c : value) {
        if (alphabet.find(c) == std::string_view::npos) return false;
    }
    return true;
}

}  // namespace

std::string new_ticket_id() {
    return encode_base32(0, entropy::randbits(kTicketBits), kTicketLength, kTicketAlphabet);
}

bool is_valid_ticket_id(std::string_view value) {
    return all_in(value, kTicketAlphabet, kTicketLength);
}

std::string new_ulid(std::optional<std::int64_t> timestamp_ms) {
    std::int64_t ms = timestamp_ms.has_value() ? *timestamp_ms : 0;
    if (!timestamp_ms.has_value()) {
        ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                 std::chrono::system_clock::now().time_since_epoch())
                 .count();
    }
    if (ms < 0 || ms >= (std::int64_t{1} << kTimestampBits)) {
        throw IdError(std::format("timestamp out of range for a 48-bit ULID: {}", ms));
    }
    // value = ts << 80 | rand80  — as a 128-bit (hi, lo) pair.
    const std::uint64_t rand_lo = entropy::randbits(64);
    const std::uint64_t rand_hi = entropy::randbits(kRandomnessBits - 64);  // 16 bits
    const auto ts = static_cast<std::uint64_t>(ms);
    const std::uint64_t hi = (ts << 16) | rand_hi;
    return encode_base32(hi, rand_lo, kUlidLength, kCrockfordAlphabet);
}

bool is_valid_ulid(std::string_view value) {
    return all_in(value, kCrockfordAlphabet, kUlidLength);
}

std::string render_id(std::string_view prefix, std::string_view ticket_id) {
    if (prefix.empty()) throw IdError("prefix must be non-empty");
    if (!is_valid_ticket_id(ticket_id)) {
        throw IdError(std::format("not a valid ticket id: {}", py::repr(ticket_id)));
    }
    return std::format("{}-{}", prefix, ticket_id);
}

std::string normalize_id(std::string_view value) {
    std::string_view candidate = value;
    const auto dash = value.rfind('-');
    if (dash != std::string_view::npos) candidate = value.substr(dash + 1);
    if (!is_valid_ticket_id(candidate)) {
        throw IdError(std::format("not a valid ticket id: {}", py::repr(value)));
    }
    return std::string(candidate);
}

}  // namespace rp::ids
