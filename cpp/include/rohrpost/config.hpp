// Project configuration: `.rohrpost/config.toml` (mirrors src/rohrpost/config.py).
//
// The prefix is display-only (spec §5.1). `[remotes.*]` tables are passed
// through as JSON values, in document order, for the sync layer to interpret.
#pragma once

#include "rohrpost/json.hpp"
#include "rohrpost/ordered_map.hpp"

#include <expected>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>

namespace rp {

inline constexpr std::string_view kDefaultPrefix = "RP";

/// Parsed `config.toml`. Value object.
struct Config {
    std::string prefix = std::string(kDefaultPrefix);
    std::optional<std::string> default_branch;
    OrderedMap<std::string, Json> remotes;
};

/// Normalise and validate a project prefix (`fac` -> `FAC`). Throws ConfigError.
[[nodiscard]] std::string validate_prefix(std::string_view prefix);

/// The config used when `config.toml` is absent.
[[nodiscard]] Config default_config();

/// Load and validate `config.toml`; a missing file yields the default config.
[[nodiscard]] Config load_config(const std::filesystem::path& rohrpost_dir);

/// The minimal committed `config.toml` body written by `rp init`.
[[nodiscard]] std::string render_config_toml(std::string_view prefix);

namespace toml_compat {
/// Parse TOML text into a JSON value (tables become objects in document order,
/// datetimes become strings). The error is the parser's message.
[[nodiscard]] std::expected<Json, std::string> parse(std::string_view text);
}  // namespace toml_compat

}  // namespace rp
