#include "rohrpost/cli.hpp"

#include "rohrpost/api.hpp"
#include "rohrpost/argparse.hpp"
#include "rohrpost/compact.hpp"
#include "rohrpost/doctor.hpp"
#include "rohrpost/errors.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/paths.hpp"
#include "rohrpost/providers/github.hpp"
#include "rohrpost/pyfmt.hpp"
#include "rohrpost/stats.hpp"
#include "rohrpost/sync.hpp"
#include "rohrpost/util.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <format>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace rp::cli {
namespace {

namespace fs = std::filesystem;
using argparse::Action;
using argparse::Argument;
using argparse::NArgs;
using argparse::Namespace;
using argparse::Parser;
using argparse::Type;

// ---------------------------------------------------------------------------
// Output helpers (NO_COLOR-aware).
// ---------------------------------------------------------------------------
bool use_color() {
    if (const auto v = io::getenv("NO_COLOR"); v && !v->empty()) return false;
    if (const auto v = io::getenv("CLICOLOR"); v && *v == "0") return false;
    return io::stdout_is_tty();
}

std::string style(const std::string& text, const char* code, bool enabled) {
    return enabled ? std::format("\033[{}m{}\033[0m", code, text) : text;
}

const char* status_color(const std::string& status) {
    static const std::map<std::string, const char*> colors = {
        {"done", "32"}, {"dropped", "90"}, {"in_progress", "36"}, {"review", "35"},
        {"waiting", "33"}, {"ready", "32"}, {"open", "0"},
    };
    const auto it = colors.find(status);
    return it == colors.end() ? "0" : it->second;
}

std::string color_status(const std::string& status, bool enabled) {
    return style(status, status_color(status), enabled);
}

/// Bundle of resolved output flags so handlers stay terse.
struct Out {
    bool json;
    bool color;
    std::string prefix;

