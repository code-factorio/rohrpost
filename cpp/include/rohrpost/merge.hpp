// Three-way field merge for sync (spec §8.2, §8.3; mirrors src/rohrpost/merge.py).
#pragma once

#include "rohrpost/json.hpp"

#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace rp::merge {

/// Conflict resolution policies (spec §8.2). `flag` is the default.
enum class Policy { Flag, Local, Remote };

inline constexpr std::string_view kBodyField = "body";

/// One field where local, remote and base all differ.
struct FieldConflict {
    std::string field;
    Json local;
    Json remote;
    std::optional<Json> merged;
};

/// The outcome of a three-way merge over all mapped fields.
struct MergeResult {
    Json remote_won = Json::object();  // values to apply locally
    Json local_won = Json::object();   // values to push to the remote
    std::vector<FieldConflict> conflicts;
    std::vector<FieldConflict> resolved;
    [[nodiscard]] bool clean() const { return conflicts.empty(); }
};

/// Three-way text merge via `git merge-file -p`; `(merged, had_conflict)`.
/// Falls back to the local text if git is unavailable.
[[nodiscard]] std::pair<std::string, bool> merge_text(const std::string& base, const std::string& local,
                                                      const std::string& remote);

/// Resolve every mapped field three-way. `labels` composes as a set.
[[nodiscard]] MergeResult three_way(const Json& base, const Json& local, const Json& remote,
                                    Policy policy = Policy::Flag);

}  // namespace rp::merge
