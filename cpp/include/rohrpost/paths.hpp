// Filesystem layout: locating `.rohrpost/` and its members (spec §4).
//
// Discovery walks up from the current directory looking for `.rohrpost/`,
// mirroring how git finds `.git/`, so `rp` works from anywhere inside a repo.
#pragma once

#include <array>
#include <filesystem>
#include <optional>
#include <string_view>
#include <vector>

namespace rp::paths {

inline constexpr std::string_view kRohrpostDirName = ".rohrpost";
inline constexpr std::string_view kConfigFilename = "config.toml";
inline constexpr std::string_view kLogFilename = "log.jsonl";
inline constexpr std::string_view kSnapshotFilename = "tickets.jsonl";
inline constexpr std::string_view kArchiveDirName = "archive";
inline constexpr std::string_view kShadowDirName = "shadow";
inline constexpr std::string_view kTemplatesDirName = "templates";
inline constexpr std::string_view kBodiesDirName = "bodies";
inline constexpr std::string_view kLockFilename = ".lock";

/// The committed `.gitattributes` rules from spec §4.
inline constexpr std::array<std::string_view, 4> kGitattributesRules = {
    ".rohrpost/log.jsonl          merge=union text eol=lf",
    ".rohrpost/archive/*.jsonl    merge=union text eol=lf",
    ".rohrpost/shadow/**/*.json   merge=ours",
    ".rohrpost/tickets.jsonl      linguist-generated",
};

/// The gitignored snapshot (regenerable).
inline constexpr std::array<std::string_view, 1> kGitignoreRules = {".rohrpost/tickets.jsonl"};

/// Walk up from `start` (default cwd) to the nearest directory containing `.git`.
[[nodiscard]] std::optional<std::filesystem::path> find_git_root(
    std::optional<std::filesystem::path> start = std::nullopt);

/// Walk up from `start` to the nearest directory containing `.rohrpost/`.
[[nodiscard]] std::optional<std::filesystem::path> find_rohrpost_dir(
    std::optional<std::filesystem::path> start = std::nullopt);

/// The `.rohrpost/` dir or throw StoreError if uninitialised.
[[nodiscard]] std::filesystem::path require_rohrpost_dir(
    std::optional<std::filesystem::path> start = std::nullopt);

[[nodiscard]] std::filesystem::path config_path(const std::filesystem::path& rohrpost_dir);
[[nodiscard]] std::filesystem::path log_path(const std::filesystem::path& rohrpost_dir);
[[nodiscard]] std::filesystem::path snapshot_path(const std::filesystem::path& rohrpost_dir);
[[nodiscard]] std::filesystem::path archive_dir(const std::filesystem::path& rohrpost_dir);
[[nodiscard]] std::filesystem::path shadow_dir(const std::filesystem::path& rohrpost_dir);
[[nodiscard]] std::filesystem::path templates_dir(const std::filesystem::path& rohrpost_dir);
[[nodiscard]] std::filesystem::path bodies_dir(const std::filesystem::path& rohrpost_dir);
[[nodiscard]] std::filesystem::path lock_path(const std::filesystem::path& rohrpost_dir);

/// Sorted `archive/*.jsonl` files (oldest first). Empty if none.
[[nodiscard]] std::vector<std::filesystem::path> archive_files(const std::filesystem::path& rohrpost_dir);

/// Create the full directory scaffold if missing (idempotent).
void ensure_layout(const std::filesystem::path& rohrpost_dir);

/// Ensure `.gitattributes` carries the merge and line-ending rules. Returns whether changed.
bool write_gitattributes(const std::filesystem::path& repo_root);

/// Ensure the snapshot is gitignored. Returns whether changed.
bool write_gitignore(const std::filesystem::path& repo_root);

/// `Path.cwd().resolve()`.
[[nodiscard]] std::filesystem::path resolved_cwd();

}  // namespace rp::paths
