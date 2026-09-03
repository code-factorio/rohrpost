#include "rohrpost/fold.hpp"

#include "rohrpost/errors.hpp"
#include "rohrpost/ids.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/paths.hpp"
#include "rohrpost/pyfmt.hpp"
#include "rohrpost/store.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <set>
#include <unordered_set>

namespace rp {
namespace {

template <std::size_t N>
bool contains(const std::array<std::string_view, N>& set, std::string_view value) {
    return std::find(set.begin(), set.end(), value) != set.end();
}

/// Mutable per-ticket accumulator used while replaying the log.
struct Builder {
    std::string id;
    std::optional<std::string> title;
    std::optional<std::string> type;
    std::optional<std::string> status;
    std::optional<std::int64_t> priority;
    std::optional<std::string> parent;
    std::optional<std::string> assignee;
    std::optional<std::string> body;
    std::set<std::string> labels;
    std::set<std::string> blocked_by;
    OrderedMap<std::string, std::string> remotes;
    std::optional<std::string> last_close_reason;
    std::vector<Comment> comments;
    std::optional<std::string> created;
    std::optional<std::string> updated;
    OrderedMap<std::string, std::string> fieldts;

    Ticket freeze() const {
        Ticket t;
        t.id = id;
        t.title = title.value_or("");
        t.type = type.value_or(std::string(kDefaultType));
        t.status = status.value_or(std::string(kDefaultStatus));
        t.priority = priority.value_or(kDefaultPriority);
        t.parent = parent;
        t.blocked_by.assign(blocked_by.begin(), blocked_by.end());
        t.labels.assign(labels.begin(), labels.end());
        t.assignee = assignee;
        t.body = body;
        t.remotes = remotes;
        t.last_close_reason = last_close_reason;
        t.comments = comments;
        t.created = created.value_or("");
        t.updated = updated.value_or("");
        t.fieldts = fieldts;
        return t;
    }
};

/// Coerce a set-op payload to strings; `blocked_by` values are normalised to bare ids.
std::vector<std::string> set_values(std::string_view field, const Json& value) {
    std::vector<std::string> out;
    if (value.is_array()) {
        for (const auto& item : value) out.push_back(json::py_str(item));
    } else {
        out.push_back(json::py_str(value));
    }
    if (field == "blocked_by") {
        for (auto& s : out) s = bare_id(s);
    }
    return out;
}

void apply_scalar(Builder& b, const std::string& key, const Json& value, const std::string& ts) {
    if (key == "priority") {
        // bool is nonsensical; anything else must look like a number or numeric
        // string. Malformed values are skipped so the fold stays total.
        if (value.is_boolean()) return;
        if (value.is_number_integer()) b.priority = value.get<std::int64_t>();
        else if (value.is_number_unsigned()) b.priority = static_cast<std::int64_t>(value.get<std::uint64_t>());
        else if (value.is_number_float()) {
            const double d = value.get<double>();
            if (!std::isfinite(d)) return;
            b.priority = static_cast<std::int64_t>(std::trunc(d));
        } else if (value.is_string()) {
            const auto parsed = py::parse_int(value.get<std::string>());
            if (!parsed) return;
            b.priority = *parsed;
        } else {
            return;
        }
        b.fieldts.insert_or_assign("priority", ts);
        return;
    }
    if (!value.is_string() && !value.is_null()) return;
    std::optional<std::string> str;
    if (value.is_string()) str = value.get<std::string>();
    if (key == "parent" && str) str = bare_id(*str);
    // Empty body means no body: the fold must agree with the snapshot round-trip.
    if (key == "body" && str && str->empty()) str = std::nullopt;
    if (key == "title") b.title = str;
    else if (key == "type") b.type = str;
    else if (key == "status") b.status = str;
    else if (key == "assignee") b.assignee = str;
    else if (key == "parent") b.parent = str;
    else if (key == "body") b.body = str;
    b.fieldts.insert_or_assign(key, ts);
}

/// Apply a `create`/`set` payload field-by-field (field-level LWW by order).
void apply_set(Builder& b, const Json& payload, const std::string& ts, const std::optional<std::string>& reason) {
    for (const auto& [key, value] : payload.items()) {
        if (key.ends_with('+')) {
            const std::string name = key.substr(0, key.size() - 1);
            if (is_set_field(name)) {
                auto& target = name == "labels" ? b.labels : b.blocked_by;
                for (auto& v : set_values(name, value)) target.insert(std::move(v));
                b.fieldts.insert_or_assign(name, ts);
            }
        } else if (key.ends_with('-')) {
            const std::string name = key.substr(0, key.size() - 1);
            if (is_set_field(name)) {
                auto& target = name == "labels" ? b.labels : b.blocked_by;
                for (const auto& v : set_values(name, value)) target.erase(v);
                b.fieldts.insert_or_assign(name, ts);
            }
        } else if (is_scalar_field(key)) {
            apply_scalar(b, key, value, ts);
            if (key == "status" && value.is_string() && is_terminal(value.get<std::string>())) {
                b.last_close_reason = reason;
            }
        }
        // Unknown keys are ignored: a future field should not crash older code.
    }
}

void apply_event(Builder& b, const Event& ev) {
    if ((ev.op == "create" || ev.op == "set") && ev.set && !ev.set->empty()) {
        apply_set(b, *ev.set, ev.ts, ev.reason);
    } else if (ev.op == "comment" && ev.text) {
        b.comments.push_back(Comment{ev.ts, ev.actor, *ev.text});
    } else if (ev.op == "link" && ev.remote && ev.ref) {
        b.remotes.insert_or_assign(*ev.remote, *ev.ref);
        b.fieldts.insert_or_assign("remotes", ev.ts);
    } else if (ev.op == "unlink" && ev.remote) {
        b.remotes.erase(*ev.remote);
        b.fieldts.insert_or_assign("remotes", ev.ts);
    }
    // "synced" carries no per-ticket field state.
}

void write_snapshot(const std::filesystem::path& snap, const TicketMap& tickets) {
    // Best-effort: the snapshot is disposable, failure to cache is non-fatal.
    try {
        std::string payload;
        for (const auto& [id, ticket] : tickets) {
            payload += json::dumps(ticket_to_mapping(ticket), json::kPyDefault);
            payload.push_back('\n');
        }
        std::filesystem::path tmp = snap;
        tmp.replace_extension(".jsonl.tmp");
        io::write_file_atomic(snap, tmp, payload);
    } catch (const std::exception&) {
        // ignore
    }
}

std::vector<std::string> as_str_list(const Json& value) {
    std::vector<std::string> out;
    if (!value.is_array()) return out;
    for (const auto& item : value) out.push_back(json::py_str(item));
    return out;
}

OrderedMap<std::string, std::string> as_str_dict(const Json& value) {
    OrderedMap<std::string, std::string> out;
    if (!value.is_object()) return out;
    for (const auto& [k, v] : value.items()) out.insert_or_assign(k, json::py_str(v));
    return out;
}

std::vector<Comment> as_comments(const Json& value) {
    std::vector<Comment> out;
    if (!value.is_array()) return out;
    for (const auto& item : value) {
        if (item.is_object() && item.contains("ts") && item.contains("actor") && item.contains("text")) {
            out.push_back(Comment{json::py_str(item["ts"]), json::py_str(item["actor"]), json::py_str(item["text"])});
        }
    }
    return out;
}

/// Python truthiness for the values a snapshot can hold.
bool truthy(const Json& value) {
    if (value.is_null()) return false;
    if (value.is_boolean()) return value.get<bool>();
    if (value.is_string()) return !value.get_ref<const std::string&>().empty();
    if (value.is_number_float()) return value.get<double>() != 0.0;
    if (value.is_number()) return value != 0;
    return !value.empty();
}

}  // namespace

bool is_status(std::string_view v) { return contains(kStatuses, v); }
bool is_terminal(std::string_view v) { return contains(kTerminal, v); }
bool is_type(std::string_view v) { return contains(kTypes, v); }
bool is_scalar_field(std::string_view v) { return contains(kScalarFields, v); }
bool is_set_field(std::string_view v) { return contains(kSetFields, v); }

std::string bare_id(std::string_view value) {
    try {
        return ids::normalize_id(value);
    } catch (const StoreError&) {
        // Mirrors the reference: only StoreError is swallowed, and normalize_id
        // raises IdError, so an invalid id propagates (and `rp doctor` reports it).
        return std::string(value);
    }
}

std::vector<Event> dedup_sort(std::vector<Event> events) {
    std::unordered_set<std::string> seen;
    std::vector<Event> unique;
    unique.reserve(events.size());
    for (auto& ev : events) {
        if (seen.insert(ev.id).second) unique.push_back(std::move(ev));
    }
    std::stable_sort(unique.begin(), unique.end(), [](const Event& a, const Event& b) {
        return a.ts != b.ts ? a.ts < b.ts : a.id < b.id;
    });
    return unique;
}

TicketMap fold(const std::vector<Event>& events) {
    OrderedMap<std::string, Builder> builders;
    for (const auto& ev : dedup_sort(events)) {
        if (ev.op == "synced") continue;
        const std::string tid = bare_id(ev.ticket);
        Builder* b = builders.find(tid);
        if (b == nullptr) {
            b = &builders.insert_or_assign(tid, Builder{});
            b->id = tid;
        }
        if (!b->created) b->created = ev.ts;
        b->updated = ev.ts;
        apply_event(*b, ev);
    }
    TicketMap out;
    for (const auto& [tid, b] : builders) out.insert_or_assign(tid, b.freeze());
    return out;
}

std::string derive_status(const Ticket& ticket, const TicketMap& by_id) {
    if (ticket.type != "epic") return ticket.status;
    bool any_child = false;
    bool all_done = true;
    for (const auto& [id, c] : by_id) {
        if (c.parent && *c.parent == ticket.id) {
            any_child = true;
            if (c.status != "done") all_done = false;
        }
    }
    if (!any_child) return ticket.status;
    return all_done ? "done" : "open";
}

bool is_ready(const Ticket& ticket, const TicketMap& by_id) {
    if (ticket.type == "epic") return false;
    if (ticket.status != "open") return false;
    for (const auto& dep : ticket.blocked_by) {
        const Ticket* blocker = by_id.find(dep);
        if (blocker == nullptr || blocker->status != "done") return false;
    }
    return true;
}

std::optional<std::vector<std::string>> find_cycle(const TicketMap& by_id) {
    enum Color { White, Gray, Black };
    OrderedMap<std::string, Color> color;
    for (const auto& [id, t] : by_id) color.insert_or_assign(id, White);
    std::vector<std::string> stack;

    std::function<std::optional<std::vector<std::string>>(const std::string&)> dfs =
        [&](const std::string& node) -> std::optional<std::vector<std::string>> {
        color.insert_or_assign(node, Gray);
        stack.push_back(node);
        for (const auto& dep : by_id.find(node)->blocked_by) {
            const Color* c = color.find(dep);
            if (c == nullptr) continue;
            if (*c == Gray) {
                const auto it = std::find(stack.begin(), stack.end(), dep);
                std::vector<std::string> cycle(it, stack.end());
                cycle.push_back(dep);
                return cycle;
            }
            if (*c == White) {
                if (auto found = dfs(dep)) return found;
            }
        }
        stack.pop_back();
        color.insert_or_assign(node, Black);
        return std::nullopt;
    };

    for (const auto& [tid, t] : by_id) {
        if (*color.find(tid) == White) {
            if (auto cycle = dfs(tid)) return cycle;
        }
    }
    return std::nullopt;
}

TicketMap fold_all(const std::filesystem::path& rohrpost_dir) {
    return fold(store::read_events(rohrpost_dir));
}

TicketMap load_tickets(const std::filesystem::path& rohrpost_dir) {
    const auto log = paths::log_path(rohrpost_dir);
    const auto snap = paths::snapshot_path(rohrpost_dir);
    const std::int64_t log_mtime = io::mtime_ns(log).value_or(0);
    // Strictly newer: a snapshot written in the same tick as the last append is
    // treated as stale and re-folded (correctness over speed).
    if (const auto snap_mtime = io::mtime_ns(snap); snap_mtime && *snap_mtime > log_mtime) {
        if (auto cached = read_snapshot(snap)) return std::move(*cached);
    }
    TicketMap tickets = fold_all(rohrpost_dir);
    write_snapshot(snap, tickets);
    return tickets;
}

Json ticket_to_mapping(const Ticket& ticket, const MappingOptions& options) {
    const auto rnd = [&](const std::optional<std::string>& tid) -> Json {
        if (!tid) return Json();
        if (options.prefix && !options.prefix->empty()) return Json(*options.prefix + "-" + *tid);
        return Json(*tid);
    };
    Json m = Json::object();
    m["id"] = rnd(ticket.id);
    m["title"] = ticket.title;
    m["type"] = ticket.type;
    m["status"] = ticket.status;
    m["priority"] = ticket.priority;
    m["parent"] = rnd(ticket.parent);
    Json blocked = Json::array();
    for (const auto& b : ticket.blocked_by) blocked.push_back(rnd(b));
    m["blocked_by"] = std::move(blocked);
    m["labels"] = ticket.labels;
    m["assignee"] = ticket.assignee ? Json(*ticket.assignee) : Json();
    if (options.include_body) m["body"] = ticket.body ? Json(*ticket.body) : Json();
    Json remotes = Json::object();
    for (const auto& [k, v] : ticket.remotes) remotes[k] = v;
    m["remotes"] = std::move(remotes);
    m["last_close_reason"] = ticket.last_close_reason ? Json(*ticket.last_close_reason) : Json();
    m["created"] = ticket.created;
    m["updated"] = ticket.updated;
    if (options.include_comments) {
        Json comments = Json::array();
        for (const auto& c : ticket.comments) comments.push_back(comment_to_mapping(c));
        m["comments"] = std::move(comments);
    }
    if (options.include_fieldts) {
        Json fieldts = Json::object();
        for (const auto& [k, v] : ticket.fieldts) fieldts[k] = v;
        m["_fieldts"] = std::move(fieldts);
    }
    return m;
}

Json comment_to_mapping(const Comment& comment) {
    Json m = Json::object();
    m["ts"] = comment.ts;
    m["actor"] = comment.actor;
    m["text"] = comment.text;
    return m;
}

Ticket mapping_to_ticket(const Json& obj) {
    const auto get = [&](const char* key) -> Json {
        const auto it = obj.find(key);
        return it == obj.end() ? Json() : *it;
    };
    Ticket t;
    t.id = json::py_str(obj.at("id"));
    t.title = obj.contains("title") ? json::py_str(obj["title"]) : "";
    t.type = obj.contains("type") ? json::py_str(obj["type"]) : std::string(kDefaultType);
    t.status = obj.contains("status") ? json::py_str(obj["status"]) : std::string(kDefaultStatus);
    const Json raw_priority = obj.contains("priority") ? obj["priority"] : Json(kDefaultPriority);
    if (raw_priority.is_number_integer()) t.priority = raw_priority.get<std::int64_t>();
    else if (raw_priority.is_number_unsigned()) t.priority = static_cast<std::int64_t>(raw_priority.get<std::uint64_t>());
    else if (raw_priority.is_number_float()) t.priority = static_cast<std::int64_t>(std::trunc(raw_priority.get<double>()));
    else t.priority = kDefaultPriority;
    const Json parent = get("parent");
    if (truthy(parent)) t.parent = json::py_str(parent);
    t.blocked_by = as_str_list(get("blocked_by"));
    t.labels = as_str_list(get("labels"));
    if (truthy(get("assignee"))) t.assignee = json::py_str(obj["assignee"]);
    if (truthy(get("body"))) t.body = json::py_str(obj["body"]);
    t.remotes = as_str_dict(get("remotes"));
    if (truthy(get("last_close_reason"))) t.last_close_reason = json::py_str(obj["last_close_reason"]);
    t.comments = as_comments(get("comments"));
    t.created = obj.contains("created") ? json::py_str(obj["created"]) : "";
    t.updated = obj.contains("updated") ? json::py_str(obj["updated"]) : "";
    t.fieldts = as_str_dict(get("_fieldts"));
    return t;
}

std::optional<TicketMap> read_snapshot(const std::filesystem::path& snap) {
    // Any read/parse/schema failure means "treat as stale and re-fold".
    auto content = io::read_file(snap);
    if (!content) return std::nullopt;
    TicketMap result;
    for (const auto raw : py::split_lines(*content)) {
        const std::string_view stripped = py::strip(raw);
        if (stripped.empty()) continue;
        auto parsed = json::parse(stripped);
        if (!parsed || !parsed->is_object() || !parsed->contains("id")) return std::nullopt;
        try {
            Ticket t = mapping_to_ticket(*parsed);
            const std::string id = json::py_str((*parsed)["id"]);
            result.insert_or_assign(id, std::move(t));
        } catch (const std::exception&) {
            return std::nullopt;
        }
    }
    return result;
}

}  // namespace rp
