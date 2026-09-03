#include "rohrpost/api.hpp"

#include "rohrpost/errors.hpp"
#include "rohrpost/ids.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/paths.hpp"
#include "rohrpost/pyfmt.hpp"
#include "rohrpost/store.hpp"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <format>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace rp::api {
namespace {

namespace fs = std::filesystem;

std::string sorted_repr(const auto& values) {
    std::vector<std::string> sorted(values.begin(), values.end());
    std::sort(sorted.begin(), sorted.end());
    Json arr = Json::array();
    for (const auto& v : sorted) arr.push_back(v);
    return json::py_repr(arr);
}

Assignment parse_set_op(std::string_view key, std::string_view raw_value, std::string_view token) {
    const std::string field(key.substr(0, key.size() - 1));
    const auto op = key.ends_with('+') ? Assignment::Op::Add : Assignment::Op::Remove;
    if (!is_set_field(field)) throw TicketError(std::format("{} is not a set field (cannot use +/-)", py::repr(field)));
    Json values = Json::array();
    std::size_t start = 0;
    while (start <= raw_value.size()) {
        const auto end = raw_value.find(',', start);
        const std::string_view piece = raw_value.substr(start, end == std::string_view::npos ? std::string_view::npos : end - start);
        const std::string_view trimmed = py::strip(piece);
        if (!trimmed.empty()) values.push_back(std::string(trimmed));
        if (end == std::string_view::npos) break;
        start = end + 1;
    }
    if (values.empty()) throw TicketError(std::format("empty value list in assignment {}", py::repr(token)));
    if (field == "blocked_by") {
        for (auto& v : values) v = normalise_structural(v.get<std::string>());
    }
    return Assignment{op, field, std::move(values)};
}

Assignment parse_scalar_assignment(std::string_view key, std::string_view raw_value) {
    if (!is_scalar_field(key)) throw TicketError(std::format("unknown field {}", py::repr(key)));
    if (key == "priority") {
        const auto parsed = py::parse_int(raw_value);
        if (!parsed) throw TicketError(std::format("priority must be an integer, got {}", py::repr(raw_value)));
        return Assignment{Assignment::Op::Set, "priority", Json(*parsed)};
    }
    if (key == "parent") return Assignment{Assignment::Op::Set, "parent", Json(normalise_structural(raw_value))};
    return Assignment{Assignment::Op::Set, std::string(key), Json(std::string(raw_value))};
}

// --- templates ---------------------------------------------------------------

fs::path template_path(const fs::path& rohrpost_dir, std::string_view name) {
    const std::string_view requested = py::strip(name);
    if (requested.empty()) throw TicketError("template name must be non-empty");
    std::string filename(requested);
    if (!filename.ends_with(".toml")) filename += ".toml";
    if (io::path_from_utf8(filename).filename() != io::path_from_utf8(filename) ||
        filename.find('/') != std::string::npos || filename.find('\\') != std::string::npos) {
        throw TicketError("template name must be a simple filename");
    }
    std::error_code ec;
    const fs::path root = fs::weakly_canonical(paths::templates_dir(rohrpost_dir), ec);
    const fs::path path = fs::weakly_canonical(root / io::path_from_utf8(filename), ec);
    const std::string root_s = io::path_str(root);
    const std::string path_s = io::path_str(path);
    if (!(path_s == root_s || path_s.starts_with(root_s + static_cast<char>(fs::path::preferred_separator)))) {
        throw TicketError("template path must stay under .rohrpost/templates");
    }
    if (!fs::is_regular_file(path, ec)) throw TicketError(std::format("no such template: {}", name));
    return path;
}

Json read_template(const fs::path& path, std::string_view name) {
    auto raw = io::read_file(path);
    if (!raw) throw TicketError(std::format("cannot read template {}: {}", py::repr(name), raw.error()));
    auto parsed = toml_compat::parse(*raw);
    if (!parsed) throw TicketError(std::format("invalid template {}: {}", py::repr(name), parsed.error()));
    return std::move(*parsed);
}

Json template_values(const Json& raw) {
    static constexpr std::array<std::string_view, 3> section_names = {"defaults", "fields", "ticket"};
    Json values = Json::object();
    for (const auto& [key, value] : raw.items()) {
        if (std::find(section_names.begin(), section_names.end(), key) == section_names.end()) values[key] = value;
    }
    for (const auto section_name : section_names) {
        const auto it = raw.find(section_name);
        if (it == raw.end() || it->is_null()) continue;
        if (!it->is_object()) throw TicketError(std::format("template section [{}] must be a table", section_name));
        for (const auto& [key, value] : it->items()) values[key] = value;
    }
    std::vector<std::string> unknown;
    for (const auto& [key, value] : values.items()) {
        if (!is_scalar_field(key) && !is_set_field(key)) unknown.push_back(key);
    }
    std::sort(unknown.begin(), unknown.end());
    if (!unknown.empty()) {
        std::string joined;
        for (std::size_t i = 0; i < unknown.size(); ++i) {
            if (i) joined += ", ";
            joined += unknown[i];
        }
        throw TicketError(std::format("unknown template field(s): {}", joined));
    }
    return values;
}

std::vector<std::string> template_strings(std::string_view field, const Json& value) {
    std::vector<std::string> cleaned;
    const auto check = [&](const Json& item) {
        if (!item.is_string() || py::strip(item.get_ref<const std::string&>()).empty()) {
            throw TicketError(std::format("template {} must contain non-empty strings", field));
        }
        cleaned.emplace_back(py::strip(item.get_ref<const std::string&>()));
    };
    if (value.is_array()) {
        for (const auto& item : value) check(item);
    } else {
        check(value);
    }
    return cleaned;
}

Json normalise_template_values(Json values) {
    if (values.contains("priority")) {
        const Json& p = values["priority"];
        if (p.is_boolean() || !p.is_number_integer()) throw TicketError("template priority must be an integer");
    }
    for (const auto field : {"labels", "blocked_by"}) {
        if (!values.contains(field)) continue;
        auto cleaned = template_strings(field, values[field]);
        if (std::string_view(field) == "blocked_by") {
            for (auto& item : cleaned) item = normalise_structural(item);
        }
        values[field] = cleaned;
    }
    for (const auto field : {"title", "type", "status", "assignee", "body"}) {
        if (values.contains(field) && !values[field].is_string()) throw TicketError(std::format("template {} must be a string", field));
    }
    if (values.contains("parent")) {
        if (!values["parent"].is_string()) throw TicketError("template parent must be a ticket id");
        values["parent"] = normalise_structural(values["parent"].get<std::string>());
    }
    return values;
}

// --- mutation helpers --------------------------------------------------------

Ticket resolve(const TicketMap& by_id, std::string_view ticket_ref) {
    const std::string tid = normalise_structural(ticket_ref);
    const Ticket* t = by_id.find(tid);
    if (t == nullptr) throw TicketNotFoundError(std::format("no such ticket: {}", ticket_ref));
    return *t;
}

struct EventFields {
    std::optional<Json> set;
    std::optional<std::string> text;
    std::optional<std::string> remote;
    std::optional<std::string> ref;
    std::optional<std::string> reason;
};

Event build_event(const std::string& ticket, std::string op, const std::string& actor, const Sources& s, EventFields f) {
    Event e;
    e.id = s.ulid();
    e.ts = s.now();
    e.ticket = ticket;
    e.op = std::move(op);
    e.actor = actor;
    e.set = std::move(f.set);
    e.text = std::move(f.text);
    e.remote = std::move(f.remote);
    e.ref = std::move(f.ref);
    e.reason = std::move(f.reason);
    return e;
}

Ticket require_after(const fs::path& dir, const std::string& tid) {
    TicketMap by_id = load_tickets(dir);
    const Ticket* t = by_id.find(tid);
    if (t == nullptr) throw StoreError(std::format("ticket {} did not appear after its event was appended", tid));
    return *t;
}

std::string new_id(const fs::path& dir) {
    const TicketMap existing = load_tickets(dir);
    for (int i = 0; i < 8; ++i) {
        std::string candidate = ids::new_ticket_id();
        if (!existing.contains(candidate)) return candidate;
    }
    throw StoreError("could not allocate a non-colliding ticket id after 8 tries");
}

void validate_new_ticket(std::string_view title, std::string_view type, std::int64_t priority) {
    if (py::strip(title).empty()) throw TicketError("title must be non-empty");
    if (!is_type(type)) throw TicketError(std::format("type must be one of {}, got {}", sorted_repr(kTypes), py::repr(type)));
    if (priority < 0 || priority > 4) throw TicketError(std::format("priority must be 0..4, got {}", priority));
}

Json current_scalar(const Ticket& t, const std::string& field) {
    if (field == "priority") return Json(t.priority);
    if (field == "title") return Json(t.title);
    if (field == "type") return Json(t.type);
    if (field == "status") return Json(t.status);
    if (field == "assignee") return t.assignee ? Json(*t.assignee) : Json();
    if (field == "parent") return t.parent ? Json(*t.parent) : Json();
    if (field == "body") return t.body ? Json(*t.body) : Json();
    return Json();
}

std::optional<Assignment> effective_one(const Ticket& t, const Assignment& a) {
    if (a.op == Assignment::Op::Set) {
        return json::py_equal(current_scalar(t, a.field), a.value) ? std::nullopt : std::optional<Assignment>(a);
    }
    const auto& source = a.field == "labels" ? t.labels : t.blocked_by;
    const std::set<std::string> current(source.begin(), source.end());
    Json changed = Json::array();
    const auto values = a.value.is_array() ? a.value : Json::array({a.value});
    for (const auto& v : values) {
        const std::string s = json::py_str(v);
        const bool present = current.contains(s);
        if (a.op == Assignment::Op::Add ? !present : present) changed.push_back(s);
    }
    if (changed.empty()) return std::nullopt;
    return Assignment{a.op, a.field, std::move(changed)};
}

std::vector<Assignment> effective_assignments(const Ticket& t, const std::vector<Assignment>& assignments) {
    std::vector<Assignment> out;
    for (const auto& a : assignments) {
        if (auto e = effective_one(t, a)) out.push_back(std::move(*e));
    }
    return out;
}

void validate_set_assignments(const std::vector<Assignment>& assignments) {
    for (const auto& a : assignments) {
        if (a.field == "status" && !(a.value.is_string() && is_status(a.value.get<std::string>()))) {
            throw TicketError(std::format("status must be one of {}, got {}", sorted_repr(kStatuses), json::py_repr(a.value)));
        }
        if (a.field == "type" && !(a.value.is_string() && is_type(a.value.get<std::string>()))) {
            throw TicketError(std::format("type must be one of {}, got {}", sorted_repr(kTypes), json::py_repr(a.value)));
        }
        if (a.field == "priority") {
            const bool ok = a.value.is_number_integer() && a.value.get<std::int64_t>() >= 0 && a.value.get<std::int64_t>() <= 4;
            if (!ok) throw TicketError(std::format("priority must be 0..4, got {}", json::py_repr(a.value)));
        }
    }
}

Json assignments_to_payload(const std::vector<Assignment>& assignments) {
    Json payload = Json::object();
    for (const auto& a : assignments) {
        if (a.op == Assignment::Op::Set) payload[a.field] = a.value;
        else if (a.op == Assignment::Op::Add) payload[a.field + "+"] = a.value;
        else payload[a.field + "-"] = a.value;
    }
    return payload;
}

WriteResult terminate(const fs::path& dir, std::string_view ticket_ref, const std::string& status,
                      const std::optional<std::string>& reason, const std::string& actor, const Sources& s) {
    const TicketMap by_id = load_tickets(dir);
    Ticket ticket = resolve(by_id, ticket_ref);
    if (ticket.status == status) return WriteResult{std::move(ticket), false};
    std::optional<std::string> clean;
    if (reason && !py::strip(*reason).empty()) clean = std::string(py::strip(*reason));
    Json payload = Json::object();
    payload["status"] = status;
    store::append_event(dir, build_event(ticket.id, "set", actor, s, EventFields{.set = payload, .reason = clean}));
    return WriteResult{require_after(dir, ticket.id), true};
}

bool by_priority_then_created(const Ticket& a, const Ticket& b) {
    if (a.priority != b.priority) return a.priority < b.priority;
    return a.created < b.created;
}

}  // namespace

