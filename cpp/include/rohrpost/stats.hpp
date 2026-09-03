// `rp stats` — on-demand repository statistics (spec §13.1; mirrors
// src/rohrpost/stats.py). Every signal is derived from the event log; only the
// fold timing is a live measurement.
#pragma once

#include "rohrpost/json.hpp"

#include <filesystem>

namespace rp::stats {

/// The kernel's PIPE_BUF for the filesystem holding `path` (4096 on Windows).
[[nodiscard]] long pipe_buf(const std::filesystem::path& path);

/// The §13.1 decision signals from the live log.
[[nodiscard]] Json compute_stats(const std::filesystem::path& rohrpost_dir, int fold_runs = 5);

}  // namespace rp::stats