    void emit_json(const Json& obj) const { io::println(json::dumps(obj, json::kPretty)); }
    [[nodiscard]] std::string rend(const std::string& bare_id) const { return prefix + "-" + bare_id; }
};

Json short_mapping(const Ticket& t, const Out& out) {
    // The list/ready shape omits fieldts, comments AND the body (decision E7).
    return ticket_to_mapping(t, MappingOptions{.prefix = out.prefix, .include_fieldts = false, .include_comments = false, .include_body = false});
}

Json full_mapping(const Ticket& t, const Out& out) {
    return ticket_to_mapping(t, MappingOptions{.prefix = out.prefix, .include_fieldts = true});
}

// ---------------------------------------------------------------------------
// Parser wiring.
// ---------------------------------------------------------------------------
Argument opt(std::vector<std::string> names, std::string help, Type type = Type::String) {
    Argument a;
    a.option_strings = std::move(names);
    a.help = std::move(help);
    a.type = type;
    return a;
}

Argument flag(std::vector<std::string> names, std::string help) {
    Argument a;
    a.option_strings = std::move(names);
    a.action = Action::StoreTrue;
    a.help = std::move(help);
    return a;
}

Argument positional(std::string dest, std::string help, NArgs nargs = NArgs::One, std::optional<std::string> metavar = std::nullopt) {
    Argument a;
    a.dest = std::move(dest);
    a.help = std::move(help);
    a.nargs = nargs;
    a.metavar = std::move(metavar);
    return a;
}

void add_json(Parser& p) { p.add_argument(flag({"--json"}, "emit machine-readable JSON")); }
void add_actor(Parser& p) { p.add_argument(opt({"--actor"}, "override the event actor (default: user/<git email> or runner from env)")); }
void add_body_file(Parser& p) { p.add_argument(opt({"--body-file"}, "read the text from a file ('-' reads stdin); UTF-8, no locale guessing")); }

std::unique_ptr<Parser> build_parser() {
    auto parser = std::make_unique<Parser>("rp", "Rohrpost — a git-native ticket system for agentic coding workflows.");
    Argument version;
    version.option_strings = {"--version"};
    version.action = Action::Version;
    version.version = std::string("rohrpost ") + RP_VERSION;
    parser->add_argument(std::move(version));

    Parser* p = &parser->add_subparser("init", "scaffold .rohrpost/ in this repository");
    p->add_argument(opt({"--prefix"}, "project id prefix (2-5 uppercase letters)"));
    add_json(*p);

    p = &parser->add_subparser("new", "create a ticket");
    p->add_argument(positional("title", "ticket title"));
    p->add_argument(opt({"--template"}, "load defaults from templates/<name>.toml"));
    p->add_argument(opt({"--type"}, "task | bug | spike | epic (default: task)"));
    p->add_argument(opt({"-p", "--priority"}, "0 highest .. 4 lowest", Type::Int));
    {
        Argument label = opt({"--label"}, "label (repeatable)");
        label.action = Action::Append;
        p->add_argument(std::move(label));
        Argument blocked = opt({"--blocked-by"}, "ticket id (repeatable)");
        blocked.action = Action::Append;
        p->add_argument(std::move(blocked));
    }
    p->add_argument(opt({"--parent"}, "parent epic id"));
    p->add_argument(opt({"--assignee"}, "assignee actor string"));
    p->add_argument(opt({"--body"}, "ticket body / description"));
    add_body_file(*p);
    add_actor(*p);
    add_json(*p);

    p = &parser->add_subparser("ready", "unblocked, actionable work");
    p->add_argument(opt({"--limit"}, "cap the number of results", Type::Int));
    add_json(*p);

    p = &parser->add_subparser("show", "show a ticket");
    p->add_argument(positional("id", "ticket id (bare or PREFIX-id)"));
    {
        Argument include = opt({"--include"}, "comma list of extra sections: body,deps,notes,fieldts (default: body)");
        p->add_argument(std::move(include));
    }
    add_json(*p);

    p = &parser->add_subparser("tree", "an epic and its children");
    p->add_argument(positional("id", "ticket id"));
    add_json(*p);

    p = &parser->add_subparser("list", "query tickets");
    p->add_argument(opt({"--status"}, "filter by (possibly derived) status"));
    p->add_argument(opt({"--label"}, "filter by label"));
    p->add_argument(opt({"--parent"}, "filter by parent id"));
    p->add_argument(opt({"--type"}, "filter by type"));
    p->add_argument(opt({"--match"}, "case-insensitive substring of the title"));
    add_json(*p);

    p = &parser->add_subparser("claim", "mark a ticket in_progress and stamp the actor");
    p->add_argument(positional("id", "ticket id"));
    add_actor(*p);
    add_json(*p);

    p = &parser->add_subparser("set", "update one or more fields (field=value ...)");
    p->add_argument(positional("id", "ticket id"));
    p->add_argument(positional("assignments", "e.g. status=done labels+=auth", NArgs::ZeroOrMore, "field=value"));
    add_body_file(*p);
    add_actor(*p);
    add_json(*p);

    p = &parser->add_subparser("close", "set status to done");
    p->add_argument(positional("id", "ticket id"));
    p->add_argument(opt({"--reason"}, "close reason (recorded on the event)"));
    add_actor(*p);
    add_json(*p);

    p = &parser->add_subparser("drop", "set status to dropped");
    p->add_argument(positional("id", "ticket id"));
    p->add_argument(opt({"--reason"}, "drop reason (recorded on the event)"));
    add_actor(*p);
    add_json(*p);

    p = &parser->add_subparser("comment", "append a local note");
    p->add_argument(positional("id", "ticket id"));
    p->add_argument(positional("text", "note text (or pass --body-file)", NArgs::Optional));
    add_body_file(*p);
    add_actor(*p);
    add_json(*p);

    p = &parser->add_subparser("comments", "show all notes on a ticket");
    p->add_argument(positional("id", "ticket id"));
    add_json(*p);

    p = &parser->add_subparser("link", "bind a ticket to a remote item");
    p->add_argument(positional("id", "ticket id"));
    p->add_argument(positional("remote", "remote name (e.g. github)"));
    p->add_argument(positional("ref", "remote item reference (e.g. issue number)"));
    add_actor(*p);
    add_json(*p);

    p = &parser->add_subparser("unlink", "remove a ticket's remote binding");
    p->add_argument(positional("id", "ticket id"));
    p->add_argument(positional("remote", "remote name (e.g. github)"));
    add_actor(*p);
    add_json(*p);

    p = &parser->add_subparser("log", "raw event history");
    p->add_argument(positional("id", "optional ticket id to filter to", NArgs::Optional));
    add_json(*p);

    p = &parser->add_subparser("doctor", "integrity and config checks");
    add_json(*p);

    p = &parser->add_subparser("compact", "archive old events and truncate the log (main only)");
    p->add_argument(flag({"--force"}, "bypass the clean-main-branch guard"));
    p->add_argument(opt({"--archive-after"}, std::format("days a terminal ticket must sit before archiving (default: {})", compact::kDefaultArchiveAfterDays), Type::Int));
    add_json(*p);

    p = &parser->add_subparser("stats", "repository statistics: body/line sizes, fold timing");
    add_json(*p);

    p = &parser->add_subparser("sync", "three-way sync with a remote tracker");
    p->add_argument(positional("remote", "remote name (default: the only one)", NArgs::Optional));
    p->add_argument(flag({"--dry-run"}, "print the plan, touch nothing"));
    add_json(*p);

    p = &parser->add_subparser("conflicts", "list tickets flagged by sync");
    add_json(*p);

    p = &parser->add_subparser("resolve", "clear a sync conflict");
    p->add_argument(positional("id", "ticket id"));
    {
        Argument take = opt({"--take"}, "");
        take.choices = {"local", "remote"};
        p->add_argument(std::move(take));
    }
    add_actor(*p);
    add_json(*p);
    return parser;
}

// ---------------------------------------------------------------------------
// Command handlers. Each returns an exit code.
// ---------------------------------------------------------------------------
fs::path repo_dir() { return paths::require_rohrpost_dir(); }

Out make_out(const Namespace& args) {
    const fs::path repo = repo_dir();
    const Config config = api::load_repo_config(repo);
    return Out{args.get_bool("json"), use_color(), config.prefix};
}

std::string actor_of(const Namespace& args) {
    return resolve_actor(args.get_str("actor"));
}

/// Read a `--body-file` argument: a path, or `-` for stdin (strict UTF-8).
std::string read_body_file(const std::string& spec) {
    std::string raw;
    if (spec == "-") {
        raw = io::read_stdin();
    } else {
        auto content = io::read_file(io::path_from_utf8(spec));
        if (!content) throw UsageError(std::format("--body-file: cannot read {}: {}", py::repr(spec), content.error()));
        raw = std::move(*content);
    }
    if (const auto bad = py::validate_utf8(raw)) {
        throw UsageError(std::format("--body-file: {} is not valid UTF-8 ({})", py::repr(spec), bad->message(raw)));
    }
    return raw;
}

std::optional<std::string> body_from_flags(std::optional<std::string> body, std::optional<std::string> body_file) {
    if (body && body_file) throw UsageError("--body and --body-file are mutually exclusive");
    if (body_file) return read_body_file(*body_file);
    return body;
}

int cmd_init(const Namespace& args) {
    const api::InitResult result = api::init_repo(paths::resolved_cwd(), args.get_str("prefix"));
    if (args.get_bool("json")) {
        Json obj = Json::object();
        obj["rohrpost_dir"] = io::path_str(result.rohrpost_dir);
        obj["prefix"] = result.prefix;
        obj["created_config"] = result.created_config;
        obj["updated_gitattributes"] = result.updated_gitattributes;
        obj["updated_gitignore"] = result.updated_gitignore;
        io::println(json::dumps(obj, json::kPyDefault));
        return 0;
    }
    io::println(std::format("Initialised rohrpost at {} (prefix={})", io::path_str(result.rohrpost_dir), result.prefix));
    if (result.created_config) io::println(std::format("  wrote {}", paths::kConfigFilename));
    if (result.updated_gitattributes) io::println("  updated .gitattributes (merge and line-ending rules)");
    if (result.updated_gitignore) io::println("  updated .gitignore (snapshot is regenerable)");
    return 0;
}

std::vector<std::string> template_list(const Json& defaults, const char* field) {
    if (!defaults.contains(field)) return {};
    const Json& value = defaults[field];
    if (!value.is_array()) throw RohrpostError(std::format("template {} must be a list", field));
    std::vector<std::string> out;
    for (const auto& item : value) out.push_back(json::py_str(item));
    return out;
}

std::int64_t template_priority(const Json& defaults) {
    if (!defaults.contains("priority")) return kDefaultPriority;
    const Json& value = defaults["priority"];
    if (value.is_boolean() || !value.is_number_integer()) throw RohrpostError("template priority must be an integer");
    return value.get<std::int64_t>();
}

std::optional<std::string> template_optional(const Json& defaults, const char* field) {
    if (!defaults.contains(field) || defaults[field].is_null()) return std::nullopt;
    return json::py_str(defaults[field]);
}

void print_summary(const Ticket& t, const Out& out, const TicketMap& by_id, std::optional<std::string> status_override = std::nullopt) {
    const std::string status = status_override.value_or(derive_status(t, by_id));
    io::println(std::format("{}  [{}]  {}  p{}  {}", out.rend(t.id), color_status(status, out.color), t.type, t.priority, t.title));
}

int cmd_new(const Namespace& args) {
    const Out out = make_out(args);
    const fs::path repo = repo_dir();
    const Json defaults = args.get_str("template") ? api::load_template(repo, *args.get_str("template")) : Json::object();
    std::optional<std::string> body = body_from_flags(args.get_str("body"), args.get_str("body_file"));
    if (!body) body = template_optional(defaults, "body");
    api::CreateOptions o;
    o.type = args.get_str("type").value_or(defaults.contains("type") ? json::py_str(defaults["type"]) : "task");
    o.priority = args.get_int("priority").value_or(template_priority(defaults));
    o.labels = args.get_list("label").value_or(template_list(defaults, "labels"));
    o.blocked_by = args.get_list("blocked_by").value_or(template_list(defaults, "blocked_by"));
    o.parent = args.get_str("parent") ? args.get_str("parent") : template_optional(defaults, "parent");
    o.assignee = args.get_str("assignee") ? args.get_str("assignee") : template_optional(defaults, "assignee");
    o.body = body;
    const api::WriteResult result = api::create_ticket(repo, *args.get_str("title"), o, actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("Created {}  {}", out.rend(result.ticket.id), result.ticket.title));
    return 0;
}

int cmd_ready(const Namespace& args) {
    const Out out = make_out(args);
    const fs::path repo = repo_dir();
    const TicketMap by_id = load_tickets(repo);
    const auto tickets = api::ready_tickets(repo, args.get_int("limit"));
    if (out.json) {
        Json arr = Json::array();
        for (const auto& t : tickets) arr.push_back(short_mapping(t, out));
        out.emit_json(arr);
        return 0;
    }
    if (tickets.empty()) {
        io::println("No actionable work. The tube is empty.");
        return 0;
    }
    for (const auto& t : tickets) print_summary(t, out, by_id, "ready");
    return 0;
}

void print_deps(const Ticket& t, const Out& out, const TicketMap& by_id) {
    if (t.blocked_by.empty()) return;
    io::println("  blocked_by:");
    for (const auto& dep : t.blocked_by) {
        const Ticket* blocker = by_id.find(dep);
        io::println(std::format("    - {} ({})", out.rend(dep), blocker ? blocker->status : "missing"));
    }
}

void print_detail(const Ticket& t, const Out& out, const std::string& include, const TicketMap& by_id) {
    std::set<std::string> sections;
    std::size_t start = 0;
    while (start <= include.size()) {
        const auto end = include.find(',', start);
        const std::string_view piece = std::string_view(include).substr(start, end == std::string::npos ? std::string::npos : end - start);
        const std::string_view trimmed = py::strip(piece);
        if (!trimmed.empty()) sections.emplace(trimmed);
        if (end == std::string::npos) break;
        start = end + 1;
    }
    const std::string status = derive_status(t, by_id);
    io::println(std::format("{}  {}", out.rend(t.id), t.title));
    io::println(std::format("  status:   {}", color_status(status, out.color)));
    io::println(std::format("  type:     {}", t.type));
    io::println(std::format("  priority: {}", t.priority));
    if (t.assignee && !t.assignee->empty()) io::println(std::format("  assignee:   {}", *t.assignee));
    if (t.parent && !t.parent->empty()) io::println(std::format("  parent:   {}", out.rend(*t.parent)));
    if (!t.labels.empty()) {
        std::string joined;
        for (std::size_t i = 0; i < t.labels.size(); ++i) {
            if (i) joined += ", ";
            joined += t.labels[i];
        }
        io::println(std::format("  labels:   {}", joined));
    }
    if (!t.remotes.empty()) {
        std::string joined;
        bool first = true;
        for (const auto& [k, v] : t.remotes) {
            if (!first) joined += ", ";
            first = false;
            joined += k + "/" + v;
        }
        io::println(std::format("  remotes:   {}", joined));
    }
    if (t.last_close_reason && !t.last_close_reason->empty()) io::println(std::format("  close:   {}", *t.last_close_reason));
    io::println(std::format("  created:  {}", t.created));
    io::println(std::format("  updated:  {}", t.updated));
    if (sections.contains("deps")) print_deps(t, out, by_id);
    if (sections.contains("body") && t.body && !t.body->empty()) {
        io::println();
        io::println(*t.body);
    }
    if (sections.contains("notes") && !t.comments.empty()) {
        io::println("  notes:");
        const std::size_t from = t.comments.size() > 10 ? t.comments.size() - 10 : 0;
        for (std::size_t i = from; i < t.comments.size(); ++i) {
            const auto& n = t.comments[i];
            io::println(std::format("    [{}] {}: {}", n.ts, n.actor, n.text));
        }
    }
    if (sections.contains("fieldts")) {
        io::println("  _fieldts:");
        std::vector<std::string> keys = t.fieldts.keys();
        std::sort(keys.begin(), keys.end());
        for (const auto& key : keys) io::println(std::format("    {}: {}", key, *t.fieldts.find(key)));
    }
}

int cmd_show(const Namespace& args) {
    const Out out = make_out(args);
    const fs::path repo = repo_dir();
    const Ticket ticket = api::show_ticket(repo, *args.get_str("id"));
    const TicketMap by_id = load_tickets(repo);
    if (out.json) {
        out.emit_json(full_mapping(ticket, out));
        return 0;
    }
    print_detail(ticket, out, args.get_str("include").value_or("body"), by_id);
    return 0;
}

int cmd_tree(const Namespace& args) {
    const Out out = make_out(args);
    const fs::path repo = repo_dir();
    const TicketMap by_id = load_tickets(repo);
    const api::Tree tree = api::tree(repo, *args.get_str("id"));
    if (out.json) {
        Json obj = Json::object();
        obj["root"] = full_mapping(tree.root, out);
        Json children = Json::array();
        for (const auto& c : tree.children) children.push_back(short_mapping(c, out));
        obj["children"] = children;
        out.emit_json(obj);
        return 0;
    }
    print_summary(tree.root, out, by_id);
    for (const auto& child : tree.children) {
        io::write_stdout("  ");
        print_summary(child, out, by_id);
    }
    return 0;
}

int cmd_list(const Namespace& args) {
    const Out out = make_out(args);
    const fs::path repo = repo_dir();
    const TicketMap by_id = load_tickets(repo);
    api::ListFilter filter;
    filter.status = args.get_str("status");
    filter.label = args.get_str("label");
    filter.parent = args.get_str("parent");
    filter.type = args.get_str("type");
    filter.match = args.get_str("match");
    const auto tickets = api::list_tickets(repo, filter);
    if (out.json) {
        Json arr = Json::array();
        for (const auto& t : tickets) arr.push_back(short_mapping(t, out));
        out.emit_json(arr);
        return 0;
    }
    if (tickets.empty()) {
        io::println("No tickets match.");
        return 0;
    }
    for (const auto& t : tickets) print_summary(t, out, by_id);
    return 0;
}

int cmd_claim(const Namespace& args) {
    const Out out = make_out(args);
    const api::WriteResult result = api::claim(repo_dir(), *args.get_str("id"), actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("{} {} -> in_progress", result.wrote ? "Claimed" : "Already claimed", out.rend(result.ticket.id)));
    return 0;
}

int cmd_set(const Namespace& args) {
    const Out out = make_out(args);
    std::vector<api::Assignment> assignments;
    for (const auto& tok : args.get_list("assignments").value_or(std::vector<std::string>{})) assignments.push_back(api::parse_assignment(tok));
    if (const auto body_file = args.get_str("body_file")) {
        const bool has_body = std::any_of(assignments.begin(), assignments.end(), [](const api::Assignment& a) {
            return a.op == api::Assignment::Op::Set && a.field == "body";
        });
        if (has_body) throw UsageError("body= and --body-file are mutually exclusive");
        assignments.push_back(api::Assignment{api::Assignment::Op::Set, "body", Json(read_body_file(*body_file))});
    } else if (assignments.empty()) {
        throw UsageError("set requires field=value assignments or --body-file");
    }
    const api::WriteResult result = api::set_fields(repo_dir(), *args.get_str("id"), assignments, actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("{} {}", result.wrote ? "Updated" : "No change to", out.rend(result.ticket.id)));
    return 0;
}

int cmd_close(const Namespace& args) {
    const Out out = make_out(args);
    const api::WriteResult result = api::close(repo_dir(), *args.get_str("id"), args.get_str("reason"), actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("{} {} -> done", result.wrote ? "Closed" : "Already closed", out.rend(result.ticket.id)));
    return 0;
}

int cmd_drop(const Namespace& args) {
    const Out out = make_out(args);
    const api::WriteResult result = api::drop(repo_dir(), *args.get_str("id"), args.get_str("reason"), actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("{} {} -> dropped", result.wrote ? "Dropped" : "Already dropped", out.rend(result.ticket.id)));
    return 0;
}

int cmd_comment(const Namespace& args) {
    const Out out = make_out(args);
    const auto text_arg = args.get_str("text");
    const auto body_file = args.get_str("body_file");
    if (text_arg && body_file) throw UsageError("comment text and --body-file are mutually exclusive");
    std::string text;
    if (body_file) text = read_body_file(*body_file);
    else if (text_arg) text = *text_arg;
    else throw UsageError("comment requires note text or --body-file");
    const api::WriteResult result = api::add_comment(repo_dir(), *args.get_str("id"), text, actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("Noted on {}", out.rend(result.ticket.id)));
    return 0;
}

int cmd_link(const Namespace& args) {
    const Out out = make_out(args);
    const api::WriteResult result = api::link_remote(repo_dir(), *args.get_str("id"), *args.get_str("remote"), *args.get_str("ref"), actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("Linked {} -> {}/{}", out.rend(result.ticket.id), *args.get_str("remote"), *args.get_str("ref")));
    return 0;
}

int cmd_unlink(const Namespace& args) {
    const Out out = make_out(args);
    const api::WriteResult result = api::unlink_remote(repo_dir(), *args.get_str("id"), *args.get_str("remote"), actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("{} {} from {}", result.wrote ? "Unlinked" : "Already unlinked", out.rend(result.ticket.id), *args.get_str("remote")));
    return 0;
}

int cmd_comments(const Namespace& args) {
    const Out out = make_out(args);
    const auto notes = api::comments(repo_dir(), *args.get_str("id"));
    if (out.json) {
        Json arr = Json::array();
        for (const auto& n : notes) arr.push_back(comment_to_mapping(n));
        out.emit_json(arr);
        return 0;
    }
    if (notes.empty()) {
        io::println("No notes.");
        return 0;
    }
    for (const auto& n : notes) io::println(std::format("[{}] {}: {}", n.ts, n.actor, n.text));
    return 0;
}

int cmd_log(const Namespace& args) {
    const Out out = make_out(args);
    const auto events = api::event_log(repo_dir(), args.get_str("id"));
    if (out.json) {
        Json arr = Json::array();
        for (const auto& e : events) arr.push_back(to_json(e));
        out.emit_json(arr);
        return 0;
    }
    if (events.empty()) {
        io::println("No events.");
        return 0;
    }
    for (const auto& e : events) {
        std::string ref;
        if (e.reason && !e.reason->empty()) ref = *e.reason;
        else if (e.text && !e.text->empty()) ref = *e.text;
        else if (e.remote && !e.remote->empty()) ref = *e.remote;
        const std::string detail = ref.empty() ? "" : "  " + ref;
        io::println(std::format("[{}] {} {} {} {}{}", e.ts, e.id, e.actor, e.op, out.rend(e.ticket), detail));
    }
    return 0;
}

int cmd_doctor(const Namespace& args) {
    return doctor::run(repo_dir(), args.get_bool("json"));
}

int cmd_compact(const Namespace& args) {
    compact::Options o;
    o.archive_after_days = static_cast<int>(args.get_int("archive_after").value_or(compact::kDefaultArchiveAfterDays));
    o.force = args.get_bool("force");
    o.json_output = args.get_bool("json");
    return compact::run(repo_dir(), o);
}

Json stats_dist(const Json& data, const char* key) {
    // Narrow a distribution mapping, defaulting the standard keys to 0 and
    // carrying non-standard keys through unchanged.
    const Json raw = (data.contains(key) && data[key].is_object()) ? data[key] : Json::object();
    Json base = Json::object();
    for (const auto* k : {"p50", "p90", "p95", "p99", "max", "count"}) base[k] = raw.contains(k) ? raw[k] : Json(0);
    for (const auto& [k, v] : raw.items()) {
        if (!base.contains(k)) base[k] = v;
    }
    return base;
}

int cmd_stats(const Namespace& args) {
    const Out out = make_out(args);
    const Json data = stats::compute_stats(repo_dir());
    if (out.json) {
        out.emit_json(data);
        return 0;
    }
    const Json body = stats_dist(data, "body_bytes");
    const Json line = stats_dist(data, "event_line_bytes");
    const std::string over = line.contains("over_pipe_buf") ? json::py_str(line["over_pipe_buf"]) : "0";
    const std::string lock_share = line.contains("lock_share_pct") ? json::py_str(line["lock_share_pct"]) : "0.0";
    io::println(std::format("events: {}  tickets: {}  PIPE_BUF: {}", json::py_str(data["events"]), json::py_str(data["tickets"]), json::py_str(data["pipe_buf"])));
    io::println(std::format("body bytes:       p50 {}  p90 {}  p95 {}  p99 {}  max {}  (n={})", json::py_str(body["p50"]), json::py_str(body["p90"]),
                            json::py_str(body["p95"]), json::py_str(body["p99"]), json::py_str(body["max"]), json::py_str(body["count"])));
    io::println(std::format("event line bytes: p50 {}  p95 {}  max {}  over PIPE_BUF: {} ({}% of set events)", json::py_str(line["p50"]),
                            json::py_str(line["p95"]), json::py_str(line["max"]), over, lock_share));
    io::println(std::format("cold fold: {} ms (median)", json::py_str(data["fold_ms"])));
    return 0;
}

std::unique_ptr<providers::Provider> build_provider(const std::string& remote, const Config& config) {
    const Json* raw = config.remotes.find(remote);
    if (raw == nullptr) throw RohrpostError(std::format("no [remotes.{}] configured", remote));
    if (remote == "github" || (raw->contains("type") && json::py_equal((*raw)["type"], Json("github")))) {
        return std::make_unique<providers::GitHubProvider>(*raw);
    }
    throw RohrpostError(std::format("no provider available for remote {}", py::repr(remote)));
}

std::string single_remote(const Config& config) {
    if (config.remotes.size() != 1) throw RohrpostError("specify a remote: rp sync <remote>");
    return config.remotes.keys().front();
}

int cmd_sync(const Namespace& args) {
    const Out out = make_out(args);
    const fs::path repo = repo_dir();
    const Config config = api::load_repo_config(repo);
    const std::string remote = args.get_str("remote").value_or("").empty() ? single_remote(config) : *args.get_str("remote");
    const auto provider = build_provider(remote, config);
    const bool dry_run = args.get_bool("dry_run");
    const sync::SyncReport report = sync::sync_round(repo, remote, *provider, config, sync::SyncOptions{.dry_run = dry_run});
    if (out.json) {
        Json payload = Json::object();
        payload["remote"] = report.remote;
        Json tickets = Json::array();
        for (const auto& t : report.tickets) {
            Json item = Json::object();
            item["ticket"] = out.prefix + "-" + t.ticket;
            item["ref"] = t.ref;
            item["pulled"] = t.pulled;
            item["pushed"] = t.pushed;
            item["conflicts"] = t.conflicts;
            tickets.push_back(item);
        }
        payload["tickets"] = tickets;
        payload["pulled"] = report.pulled();
        payload["pushed"] = report.pushed();
        payload["conflicts"] = report.conflicts();
        out.emit_json(payload);
        return 0;
    }
    io::println(std::format("Synced {}{}: pulled {}, pushed {}, {} conflict(s) across {} ticket(s).", report.remote, dry_run ? " (dry run)" : "",
                            report.pulled(), report.pushed(), report.conflicts(), report.tickets.size()));
    for (const auto& t : report.tickets) {
        if (!t.conflicts.empty()) {
            std::string joined;
            for (std::size_t i = 0; i < t.conflicts.size(); ++i) {
                if (i) joined += ",";
                joined += t.conflicts[i];
            }
            io::println(std::format("  conflict: {} on {}", out.rend(t.ticket), joined));
        }
    }
    return 0;
}

int cmd_conflicts(const Namespace& args) {
    const Out out = make_out(args);
    const auto tickets = api::list_conflicts(repo_dir());
    if (out.json) {
        Json arr = Json::array();
        for (const auto& t : tickets) arr.push_back(short_mapping(t, out));
        out.emit_json(arr);
        return 0;
    }
    if (tickets.empty()) {
        io::println("No conflicts.");
        return 0;
    }
    for (const auto& t : tickets) {
        Json labels = Json::array();
        for (const auto& l : t.labels) {
            if (l.starts_with("conflict:")) labels.push_back(l);
        }
        io::println(std::format("{} {}", out.rend(t.id), json::py_repr(labels)));
    }
    return 0;
}

int cmd_resolve(const Namespace& args) {
    const Out out = make_out(args);
    const auto take = args.get_str("take");
    if (!take || take->empty()) throw RohrpostError("resolve requires --take local|remote");
    const api::WriteResult result = api::resolve_conflict(repo_dir(), *args.get_str("id"), *take, actor_of(args));
    if (out.json) out.emit_json(full_mapping(result.ticket, out));
    else io::println(std::format("Resolved {} (took {})", out.rend(result.ticket.id), *take));
    return 0;
}

using Handler = int (*)(const Namespace&);
const std::map<std::string, Handler> kHandlers = {
    {"init", cmd_init},       {"new", cmd_new},           {"ready", cmd_ready},     {"show", cmd_show},
    {"tree", cmd_tree},       {"list", cmd_list},         {"claim", cmd_claim},     {"set", cmd_set},
    {"close", cmd_close},     {"drop", cmd_drop},         {"comment", cmd_comment}, {"comments", cmd_comments},
    {"link", cmd_link},       {"unlink", cmd_unlink},     {"log", cmd_log},         {"doctor", cmd_doctor},
    {"compact", cmd_compact}, {"stats", cmd_stats},       {"sync", cmd_sync},       {"conflicts", cmd_conflicts},
    {"resolve", cmd_resolve},
};

}  // namespace

int main(const std::vector<std::string>& argv) {
    const auto parser = build_parser();
    Namespace args;
    try {
        args = parser->parse_args(argv);
    } catch (const argparse::ExitWithOutput& exit) {
        io::write_stdout(exit.text);
        return 0;
    } catch (const argparse::ParseError& err) {
        io::flush_stdout();
        io::write_stderr(err.usage);
        io::eprintln(std::format("{}: error: {}", err.prog, err.message));
        return 2;
    }
    const auto command = args.get_str("command");
    if (!command) {
        io::write_stdout(parser->format_help());
        return 0;
    }
    try {
        return kHandlers.at(*command)(args);
    } catch (const UsageError& exc) {
        io::flush_stdout();
        io::eprintln(std::format("rp: {}", exc.what()));
        return 2;
    } catch (const RohrpostError& exc) {
        io::flush_stdout();
        io::eprintln(std::format("rp: {}", exc.what()));
        return 1;
    }
}

}  // namespace rp::cli