std::string normalise_structural(std::string_view value) {
    try {
        return ids::normalize_id(value);
    } catch (const StoreError& exc) {
        throw TicketError(exc.what());
    }
}

Assignment parse_assignment(std::string_view token) {
    const auto eq = token.find('=');
    if (eq == std::string_view::npos) throw TicketError(std::format("expected field=value, got {}", py::repr(token)));
    const std::string_view key = py::strip(token.substr(0, eq));
    const std::string_view raw_value = token.substr(eq + 1);
    if (key.empty()) throw TicketError(std::format("empty field in assignment {}", py::repr(token)));
    if (key.ends_with('+') || key.ends_with('-')) return parse_set_op(key, raw_value, token);
    return parse_scalar_assignment(key, raw_value);
}

Json load_template(const fs::path& rohrpost_dir, std::string_view name) {
    const fs::path path = template_path(rohrpost_dir, name);
    const Json raw = read_template(path, name);
    return normalise_template_values(template_values(raw));
}

std::string propose_prefix(const fs::path& directory) {
    std::string candidate;
    for (const char c : io::path_str(directory.filename())) {
        if (c >= 'a' && c <= 'z') candidate.push_back(static_cast<char>(c - 'a' + 'A'));
        else if (c >= 'A' && c <= 'Z') candidate.push_back(c);
        if (candidate.size() == 5) break;
    }
    if (candidate.size() < 2) return "RP";
    return candidate;
}

