// `rp compact` — archive terminal tickets' events and truncate the live log
// (spec §6.1; mirrors src/rohrpost/compact.py).
#pragma once

#include "rohrpost/json.hpp"

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

namespace rp::compact {

/// Default retention before terminal-ticket events are archived.
inline constexpr int kDefaultArchiveAfterDays = 90;

/// What compaction did.
struct CompactResult {
    std::size_t archived;
    std::size_t remaining;
    std::vector<std::string> archive_files;
    [[nodiscard]] Json to_mapping() const;
};

struct Options {
    int archive_after_days = kDefaultArchiveAfterDays;
    bool force = false;
    bool json_output = false;
    std::optional<std::int64_t> now_ms;  // injectable "now" for tests
};

/// The `log-<YYYY>-Q<N>.jsonl` bucket for an event timestamp.
[[nodiscard]] std::string quarter_bucket(const std::string& ts);

/// Run compaction; returns the process exit code (0 success, 1 refused/error).
int run(const std::filesystem::path& rohrpost_dir, const Options& options);

}  // namespace rp::compact
