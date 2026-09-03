// The fold: turning the append-only event log into the ticket snapshot
// (mirrors src/rohrpost/fold.py, spec §6).
//
// Dedupe by event id, sort by (ts, id), replay field by field with per-field
// last-write-wins. Tickets are kept in first-appearance order (Python dict
// order), which the CLI's stable sorts and `doctor`'s cycle report rely on.
#pragma once

#include "rohrpost/events.hpp"
#include "rohrpost/json.hpp"
#include "rohrpost/ordered_map.hpp"

#include <array>
#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace rp {

/// Stored status values (spec §5.4); `ready` is derived and never written.
inline constexpr std::array<std::string_view, 6> kStatuses = {"open", "in_progress", "review", "waiting", "done", "dropped"};
/// Terminal statuses.
inline constexpr std::array<std::string_view, 2> kTerminal = {"done", "dropped"};
/// Ticket types (spec §5.3).
inline constexpr std::array<std::string_view, 4> kTypes = {"task", "bug", "spike", "epic"};
/// Scalar fields updated by field-level last-write-wins.
inline constexpr std::array<std::string_view, 7> kScalarFields = {"title", "type", "status", "priority", "assignee", "parent", "body"};
/// Array fields updated by set add/remove ops.
inline constexpr std::array<std::string_view, 2> kSetFields = {"labels", "blocked_by"};

inline constexpr std::string_view kDefaultType = "task";
inline constexpr std::string_view kDefaultStatus = "open";
inline constexpr std::int64_t kDefaultPriority = 2;

[[nodiscard]] bool is_status(std::string_view value);
[[nodiscard]] bool is_terminal(std::string_view value);
[[nodiscard]] bool is_type(std::string_view value);
[[nodiscard]] bool is_scalar_field(std::string_view value);
[[nodiscard]] bool is_set_field(std::string_view value);

/// A local note appended to a ticket (spec §9). Never synced.
struct Comment {
    std::string ts;
    std::string actor;
    std::string text;
    bool operator==(const Comment&) const = default;
};

/// The folded shape of a ticket (spec §5.3); ids are bare internally.
struct Ticket {
    std::string id;
    std::string title;
    std::string type;
    std::string status;
    std::int64_t priority = kDefaultPriority;
    std::optional<std::string> parent;
    std::vector<std::string> blocked_by;  // sorted
    std::vector<std::string> labels;      // sorted
    std::optional<std::string> assignee;
    std::optional<std::string> body;
    OrderedMap<std::string, std::string> remotes;
    std::optional<std::string> last_close_reason;
    std::vector<Comment> comments;
    std::string created;
    std::string updated;
    OrderedMap<std::string, std::string> fieldts;

    bool operator==(const Ticket&) const = default;
};

using TicketMap = OrderedMap<std::string, Ticket>;

/// Fold events into `{bare_id: Ticket}`; input need not be sorted or unique.
[[nodiscard]] TicketMap fold(const std::vector<Event>& events);

/// Deduplicate by id then sort by (ts, id).
[[nodiscard]] std::vector<Event> dedup_sort(std::vector<Event> events);

/// Stored status, except epics with children (`done` when every child is).
[[nodiscard]] std::string derive_status(const Ticket& ticket, const TicketMap& by_id);

/// True if actionable now: `open`, non-epic, every blocker `done`.
[[nodiscard]] bool is_ready(const Ticket& ticket, const TicketMap& by_id);

/// A cyclic path of bare ids via `blocked_by`, or nullopt if acyclic.
[[nodiscard]] std::optional<std::vector<std::string>> find_cycle(const TicketMap& by_id);

/// Read + fold the whole log.
[[nodiscard]] TicketMap fold_all(const std::filesystem::path& rohrpost_dir);

/// The folded tickets, using the `tickets.jsonl` cache when strictly newer than the log.
[[nodiscard]] TicketMap load_tickets(const std::filesystem::path& rohrpost_dir);

struct MappingOptions {
    std::optional<std::string> prefix;
    bool include_fieldts = true;
    bool include_comments = true;
    bool include_body = true;
};

/// Convert a ticket to a plain JSON mapping (the snapshot and `--json` shape).
[[nodiscard]] Json ticket_to_mapping(const Ticket& ticket, const MappingOptions& options = {});
[[nodiscard]] Json comment_to_mapping(const Comment& comment);

/// Inverse of ticket_to_mapping for snapshot reads (bare ids assumed).
[[nodiscard]] Ticket mapping_to_ticket(const Json& obj);

/// Read a snapshot file, or nullopt if it cannot be parsed.
[[nodiscard]] std::optional<TicketMap> read_snapshot(const std::filesystem::path& snap);

/// `_bare_id`: normalise defensively, returning the input unchanged only for
/// the (never raised) StoreError the reference catches — an invalid id throws IdError.
[[nodiscard]] std::string bare_id(std::string_view value);

}  // namespace rp