InitResult init_repo(std::optional<fs::path> target_dir, std::optional<std::string> prefix) {
    std::error_code ec;
    const fs::path base = target_dir ? fs::weakly_canonical(*target_dir, ec) : paths::resolved_cwd();
    const auto git_root = paths::find_git_root(base);
    const fs::path repo_root = git_root ? *git_root : base;
    const fs::path rohrpost_dir = repo_root / paths::kRohrpostDirName;

    paths::ensure_layout(rohrpost_dir);

    const fs::path cfg_path = paths::config_path(rohrpost_dir);
    bool created_config = false;
    Config config;
    if (fs::is_regular_file(cfg_path, ec)) {
        config = load_config(rohrpost_dir);
    } else {
        const std::string chosen = (prefix && !prefix->empty()) ? validate_prefix(*prefix) : propose_prefix(repo_root);
        io::write_file(cfg_path, render_config_toml(chosen));
        created_config = true;
        config = load_config(rohrpost_dir);
    }
    const bool updated_gitattributes = paths::write_gitattributes(repo_root);
    const bool updated_gitignore = paths::write_gitignore(repo_root);
    return InitResult{rohrpost_dir, config.prefix, created_config, updated_gitattributes, updated_gitignore};
}

WriteResult create_ticket(const fs::path& dir, std::string_view title, const CreateOptions& o,
                          const std::string& actor, const Sources& s) {
    validate_new_ticket(title, o.type, o.priority);
    Json payload = Json::object();
    payload["title"] = std::string(py::strip(title));
    payload["type"] = o.type;
    payload["status"] = "open";
    payload["priority"] = o.priority;
    if (o.parent) payload["parent"] = normalise_structural(*o.parent);
    if (!o.labels.empty()) {
        std::set<std::string> labels(o.labels.begin(), o.labels.end());
        payload["labels+"] = std::vector<std::string>(labels.begin(), labels.end());
    }
    if (!o.blocked_by.empty()) {
        std::set<std::string> deps;
        for (const auto& b : o.blocked_by) deps.insert(normalise_structural(b));
        payload["blocked_by+"] = std::vector<std::string>(deps.begin(), deps.end());
    }
    if (o.assignee && !o.assignee->empty()) payload["assignee"] = *o.assignee;
    if (o.body && !py::strip(*o.body).empty()) payload["body"] = *o.body;
    const std::string tid = new_id(dir);
    store::append_event(dir, build_event(tid, "create", actor, s, EventFields{.set = payload}));
    return WriteResult{require_after(dir, tid), true};
}

