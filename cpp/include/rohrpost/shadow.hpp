// Shadow snapshots: the sync merge base (spec §8.1; mirrors src/rohrpost/shadow.py).
#pragma once

#include "rohrpost/json.hpp"

#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace rp::shadow {

[[nodiscard]] std::filesystem::path shadow_path(const std::filesystem::path& rohrpost_dir, std::string_view remote,
                                                std::string_view ref);

/// The merge base for `(remote, ref)`, or nullopt if there is no shadow yet.
[[nodiscard]] std::optional<Json> read_shadow(const std::filesystem::path& rohrpost_dir, std::string_view remote,
                                              std::string_view ref);

/// Atomically persist the post-sync remote fields as the new merge base.
void write_shadow(const std::filesystem::path& rohrpost_dir, std::string_view remote, std::string_view ref,
                  const Json& fields);

/// `(remote, ref)` pairs that currently have a shadow file.
[[nodiscard]] std::vector<std::pair<std::string, std::string>> all_shadowed(const std::filesystem::path& rohrpost_dir);

}  // namespace rp::shadow
