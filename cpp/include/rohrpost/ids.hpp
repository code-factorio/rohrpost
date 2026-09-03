// Identifiers: ticket ids and event ULIDs (mirrors src/rohrpost/ids.py).
//
// Ticket ids are 6 lowercase Crockford base32 characters drawn from 30 random
// bits; the display prefix never enters the log. Event ids are 26-character
// ULIDs (48-bit millisecond timestamp + 80 random bits), lexicographically
// time-ordered, which gives the fold a deterministic tiebreak.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace rp::ids {

/// A fresh 6-char lowercase base32 ticket id (e.g. `a1b2c3`).
[[nodiscard]] std::string new_ticket_id();

/// True if `value` is a bare 6-char lowercase base32 ticket id.
[[nodiscard]] bool is_valid_ticket_id(std::string_view value);

/// A fresh 26-char Crockford-base32 ULID; `timestamp_ms` pins the clock for tests.
/// Throws IdError when the timestamp does not fit 48 bits.
[[nodiscard]] std::string new_ulid(std::optional<std::int64_t> timestamp_ms = std::nullopt);

/// True if `value` is a well-formed 26-char Crockford-base32 ULID.
[[nodiscard]] bool is_valid_ulid(std::string_view value);

/// `PREFIX-a1b2c3`. Throws IdError on an empty prefix or invalid id.
[[nodiscard]] std::string render_id(std::string_view prefix, std::string_view ticket_id);

/// The bare ticket id from either `a1b2c3` or `PREFIX-a1b2c3`. Throws IdError.
[[nodiscard]] std::string normalize_id(std::string_view value);

}  // namespace rp::ids