WriteResult set_fields(const fs::path& dir, std::string_view ticket_ref, const std::vector<Assignment>& assignments,
                       const std::string& actor, const Sources& s) {
    const TicketMap by_id = load_tickets(dir);
    Ticket ticket = resolve(by_id, ticket_ref);
    const auto effective = effective_assignments(ticket, assignments);
    if (effective.empty()) return WriteResult{std::move(ticket), false};
    validate_set_assignments(effective);
    Json payload = assignments_to_payload(effective);
    store::append_event(dir, build_event(ticket.id, "set", actor, s, EventFields{.set = payload}));
    return WriteResult{require_after(dir, ticket.id), true};
}

WriteResult claim(const fs::path& dir, std::string_view ticket_ref, const std::string& actor, const Sources& s) {
    const std::vector<Assignment> assignments = {
        Assignment{Assignment::Op::Set, "status", Json("in_progress")},
        Assignment{Assignment::Op::Set, "assignee", Json(actor)},
    };
    return set_fields(dir, ticket_ref, assignments, actor, s);
}

WriteResult close(const fs::path& dir, std::string_view ticket_ref, std::optional<std::string> reason,
                  const std::string& actor, const Sources& s) {
    return terminate(dir, ticket_ref, "done", reason, actor, s);
}

WriteResult drop(const fs::path& dir, std::string_view ticket_ref, std::optional<std::string> reason,
                 const std::string& actor, const Sources& s) {
    return terminate(dir, ticket_ref, "dropped", reason, actor, s);
}

