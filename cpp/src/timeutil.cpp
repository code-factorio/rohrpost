#include "rohrpost/timeutil.hpp"

#include <charconv>
#include <chrono>
#include <cmath>
#include <format>

namespace rp::timeutil {
namespace {

// Howard Hinnant's civil-from-days / days-from-civil (proleptic Gregorian).
std::int64_t days_from_civil(std::int64_t y, unsigned m, unsigned d) {
    y -= m <= 2;
    const std::int64_t era = (y >= 0 ? y : y - 399) / 400;
    const auto yoe = static_cast<unsigned>(y - era * 400);
    const unsigned doy = (153 * (m > 2 ? m - 3 : m + 9) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + static_cast<std::int64_t>(doe) - 719468;
}

bool parse_uint(std::string_view text, std::size_t& pos, std::size_t digits, unsigned& out) {
    if (pos + digits > text.size()) return false;
    unsigned value = 0;
    for (std::size_t i = 0; i < digits; ++i) {
        const char c = text[pos + i];
        if (c < '0' || c > '9') return false;
        value = value * 10 + static_cast<unsigned>(c - '0');
    }
    out = value;
    pos += digits;
    return true;
}

}  // namespace

Civil to_civil(std::int64_t epoch_ms) {
    std::int64_t days = epoch_ms / 86'400'000;
    std::int64_t rem = epoch_ms % 86'400'000;
    if (rem < 0) {
        rem += 86'400'000;
        --days;
    }
    const std::int64_t z = days + 719468;
    const std::int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    const auto doe = static_cast<unsigned>(z - era * 146097);
    const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const std::int64_t y = static_cast<std::int64_t>(yoe) + era * 400;
    const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const unsigned mp = (5 * doy + 2) / 153;
    const unsigned d = doy - (153 * mp + 2) / 5 + 1;
    const unsigned m = mp < 10 ? mp + 3 : mp - 9;
    Civil c{};
    c.year = static_cast<int>(y + (m <= 2));
    c.month = m;
    c.day = d;
    c.hour = static_cast<unsigned>(rem / 3'600'000);
    c.minute = static_cast<unsigned>((rem / 60'000) % 60);
    c.second = static_cast<unsigned>((rem / 1000) % 60);
    c.millisecond = static_cast<unsigned>(rem % 1000);
    return c;
}

std::string format_ts(std::int64_t epoch_ms) {
    const Civil c = to_civil(epoch_ms);
    return std::format("{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}.{:03d}Z", c.year, c.month, c.day,
                       c.hour, c.minute, c.second, c.millisecond);
}

std::int64_t now_epoch_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string now_ts() {
    static std::int64_t last_ms = 0;  // process-local high-water mark
    std::int64_t ms = now_epoch_ms();
    if (ms <= last_ms) ms = last_ms + 1;
    last_ms = ms;
    return format_ts(ms);
}

std::optional<std::int64_t> parse_ts(std::string_view text) {
    std::size_t pos = 0;
    unsigned year = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0;
    if (!parse_uint(text, pos, 4, year)) return std::nullopt;
    if (pos >= text.size() || text[pos] != '-') return std::nullopt;
    ++pos;
    if (!parse_uint(text, pos, 2, month)) return std::nullopt;
    if (pos >= text.size() || text[pos] != '-') return std::nullopt;
    ++pos;
    if (!parse_uint(text, pos, 2, day)) return std::nullopt;
    if (month < 1 || month > 12 || day < 1 || day > 31) return std::nullopt;
    std::int64_t frac_ms = 0;
    std::int64_t offset_ms = 0;
    if (pos < text.size()) {
        if (text[pos] != 'T' && text[pos] != ' ') return std::nullopt;
        ++pos;
        if (!parse_uint(text, pos, 2, hour)) return std::nullopt;
        if (pos >= text.size() || text[pos] != ':') return std::nullopt;
        ++pos;
        if (!parse_uint(text, pos, 2, minute)) return std::nullopt;
        if (pos < text.size() && text[pos] == ':') {
            ++pos;
            if (!parse_uint(text, pos, 2, second)) return std::nullopt;
            if (pos < text.size() && (text[pos] == '.' || text[pos] == ',')) {
                ++pos;
                std::size_t digits = 0;
                double frac = 0.0;
                double scale = 0.1;
                while (pos < text.size() && text[pos] >= '0' && text[pos] <= '9') {
                    frac += (text[pos] - '0') * scale;
                    scale /= 10.0;
                    ++pos;
                    ++digits;
                }
                if (digits == 0) return std::nullopt;
                frac_ms = static_cast<std::int64_t>(std::floor(frac * 1000.0 + 1e-9));
            }
        }
        if (hour > 23 || minute > 59 || second > 59) return std::nullopt;
        if (pos < text.size()) {
            if (text[pos] == 'Z' || text[pos] == 'z') {
                ++pos;
            } else if (text[pos] == '+' || text[pos] == '-') {
                const int sign = text[pos] == '-' ? -1 : 1;
                ++pos;
                unsigned oh = 0, om = 0;
                if (!parse_uint(text, pos, 2, oh)) return std::nullopt;
                if (pos < text.size() && text[pos] == ':') ++pos;
                if (!parse_uint(text, pos, 2, om)) return std::nullopt;
                offset_ms = sign * (static_cast<std::int64_t>(oh) * 3'600'000 + static_cast<std::int64_t>(om) * 60'000);
            } else {
                return std::nullopt;
            }
        }
    }
    if (pos != text.size()) return std::nullopt;
    const std::int64_t days = days_from_civil(year, month, day);
    const std::int64_t ms = days * 86'400'000 + hour * 3'600'000LL + minute * 60'000LL + second * 1000LL + frac_ms;
    return ms - offset_ms;
}

}  // namespace rp::timeutil
