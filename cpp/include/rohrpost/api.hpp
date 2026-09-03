// High-level ticket operations: the library behind the `rp` commands
// (mirrors src/rohrpost/api.py). This is the one write path: every mutation
// builds a well-formed Event, appends it through the store and returns the
// re-folded ticket. Mutations are idempotent: nothing is appended when the
// current state already satisfies the request.
#pragma once

#include "rohrpost/config.hpp"
#include "rohrpost/events.hpp"
#include "rohrpost/fold.hpp"
#include "rohrpost/ids.hpp"
#include "rohrpost/json.hpp"
#include "rohrpost/timeutil.hpp"

#include <cstdint>
#include <filesystem>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace rp::api {

using UlidFactory = std::function<std::string()>;

/// Outcome of a mutation: the ticket and whether an event was appended.
struct WriteResult {
    Ticket ticket;
    bool wrote;
};

/// One `field=value` / `field+=v,v` / `field-=v,v` directive.
struct Assignment {
    enum class Op { Set, Add, Remove };
    Op op;
    std::string field;
    Json value;  // string or int for Set; array of strings for Add/Remove
};

/// Parse a single `field=value` token. Throws TicketError.
[[nodiscard]] Assignment parse_assignment(std::string_view token);

/// Load and normalise ticket defaults from `templates/<name>.toml`. Throws TicketError.
[[nodiscard]] Json load_template(const std::filesystem::path& rohrpost_dir, std::string_view name);

/// Outcome of `rp init`.
struct InitResult {
    std::filesystem::path rohrpost_dir;
    std::string prefix;
    bool created_config;
    bool updated_gitattributes;
    bool updated_gitignore;
};

/// Derive a 2-5 letter uppercase prefix from a directory name (spec §5.1).
[[nodiscard]] std::string propose_prefix(const std::filesystem::path& directory);

/// Scaffold `.rohrpost/` and the committed git housekeeping files (idempotent).
[[nodiscard]] InitResult init_repo(std::optional<std::filesystem::path> target_dir = std::nullopt,
                                   std::optional<std::string> prefix = std::nullopt);

/// The injectable clock and id factory shared by the mutations.
struct Sources {
    Clock now = timeutil::now_ts;
    UlidFactory ulid = [] { return ids::new_ulid(); };
};

struct CreateOptions {
    std::string type = std::string(kDefaultType);
    std::int64_t priority = kDefaultPriority;
    std::optional<std::string> parent;
    std::vector<std::string> labels;
    std::vector<std::string> blocked_by;
    std::optional<std::string> assignee;
    std::optional<std::string> body;
};

[[nodiscard]] WriteResult create_ticket(const std::filesystem::path& dir, std::string_view title,
                                        const CreateOptions& options, const std::string& actor,
                                        const Sources& sources = {});

[[nodiscard]] WriteResult set_fields(const std::filesystem::path& dir, std::string_view ticket_ref,
                                     const std::vector<Assignment>& assignments, const std::string& actor,
                                     const Sources& sources = {});

[[nodiscard]] WriteResult claim(const std::filesystem::path& dir, std::string_view ticket_ref,
                                const std::string& actor, const Sources& sources = {});

[[nodiscard]] WriteResult close(const std::filesystem::path& dir, std::string_view ticket_ref,
                                std::optional<std::string> reason, const std::string& actor,
                                const Sources& sources = {});

[[nodiscard]] WriteResult drop(const std::filesystem::path& dir, std::string_view ticket_ref,
                               std::optional<std::string> reason, const std::string& actor,
                               const Sources& sources = {});

[[nodiscard]] WriteResult add_comment(const std::filesystem::path& dir, std::string_view ticket_ref,
                                      std::string_view text, const std::string& actor,
                                      const Sources& sources = {});

[[nodiscard]] WriteResult link_remote(const std::filesystem::path& dir, std::string_view ticket_ref,
                                      std::string_view remote, std::string_view ref, const std::string& actor,
                                      const Sources& sources = {});

[[nodiscard]] WriteResult unlink_remote(const std::filesystem::path& dir, std::string_view ticket_ref,
                                        std::string_view remote, const std::string& actor,
                                        const Sources& sources = {});

[[nodiscard]] Ticket show_ticket(const std::filesystem::path& dir, std::string_view ticket_ref);

struct ListFilter {
    std::optional<std::string> status;
    std::optional<std::string> label;
    std::optional<std::string> parent;
    std::optional<std::string> type;
    std::optional<std::string> match;
    bool ready = false;
};

[[nodiscard]] std::vector<Ticket> list_tickets(const std::filesystem::path& dir, const ListFilter& filter = {});
[[nodiscard]] std::vector<Ticket> ready_tickets(const std::filesystem::path& dir,
                                                std::optional<std::int64_t> limit = std::nullopt);
[[nodiscard]] std::vector<Ticket> list_conflicts(const std::filesystem::path& dir);

[[nodiscard]] WriteResult resolve_conflict(const std::filesystem::path& dir, std::string_view ticket_ref,
                                           std::string_view take, const std::string& actor,
                                           const Sources& sources = {});

/// An epic and its direct children.
struct Tree {
    Ticket root;
    std::vector<Ticket> children;
};
[[nodiscard]] Tree tree(const std::filesystem::path& dir, std::string_view ticket_ref);

/// Raw event history, optionally filtered to one ticket (sorted by ts, id).
[[nodiscard]] std::vector<Event> event_log(const std::filesystem::path& dir,
                                           std::optional<std::string> ticket_ref = std::nullopt);

[[nodiscard]] std::vector<Comment> comments(const std::filesystem::path& dir, std::string_view ticket_ref);

/// Load config, falling back to defaults when unreadable for store reasons.
[[nodiscard]] Config load_repo_config(const std::filesystem::path& dir);

/// Normalise a parent/blocked_by id to bare form (throws on invalid).
[[nodiscard]] std::string normalise_structural(std::string_view value);

}  // namespace rp::api
