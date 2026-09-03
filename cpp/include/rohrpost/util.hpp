// The actor-resolution policy (mirrors src/rohrpost/util.py).
#pragma once

#include <functional>
#include <optional>
#include <string>
#include <string_view>

namespace rp {

/// Environment lookup, injectable for tests.
using EnvLookup = std::function<std::optional<std::string>(std::string_view)>;

/// Resolve the actor string for an event: explicit `--actor` > ROHRPOST_ACTOR >
/// ROHRPOST_RUNNER[@ROHRPOST_BATCH] > `user/<git config user.email>` >
/// `user/<login>` > `user/unknown`.
[[nodiscard]] std::string resolve_actor(std::optional<std::string> explicit_actor = std::nullopt,
                                        const EnvLookup& env = nullptr);

}  // namespace rp
