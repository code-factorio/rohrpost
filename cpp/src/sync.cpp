#include "rohrpost/sync.hpp"

#include "rohrpost/errors.hpp"
#include "rohrpost/merge.hpp"
#include "rohrpost/shadow.hpp"
#include "rohrpost/store.hpp"

#include <algorithm>
#include <format>
#include <set>

namespace rp::sync {
namespace {

namespace fs = std::filesystem;

int SyncReport_sum(const std::vector<TicketSync>& tickets, int TicketSync::*member) {
    int total = 0;
    for (const auto& t : tickets) total += t.*member;
    return total;
}

std::set<std::string> mapped_fields(const Json& remote_config) {
    std::set<std::string> out;
    const auto it = remote_config.find("fields");
    if (it != remote_config.end() && it->is_object()) {
        for (const auto& [k, v] : it->items()) out.insert(k);
    }
    return out;
}

merge::Policy policy_of(const Json& remote_config) {
    const auto it = remote_config.find("policy");
    if (it != remote_config.end() && it->is_string()) {
        const std::string p = it->get<std::string>();
        if (p == "local") return merge::Policy::Local;
        if (p == "remote") return merge::Policy::Remote;
    }
    return merge::Policy::Flag;
}

/// Extract the fields the merge engine understands from a ticket.
Json ticket_fields(const Ticket& t) {
    Json out = Json::object();
    out["title"] = t.title;
    out["body"] = t.body ? Json(*t.body) : Json();
    out["status"] = t.status;
    out["priority"] = t.priority;
    out["labels"] = t.labels;
    return out;
}

Json filter_mapped(const Json& fields, const std::set<std::string>& mapped) {
    Json out = Json::object();
    if (!fields.is_object()) return out;
    for (const auto& [k, v] : fields.items()) {
        if (mapped.contains(k)) out[k] = v;
    }
    return out;
}

struct Ctx {
    fs::path repo;
    std::string remote;
    std::string who;
    const api::Sources& sources;
    bool dry_run;
};

bool is_flagged(const Ticket& t, const std::string& remote) {
    return std::find(t.labels.begin(), t.labels.end(), "conflict:" + remote) != t.labels.end();
}

bool append_comment_once(const Ctx& ctx, const std::string& tid, const Ticket& ticket, const std::string& text) {
    for (const auto& c : ticket.comments) {
        if (c.actor == ctx.who && c.text == text) return false;
    }
    Event e;
    e.id = ctx.sources.ulid();
    e.ts = ctx.sources.now();
    e.ticket = tid;
    e.op = "comment";
    e.actor = ctx.who;
    e.text = text;
    store::append_event(ctx.repo, e);
    return true;
}

/// Translate merged whole values to the event log's scalar/set operations.
Json event_payload(const Ticket& ticket, const Json& fields) {
    Json payload = Json::object();
    const Json current_fields = ticket_fields(ticket);
    for (const auto& [name, value] : fields.items()) {
        if (name != "labels") {
            if (!json::py_equal(current_fields.contains(name) ? current_fields[name] : Json(), value)) payload[name] = value;
            continue;
        }
        const std::set<std::string> current(ticket.labels.begin(), ticket.labels.end());
        std::set<std::string> target;
        if (value.is_array()) {
            for (const auto& item : value) target.insert(json::py_str(item));
        }
        Json added = Json::array();
        Json removed = Json::array();
        for (const auto& l : target) {
            if (!current.contains(l)) added.push_back(l);
        }
        for (const auto& l : current) {
            if (!target.contains(l)) removed.push_back(l);
        }
        if (!added.empty()) payload["labels+"] = added;
        if (!removed.empty()) payload["labels-"] = removed;
    }
    return payload;
}

bool append_set(const Ctx& ctx, const std::string& tid, const Ticket& ticket, const Json& fields) {
    const Json payload = event_payload(ticket, fields);
    if (payload.empty()) return false;
    Event e;
    e.id = ctx.sources.ulid();
    e.ts = ctx.sources.now();
    e.ticket = tid;
    e.op = "set";
    e.actor = ctx.who;
    e.set = payload;
    store::append_event(ctx.repo, e);
    return true;
}

std::string conflict_detail(const std::vector<merge::FieldConflict>& conflicts) {
    std::string detail;
    for (std::size_t i = 0; i < conflicts.size(); ++i) {
        if (i) detail += "; ";
        detail += std::format("{}: local={} remote={}", conflicts[i].field, json::py_repr(conflicts[i].local), json::py_repr(conflicts[i].remote));
    }
    return detail;
}

bool flag_conflict(const Ctx& ctx, const std::string& tid, const Ticket& ticket,
                   const std::vector<merge::FieldConflict>& conflicts, const Json& inbound) {
    const std::string comment = std::format("sync conflict with {} — {}", ctx.remote, conflict_detail(conflicts));
    const bool changed = append_comment_once(ctx, tid, ticket, comment);
    Json updates = inbound.is_object() ? inbound : Json::object();
    updates["status"] = "review";
    std::set<std::string> target_labels;
    if (updates.contains("labels") && updates["labels"].is_array()) {
        for (const auto& item : updates["labels"]) target_labels.insert(json::py_str(item));
    } else {
        target_labels.insert(ticket.labels.begin(), ticket.labels.end());
    }
    target_labels.insert("conflict:" + ctx.remote);
    updates["labels"] = std::vector<std::string>(target_labels.begin(), target_labels.end());
    const bool wrote = append_set(ctx, tid, ticket, updates);
    return wrote || changed;
}

bool record_resolution(const Ctx& ctx, const std::string& tid, const Ticket& ticket, const merge::FieldConflict& conflict,
                       const Json& local_won) {
    const char* winner = local_won.contains(conflict.field) ? "local" : "remote";
    const std::string text = std::format("sync conflict with {} resolved by {} policy — {}: local={} remote={}", ctx.remote, winner,
                                         conflict.field, json::py_repr(conflict.local), json::py_repr(conflict.remote));
    return append_comment_once(ctx, tid, ticket, text);
}

bool flag_deleted(const Ctx& ctx, const std::string& tid, const std::string& ref, const Ticket& ticket) {
    const merge::FieldConflict conflict{"remote", Json(std::format("linked ticket {}", tid)), Json(std::format("missing item {}", ref)), std::nullopt};
    if (ctx.dry_run) {
        const std::string text = std::format("sync conflict with {} — remote: local={} remote={}", ctx.remote, json::py_repr(conflict.local), json::py_repr(conflict.remote));
        bool has_comment = false;
        for (const auto& c : ticket.comments) {
            if (c.actor == ctx.who && c.text == text) has_comment = true;
        }
        return !has_comment || !is_flagged(ticket, ctx.remote) || ticket.status != "review";
    }
    return flag_conflict(ctx, tid, ticket, {conflict}, Json::object());
}

bool changed_during_fetch(const fs::path& repo, const std::string& tid, const Ticket& before, const std::set<std::string>& mapped) {
    const TicketMap current_map = fold_all(repo);
    const Ticket* current = current_map.find(tid);
    if (current == nullptr) return true;
    return !json::py_equal(filter_mapped(ticket_fields(*current), mapped), filter_mapped(ticket_fields(before), mapped));
}

struct Merged {
    merge::MergeResult result;
    Json live;
    Json base;
    bool had_shadow;
};

Merged merge_ticket(const Ctx& ctx, const Ticket& ticket, providers::Provider& provider, const Json& remote_config,
                    merge::Policy policy) {
    const std::string* ref_ptr = ticket.remotes.find(ctx.remote);
    const std::string ref = ref_ptr ? *ref_ptr : "";
    const auto mapped = mapped_fields(remote_config);
    const Json live = filter_mapped(provider.fetch(ref), mapped);
    const auto stored = shadow::read_shadow(ctx.repo, ctx.remote, ref);
    const Json base = filter_mapped(stored.value_or(Json::object()), mapped);
    const Json local = filter_mapped(ticket_fields(ticket), mapped);
    if (!stored) {
        // A missing/corrupt shadow is not enough information to choose a
        // winner: establish a base without touching either side.
        return Merged{merge::MergeResult{}, live, base, false};
    }
    return Merged{merge::three_way(base, local, live, policy), live, base, true};
}

bool has_planned_change(const merge::MergeResult& m, const Json& live, const Json& base, bool had_shadow) {
    return !m.remote_won.empty() || !m.local_won.empty() || !m.conflicts.empty() || !m.resolved.empty() ||
           !had_shadow || !json::py_equal(live, base);
}

TicketSync sync_ticket(const Ctx& ctx, const std::string& tid, const Ticket& ticket, providers::Provider& provider,
                       const Json& remote_config, merge::Policy policy, const std::set<std::string>& mapped) {
    const std::string ref = *ticket.remotes.find(ctx.remote);
    Merged merged;
    try {
        merged = merge_ticket(ctx, ticket, provider, remote_config, policy);
    } catch (const RemoteItemNotFoundError&) {
        const bool changed = flag_deleted(ctx, tid, ref, ticket);
        return TicketSync{tid, ref, 0, 0, {"remote"}, changed};
    }
    // _apply_merge
    const bool completed_conflict = merged.had_shadow && is_flagged(ticket, ctx.remote) && json::py_equal(merged.live, merged.base);
    if (completed_conflict || changed_during_fetch(ctx.repo, tid, ticket, mapped)) {
        std::vector<std::string> conflicts;
        for (const auto& c : merged.result.conflicts) conflicts.push_back(c.field);
        return TicketSync{tid, ref, 0, 0, conflicts, false};
    }
    const int pulled = static_cast<int>(merged.result.remote_won.size());
    int pushed = static_cast<int>(merged.result.local_won.size());
    std::vector<std::string> conflicts;
    for (const auto& c : merged.result.conflicts) conflicts.push_back(c.field);
    if (ctx.dry_run) {
        return TicketSync{tid, ref, pulled, pushed, conflicts, has_planned_change(merged.result, merged.live, merged.base, merged.had_shadow)};
    }
    // _apply_local_merge
    bool changed;
    if (!merged.result.conflicts.empty()) {
        changed = flag_conflict(ctx, tid, ticket, merged.result.conflicts, merged.result.remote_won);
    } else {
        changed = !merged.result.remote_won.empty() && append_set(ctx, tid, ticket, merged.result.remote_won);
    }
    for (const auto& conflict : merged.result.resolved) {
        changed = record_resolution(ctx, tid, ticket, conflict, merged.result.local_won) || changed;
    }
    // _push_merge
    Json live = merged.live;
    bool pushed_change = false;
    if (!merged.result.local_won.empty()) {
        live = provider.push(ref, merged.result.local_won);
        pushed_change = true;
    }
    // _update_shadow
    bool shadow_change = false;
    if (!(merged.had_shadow && json::py_equal(live, merged.base))) {
        shadow::write_shadow(ctx.repo, ctx.remote, ref, live);
        shadow_change = true;
    }
    changed = changed || pushed_change || shadow_change;
    return TicketSync{tid, ref, pulled, pushed, conflicts, changed};
}

void append_synced(const fs::path& dir, const std::string& remote, const std::string& who, const api::Sources& s) {
    const std::string timestamp = s.now();
    Event e;
    e.id = s.ulid();
    e.ts = timestamp;
    e.ticket = std::string(kSyncTicket);
    e.op = "synced";
    e.actor = who;
    e.remote = remote;
    e.at = timestamp;
    store::append_event(dir, e);
}

}  // namespace

int SyncReport::pulled() const { return SyncReport_sum(tickets, &TicketSync::pulled); }
int SyncReport::pushed() const { return SyncReport_sum(tickets, &TicketSync::pushed); }
int SyncReport::conflicts() const {
    int total = 0;
    for (const auto& t : tickets) total += static_cast<int>(t.conflicts.size());
    return total;
}

SyncReport sync_round(const fs::path& rohrpost_dir, const std::string& remote, providers::Provider& provider,
                      const Config& config, const SyncOptions& options) {
    const Json* raw = config.remotes.find(remote);
    if (raw == nullptr) throw ConfigError(std::format("no [remotes.{}] configured", remote));
    const Json& remote_config = *raw;
    const merge::Policy policy = policy_of(remote_config);
    const std::string who = options.actor.value_or("remote/" + remote);
    SyncReport report{remote, {}};

    const TicketMap by_id = load_tickets(rohrpost_dir);
    std::vector<std::pair<std::string, Ticket>> linked;
    for (const auto& [tid, t] : by_id) {
        if (t.remotes.contains(remote)) linked.emplace_back(tid, t);
    }
    const Ctx ctx{rohrpost_dir, remote, who, options.sources, options.dry_run};
    const auto mapped = mapped_fields(remote_config);
    for (const auto& [tid, ticket] : linked) {
        report.tickets.push_back(sync_ticket(ctx, tid, ticket, provider, remote_config, policy, mapped));
    }
    const bool any_changed = std::any_of(report.tickets.begin(), report.tickets.end(), [](const TicketSync& t) { return t.changed; });
    if (!options.dry_run && any_changed) append_synced(rohrpost_dir, remote, who, options.sources);
    return report;
}

}  // namespace rp::sync
