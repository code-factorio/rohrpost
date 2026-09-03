// Timestamps: RFC 3339 UTC with millisecond precision (`2026-08-11T09:20:14.221Z`).
#pragma once

#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>

namespace rp {

/// A clock returns the current timestamp string. Injectable for deterministic tests.
using Clock = std::function<std::string()>;

namespace timeutil {

/// Render milliseconds since the Unix epoch as `YYYY-MM-DDTHH:MM:SS.mmmZ`.
[[nodiscard]] std::string format_ts(std::int64_t epoch_ms);

/// RFC 3339 UTC timestamp, strictly increasing per process (mirrors util.now_ts):
/// two events in the same millisecond would otherwise tie on `ts` and fall back
/// to the ULID's random suffix for ordering, so the millisecond is bumped on
/// collision.
[[nodiscard]] std::string now_ts();

/// Milliseconds since the Unix epoch, wall clock.
[[nodiscard]] std::int64_t now_epoch_ms();

/// Parse an ISO 8601 / RFC 3339 timestamp (`Z` or a numeric offset) to epoch
/// milliseconds, the way `datetime.fromisoformat` does for the forms rp writes.
[[nodiscard]] std::optional<std::int64_t> parse_ts(std::string_view text);

/// Calendar fields of an epoch-ms instant, in UTC.
struct Civil {
    int year;
    unsigned month;  // 1-12
    unsigned day;    // 1-31
    unsigned hour;
    unsigned minute;
    unsigned second;
    unsigned millisecond;
};
[[nodiscard]] Civil to_civil(std::int64_t epoch_ms);

}  // namespace timeutil
}  // namespace rp
