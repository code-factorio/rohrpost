// The sync round: bidirectional three-way merge per linked ticket
// (spec §8.4; mirrors src/rohrpost/sync.py).
#pragma once

#include "rohrpost/api.hpp"
#include "rohrpost/config.hpp"
#include "rohrpost/providers/provider.hpp"

#include <array>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace rp::sync {

/// Ticket fields supported by the provider mapping and merge engine.
inline constexpr std::array<std::string_view, 5> kSyncedFields = {"title", "body", "status", "priority", "labels"};

/// Per-ticket outcome of a sync round.
struct TicketSync {
    std::string ticket;
    std::string ref;
    int pulled = 0;
    int pushed = 0;
    std::vector<std::string> conflicts;
    bool changed = false;
};

/// Aggregate outcome of a sync round over one remote.
struct SyncReport {
    std::string remote;
    std::vector<TicketSync> tickets;
    [[nodiscard]] int pulled() const;
    [[nodiscard]] int pushed() const;
    [[nodiscard]] int conflicts() const;
};

struct SyncOptions {
    bool dry_run = false;
    std::optional<std::string> actor;
    api::Sources sources;
};

/// Run one sync round against `remote`.
[[nodiscard]] SyncReport sync_round(const std::filesystem::path& rohrpost_dir, const std::string& remote,
                                    providers::Provider& provider, const Config& config,
                                    const SyncOptions& options = {});

}  // namespace rp::sync