WriteResult add_comment(const fs::path& dir, std::string_view ticket_ref, std::string_view text,
                        const std::string& actor, const Sources& s) {
    if (py::strip(text).empty()) throw TicketError("comment text must be non-empty");
    const TicketMap by_id = load_tickets(dir);
    const Ticket ticket = resolve(by_id, ticket_ref);
    store::append_event(dir, build_event(ticket.id, "comment", actor, s, EventFields{.text = std::string(py::strip(text))}));
    return WriteResult{require_after(dir, ticket.id), true};
}

WriteResult link_remote(const fs::path& dir, std::string_view ticket_ref, std::string_view remote, std::string_view ref,
                        const std::string& actor, const Sources& s) {
    if (py::strip(remote).empty() || py::strip(ref).empty()) throw TicketError("remote and ref must be non-empty");
    const TicketMap by_id = load_tickets(dir);
    Ticket ticket = resolve(by_id, ticket_ref);
    const std::string remote_s(py::strip(remote));
    const std::string ref_s(py::strip(ref));
    if (const auto* current = ticket.remotes.find(remote_s); current != nullptr && *current == ref_s) {
        return WriteResult{std::move(ticket), false};
    }
    store::append_event(dir, build_event(ticket.id, "link", actor, s, EventFields{.remote = remote_s, .ref = ref_s}));
    return WriteResult{require_after(dir, ticket.id), true};
}

WriteResult unlink_remote(const fs::path& dir, std::string_view ticket_ref, std::string_view remote,
                          const std::string& actor, const Sources& s) {
    const std::string remote_s(py::strip(remote));
    if (remote_s.empty()) throw TicketError("remote must be non-empty");
    const TicketMap by_id = load_tickets(dir);
    Ticket ticket = resolve(by_id, ticket_ref);
    if (!ticket.remotes.contains(remote_s)) return WriteResult{std::move(ticket), false};
    store::append_event(dir, build_event(ticket.id, "unlink", actor, s, EventFields{.remote = remote_s}));
    return WriteResult{require_after(dir, ticket.id), true};
}

Ticket show_ticket(const fs::path& dir, std::string_view ticket_ref) {
    const TicketMap by_id = load_tickets(dir);
    return resolve(by_id, ticket_ref);
}

std::vector<Ticket> list_tickets(const fs::path& dir, const ListFilter& f) {
    const TicketMap by_id = load_tickets(dir);
    const std::optional<std::string> parent_bare = (f.parent && !f.parent->empty()) ? std::optional(normalise_structural(*f.parent)) : std::nullopt;
    const std::optional<std::string> needle = f.match ? std::optional(py::casefold(*f.match)) : std::nullopt;
    std::vector<Ticket> out;
    for (const auto& [id, t] : by_id) {
        if (f.status && derive_status(t, by_id) != *f.status) continue;
        if (f.label && std::find(t.labels.begin(), t.labels.end(), *f.label) == t.labels.end()) continue;
        if (parent_bare && !(t.parent && *t.parent == *parent_bare)) continue;
        if (f.type && t.type != *f.type) continue;
        if (needle && py::casefold(t.title).find(*needle) == std::string::npos) continue;
        out.push_back(t);
    }
    if (f.ready) {
        std::vector<Ticket> ready;
        for (auto& t : out) {
            if (is_ready(t, by_id)) ready.push_back(std::move(t));
        }
        out = std::move(ready);
    }
    std::stable_sort(out.begin(), out.end(), by_priority_then_created);
    return out;
}

