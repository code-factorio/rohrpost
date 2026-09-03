#include "rohrpost/paths.hpp"

#include "rohrpost/errors.hpp"
#include "rohrpost/io.hpp"

#include <algorithm>
#include <array>
#include <cstddef>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace rp::paths {
namespace {

namespace fs = std::filesystem;

fs::path resolve(const fs::path& p) {
    std::error_code ec;
    fs::path out = fs::weakly_canonical(p, ec);
    return ec ? fs::absolute(p) : out;
}

/// `[here, *here.parents]`.
std::vector<fs::path> ancestry(const fs::path& here) {
    std::vector<fs::path> out{here};
    fs::path current = here;
    for (;;) {
        const fs::path parent = current.parent_path();
        if (parent.empty() || parent == current) break;
        out.push_back(parent);
        current = parent;
    }
    return out;
}

/// Python's `line not in existing` after universal-newline decoding.
std::string normalise_newlines(std::string_view text) {
    std::string out;
    out.reserve(text.size());
    for (std::size_t i = 0; i < text.size(); ++i) {
        if (text[i] == '\r') {
            out.push_back('\n');
            if (i + 1 < text.size() && text[i + 1] == '\n') ++i;
        } else {
            out.push_back(text[i]);
        }
    }
    return out;
}

/// Append any of `lines` not already present to `path`. Returns whether changed.
template <std::size_t N>
bool append_unique_lines(const fs::path& path, const std::array<std::string_view, N>& lines) {
    std::string existing;
    std::error_code ec;
    if (fs::is_regular_file(path, ec)) {
        auto read = io::read_file(path);
        if (!read) throw StoreError("cannot read " + io::path_str(path) + ": " + read.error());
        existing = normalise_newlines(*read);
    }
    std::vector<std::string_view> fresh;
    for (const auto line : lines) {
        if (existing.find(line) == std::string::npos) fresh.push_back(line);
    }
    if (fresh.empty()) return false;
    std::string payload;
    if (!existing.empty() && !existing.ends_with('\n')) payload.push_back('\n');
    for (std::size_t i = 0; i < fresh.size(); ++i) {
        if (i > 0) payload.push_back('\n');
        payload.append(fresh[i]);
    }
    payload.push_back('\n');
    io::append_file(path, payload);
    return true;
}

}  // namespace

fs::path resolved_cwd() {
    return resolve(fs::current_path());
}

std::optional<fs::path> find_git_root(std::optional<fs::path> start) {
    const fs::path here = start ? resolve(*start) : resolved_cwd();
    for (const auto& candidate : ancestry(here)) {
        std::error_code ec;
        if (fs::exists(candidate / ".git", ec)) return candidate;
    }
    return std::nullopt;
}

std::optional<fs::path> find_rohrpost_dir(std::optional<fs::path> start) {
    const fs::path here = start ? resolve(*start) : resolved_cwd();
    for (const auto& candidate : ancestry(here)) {
        std::error_code ec;
        if (fs::is_directory(candidate / kRohrpostDirName, ec)) return candidate / kRohrpostDirName;
    }
    return std::nullopt;
}

fs::path require_rohrpost_dir(std::optional<fs::path> start) {
    auto found = find_rohrpost_dir(std::move(start));
    if (!found) throw StoreError("not a rohrpost repository (no .rohrpost/ found). Run `rp init` first.");
    return *found;
}

fs::path config_path(const fs::path& d) { return d / kConfigFilename; }
fs::path log_path(const fs::path& d) { return d / kLogFilename; }
fs::path snapshot_path(const fs::path& d) { return d / kSnapshotFilename; }
fs::path archive_dir(const fs::path& d) { return d / kArchiveDirName; }
fs::path shadow_dir(const fs::path& d) { return d / kShadowDirName; }
fs::path templates_dir(const fs::path& d) { return d / kTemplatesDirName; }
fs::path bodies_dir(const fs::path& d) { return d / kBodiesDirName; }
fs::path lock_path(const fs::path& d) { return d / kLockFilename; }

std::vector<fs::path> archive_files(const fs::path& rohrpost_dir) {
    const fs::path adir = archive_dir(rohrpost_dir);
    std::error_code ec;
    if (!fs::is_directory(adir, ec)) return {};
    std::vector<fs::path> out;
    for (const auto& entry : fs::directory_iterator(adir, ec)) {
        if (entry.path().extension() == ".jsonl") out.push_back(entry.path());
    }
    std::sort(out.begin(), out.end());
    return out;
}

void ensure_layout(const fs::path& rohrpost_dir) {
    std::error_code ec;
    for (const auto& d : {rohrpost_dir, archive_dir(rohrpost_dir), shadow_dir(rohrpost_dir), templates_dir(rohrpost_dir)}) {
        fs::create_directories(d, ec);
        if (ec) throw StoreError("cannot create " + io::path_str(d) + ": " + ec.message());
    }
    const fs::path log = log_path(rohrpost_dir);
    if (!fs::exists(log, ec)) io::write_file(log, "");
}

bool write_gitattributes(const fs::path& repo_root) {
    return append_unique_lines(repo_root / ".gitattributes", kGitattributesRules);
}

bool write_gitignore(const fs::path& repo_root) {
    return append_unique_lines(repo_root / ".gitignore", kGitignoreRules);
}

}  // namespace rp::paths
