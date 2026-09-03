#include "rohrpost/shadow.hpp"

#include "rohrpost/entropy.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/paths.hpp"

#include <algorithm>
#include <filesystem>
#include <format>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace rp::shadow {
namespace {

namespace fs = std::filesystem;

std::string safe_component(std::string_view text) {
    std::string out(text);
    std::replace(out.begin(), out.end(), '/', '_');
    std::replace(out.begin(), out.end(), '\\', '_');
    return out;
}

}  // namespace

fs::path shadow_path(const fs::path& rohrpost_dir, std::string_view remote, std::string_view ref) {
    return paths::shadow_dir(rohrpost_dir) / io::path_from_utf8(safe_component(remote)) /
           io::path_from_utf8(safe_component(ref) + ".json");
}

std::optional<Json> read_shadow(const fs::path& rohrpost_dir, std::string_view remote, std::string_view ref) {
    const fs::path path = shadow_path(rohrpost_dir, remote, ref);
    std::error_code ec;
    if (!fs::is_regular_file(path, ec)) return std::nullopt;
    auto content = io::read_file(path);
    if (!content) return std::nullopt;
    auto parsed = json::parse(*content);
    if (!parsed || !parsed->is_object()) return std::nullopt;
    return std::move(*parsed);
}

void write_shadow(const fs::path& rohrpost_dir, std::string_view remote, std::string_view ref, const Json& fields) {
    const fs::path path = shadow_path(rohrpost_dir, remote, ref);
    std::error_code ec;
    fs::create_directories(path.parent_path(), ec);
    const fs::path tmp = path.parent_path() /
                         io::path_from_utf8(std::format(".{}.{:016x}", io::path_str(path.filename()), entropy::randbits(64)));
    io::write_file_atomic(path, tmp, json::dumps(fields, json::kSortedRaw));
}

std::vector<std::pair<std::string, std::string>> all_shadowed(const fs::path& rohrpost_dir) {
    const fs::path root = paths::shadow_dir(rohrpost_dir);
    std::error_code ec;
    if (!fs::is_directory(root, ec)) return {};
    std::vector<fs::path> remote_dirs;
    for (const auto& entry : fs::directory_iterator(root, ec)) {
        if (entry.is_directory()) remote_dirs.push_back(entry.path());
    }
    std::sort(remote_dirs.begin(), remote_dirs.end());
    std::vector<std::pair<std::string, std::string>> out;
    for (const auto& remote_dir : remote_dirs) {
        std::vector<fs::path> files;
        for (const auto& entry : fs::directory_iterator(remote_dir, ec)) {
            if (entry.path().extension() == ".json") files.push_back(entry.path());
        }
        std::sort(files.begin(), files.end());
        for (const auto& f : files) out.emplace_back(io::path_str(remote_dir.filename()), io::path_str(f.stem()));
    }
    return out;
}

}  // namespace rp::shadow
