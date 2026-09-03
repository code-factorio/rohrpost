#include "rohrpost/config.hpp"

#include "rohrpost/errors.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/paths.hpp"
#include "rohrpost/pyfmt.hpp"

#include <tomlplusplus/toml.hpp>

#include <algorithm>
#include <expected>
#include <filesystem>
#include <format>
#include <sstream>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace rp {
namespace toml_compat {
namespace {

Json convert(const toml::node& node);

/// Table entries sorted by source position: toml++ stores keys sorted, the
/// reference (tomllib) keeps document order, and order is load-bearing for
/// status mappings (first match wins in `_state_to_status`).
Json convert_table(const toml::table& table) {
    std::vector<std::pair<const toml::key*, const toml::node*>> entries;
    for (const auto& [key, value] : table) entries.emplace_back(&key, &value);
    std::stable_sort(entries.begin(), entries.end(), [](const auto& a, const auto& b) {
        const auto& pa = a.second->source().begin;
        const auto& pb = b.second->source().begin;
        // A subtable declared by a later header sorts by that header; entries
        // inside it come later still, so comparing the node start is enough.
        return pa.line != pb.line ? pa.line < pb.line : pa.column < pb.column;
    });
    Json out = Json::object();
    for (const auto& [key, value] : entries) out[std::string(key->str())] = convert(*value);
    return out;
}

Json convert(const toml::node& node) {
    switch (node.type()) {
        case toml::node_type::table: return convert_table(*node.as_table());
        case toml::node_type::array: {
            Json out = Json::array();
            for (const auto& item : *node.as_array()) out.push_back(convert(item));
            return out;
        }
        case toml::node_type::string: return Json(std::string(node.as_string()->get()));
        case toml::node_type::integer: return Json(node.as_integer()->get());
        case toml::node_type::floating_point: return Json(node.as_floating_point()->get());
        case toml::node_type::boolean: return Json(node.as_boolean()->get());
        case toml::node_type::date: {
            std::ostringstream ss;
            ss << node.as_date()->get();
            return Json(ss.str());
        }
        case toml::node_type::time: {
            std::ostringstream ss;
            ss << node.as_time()->get();
            return Json(ss.str());
        }
        case toml::node_type::date_time: {
            std::ostringstream ss;
            ss << node.as_date_time()->get();
            return Json(ss.str());
        }
        default: return Json();
    }
}

}  // namespace

std::expected<Json, std::string> parse(std::string_view text) {
    toml::parse_result result = toml::parse(text);
    if (!result) {
        std::ostringstream ss;
        ss << result.error().description() << " (at line " << result.error().source().begin.line
           << ", column " << result.error().source().begin.column << ")";
        return std::unexpected(ss.str());
    }
    return convert_table(result.table());
}

}  // namespace toml_compat

std::string validate_prefix(std::string_view prefix) {
    std::string candidate(py::strip(prefix));
    for (char& c : candidate) {
        if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 'a' + 'A');
    }
    const bool ok = candidate.size() >= 2 && candidate.size() <= 5 &&
                    std::all_of(candidate.begin(), candidate.end(), [](char c) { return c >= 'A' && c <= 'Z'; });
    if (!ok) {
        throw ConfigError(std::format("prefix must be 2-5 uppercase letters (e.g. 'FAC'), got {}", py::repr(prefix)));
    }
    return candidate;
}

Config default_config() {
    return Config{};
}

Config load_config(const std::filesystem::path& rohrpost_dir) {
    const std::filesystem::path path = rohrpost_dir / paths::kConfigFilename;
    std::error_code ec;
    if (!std::filesystem::is_regular_file(path, ec)) return default_config();

    auto raw = io::read_file(path);
    if (!raw) throw ConfigError(std::format("invalid {}: {}", paths::kConfigFilename, raw.error()));
    auto data = toml_compat::parse(*raw);
    if (!data) throw ConfigError(std::format("invalid {}: {}", paths::kConfigFilename, data.error()));

    Json project = data->contains("project") ? (*data)["project"] : Json::object();
    if (!project.is_object()) throw ConfigError("[project] must be a table");

    Json raw_prefix = project.contains("prefix") ? project["prefix"] : Json(std::string(kDefaultPrefix));
    if (!raw_prefix.is_string()) throw ConfigError("[project].prefix must be a string");
    Config config;
    config.prefix = validate_prefix(raw_prefix.get<std::string>());

    if (project.contains("default_branch") && !project["default_branch"].is_null()) {
        if (!project["default_branch"].is_string()) throw ConfigError("[project].default_branch must be a string");
        config.default_branch = project["default_branch"].get<std::string>();
    }

    Json remotes_raw = data->contains("remotes") ? (*data)["remotes"] : Json::object();
    if (!remotes_raw.is_object()) throw ConfigError("[remotes] must be a table");
    for (const auto& [name, table] : remotes_raw.items()) {
        if (table.is_object()) config.remotes.insert_or_assign(name, table);
    }
    return config;
}

std::string render_config_toml(std::string_view prefix) {
    return std::format(
        "# Rohrpost project configuration. Committed; safe to hand-edit.\n"
        "# The prefix is DISPLAY-ONLY: it never enters the event log, so\n"
        "# renaming it here re-renders every ticket id with no migration.\n"
        "\n"
        "[project]\nprefix = \"{}\"\n"
        "\n"
        "# [remotes.github]   # phase 1: see spec §8\n"
        "# url = \"https://api.github.com\"\n"
        "# repo = \"owner/name\"\n",
        prefix);
}

}  // namespace rp