std::vector<Ticket> ready_tickets(const fs::path& dir, std::optional<std::int64_t> limit) {
    std::vector<Ticket> tickets = list_tickets(dir, ListFilter{.ready = true});
    if (limit && *limit >= 0 && static_cast<std::size_t>(*limit) < tickets.size()) {
        tickets.resize(static_cast<std::size_t>(*limit));
    }
    return tickets;
}

std::vector<Ticket> list_conflicts(const fs::path& dir) {
    const TicketMap by_id = load_tickets(dir);
    std::vector<Ticket> out;
    for (const auto& [id, t] : by_id) {
        if (std::any_of(t.labels.begin(), t.labels.end(), [](const std::string& l) { return l.starts_with("conflict:"); })) {
            out.push_back(t);
        }
    }
    std::stable_sort(out.begin(), out.end(), by_priority_then_created);
    return out;
}

WriteResult resolve_conflict(const fs::path& dir, std::string_view ticket_ref, std::string_view take,
                             const std::string& actor, const Sources& s) {
    if (take != "local" && take != "remote") throw TicketError("resolve --take must be 'local' or 'remote'");
    const TicketMap by_id = load_tickets(dir);
    Ticket ticket = resolve(by_id, ticket_ref);
    Json conflict_labels = Json::array();
    for (const auto& l : ticket.labels) {
        if (l.starts_with("conflict:")) conflict_labels.push_back(l);
    }
    if (conflict_labels.empty()) return WriteResult{std::move(ticket), false};

    const WriteResult result = set_fields(dir, ticket.id, {Assignment{Assignment::Op::Remove, "labels", conflict_labels}}, actor, s);
    (void)add_comment(dir, ticket.id, std::format("sync conflict resolved taking {}", take), actor, s);
    // Reopen unless the operator has since moved it to a terminal state.
    const WriteResult reopened = set_fields(dir, ticket.id, {Assignment{Assignment::Op::Set, "status", Json("open")}}, actor, s);
    Ticket final_ticket = reopened.ticket.status == "open" ? reopened.ticket : result.ticket;
    return WriteResult{std::move(final_ticket), true};
}

Tree tree(const fs::path& dir, std::string_view ticket_ref) {
    const TicketMap by_id = load_tickets(dir);
    Ticket root = resolve(by_id, ticket_ref);
    std::vector<Ticket> children;
    for (const auto& [id, c] : by_id) {
        if (c.parent && *c.parent == root.id) children.push_back(c);
    }
    std::stable_sort(children.begin(), children.end(), by_priority_then_created);
    return Tree{std::move(root), std::move(children)};
}

std::vector<Event> event_log(const fs::path& dir, std::optional<std::string> ticket_ref) {
    std::vector<Event> events = store::read_events(dir);
    if (ticket_ref) {
        const std::string tid = normalise_structural(*ticket_ref);
        std::vector<Event> filtered;
        for (auto& e : events) {
            if (e.op != "synced" && normalise_structural(e.ticket) == tid) filtered.push_back(std::move(e));
        }
        events = std::move(filtered);
    }
    std::stable_sort(events.begin(), events.end(), [](const Event& a, const Event& b) {
        return a.ts != b.ts ? a.ts < b.ts : a.id < b.id;
    });
    return events;
}

std::vector<Comment> comments(const fs::path& dir, std::string_view ticket_ref) {
    return show_ticket(dir, ticket_ref).comments;
}

Config load_repo_config(const fs::path& dir) {
    try {
        return load_config(dir);
    } catch (const StoreError&) {
        return default_config();
    }
}

}  // namespace rp::api
