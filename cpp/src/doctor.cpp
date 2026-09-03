#include "rohrpost/doctor.hpp"

#include "rohrpost/config.hpp"
#include "rohrpost/errors.hpp"
#include "rohrpost/fold.hpp"
#include "rohrpost/http.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/paths.hpp"
#include "rohrpost/pyfmt.hpp"
#include "rohrpost/shadow.hpp"
#include "rohrpost/store.hpp"
#include "rohrpost/subprocess.hpp"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <filesystem>
#include <format>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <vector>

namespace rp::doctor {
namespace {

namespace fs = std::filesystem;

std::string join(const std::vector<std::string>& parts, std::string_view sep) {
    std::string out;
    for (std::size_t i = 0; i < parts.size(); ++i) {
        if (i) out += sep;
        out += parts[i];
    }
    return out;
}

/// `repr(list[:3])` for the report's sample of offenders.
std::string sample_repr(const std::vector<std::string>& items) {
    Json arr = Json::array();
    for (std::size_t i = 0; i < items.size() && i < 3; ++i) arr.push_back(items[i]);
    return json::py_repr(arr);
}

Finding check_log_parses(const std::vector<Event>& events, const std::vector<std::string>& errors) {
    if (!errors.empty()) return Finding{"log_parses", false, std::format("{} malformed line(s); first: {}", errors.size(), errors.front())};
    return Finding{"log_parses", true, std::format("{} event(s) parsed cleanly", events.size())};
}

Finding check_no_duplicate_ids(bool log_ok, const std::vector<Event>& events) {
    if (!log_ok) return Finding{"no_duplicate_ids", true, "skipped (log unparseable)"};
    std::set<std::string> seen;
    std::set<std::string> dupes;
    for (const auto& ev : events) {
        if (!seen.insert(ev.id).second) dupes.insert(ev.id);
    }
    if (!dupes.empty()) return Finding{"no_duplicate_ids", false, std::format("{} duplicate event id(s) after merge", dupes.size())};
    return Finding{"no_duplicate_ids", true, std::format("{} unique event id(s)", seen.size())};
}

Finding check_references_resolve(const fs::path& dir, bool log_ok) {
    if (!log_ok) return Finding{"references_resolve", true, "skipped (log unparseable)"};
    const TicketMap by_id = fold_all(dir);
    std::vector<std::string> missing;
    for (const auto& [id, ticket] : by_id) {
        if (ticket.parent && !ticket.parent->empty() && !by_id.contains(*ticket.parent)) {
            missing.push_back(std::format("{} -> parent {}", ticket.id, *ticket.parent));
        }
        for (const auto& dep : ticket.blocked_by) {
            if (!by_id.contains(dep)) missing.push_back(std::format("{} -> blocked_by {}", ticket.id, dep));
        }
    }
    if (!missing.empty()) return Finding{"references_resolve", false, std::format("{} dangling reference(s): {}", missing.size(), sample_repr(missing))};
    return Finding{"references_resolve", true, "all parent/blocked_by references resolve"};
}

Finding check_no_cycles(const fs::path& dir, bool log_ok) {
    if (!log_ok) return Finding{"no_cycles", true, "skipped (log unparseable)"};
    const TicketMap by_id = fold_all(dir);
    if (const auto cycle = find_cycle(by_id)) return Finding{"no_cycles", false, std::format("dependency cycle: {}", join(*cycle, " -> "))};
    return Finding{"no_cycles", true, "no dependency cycles"};
}

Finding check_gitattributes(const fs::path& dir) {
    const fs::path path = dir.parent_path() / ".gitattributes";
    std::error_code ec;
    if (!fs::is_regular_file(path, ec)) return Finding{"gitattributes", false, ".gitattributes missing the required rules"};
    auto text = io::read_file(path);
    if (!text) throw StoreError(std::format("cannot read {}: {}", io::path_str(path), text.error()));
    // Universal newlines, like Path.read_text.
    std::string normalised;
    for (std::size_t i = 0; i < text->size(); ++i) {
        if ((*text)[i] == '\r') {
            normalised.push_back('\n');
            if (i + 1 < text->size() && (*text)[i + 1] == '\n') ++i;
        } else {
            normalised.push_back((*text)[i]);
        }
    }
    std::vector<std::string> missing;
    for (const auto rule : paths::kGitattributesRules) {
        if (normalised.find(rule) == std::string::npos) missing.emplace_back(rule);
    }
    if (!missing.empty()) {
        Json arr = Json::array();
        for (const auto& m : missing) arr.push_back(m);
        return Finding{"gitattributes", false, std::format("missing rule(s): {}", json::py_repr(arr))};
    }
    return Finding{"gitattributes", true, "merge and line-ending rules present"};
}

Finding check_snapshot_matches(const fs::path& dir, bool log_ok) {
    if (!log_ok) return Finding{"snapshot_matches", true, "skipped (log unparseable)"};
    const fs::path snap = paths::snapshot_path(dir);
    const TicketMap fresh = fold_all(dir);
    std::error_code ec;
    if (!fs::is_regular_file(snap, ec)) return Finding{"snapshot_matches", true, "no snapshot on disk (will be generated on next read)"};
    const auto cached = read_snapshot(snap);
    if (!cached) return Finding{"snapshot_matches", false, "snapshot on disk is unreadable"};
    if (!(fresh == *cached)) return Finding{"snapshot_matches", false, "tickets.jsonl is stale relative to the log"};
    return Finding{"snapshot_matches", true, std::format("snapshot matches a fresh fold ({} ticket(s))", fresh.size())};
}

Finding check_shadow_files(const fs::path& dir, bool log_ok) {
    if (!log_ok) return Finding{"shadow_files", true, "skipped (log unparseable)"};
    const TicketMap by_id = fold_all(dir);
    bool any_remote = false;
    std::vector<std::string> missing;
    for (const auto& [id, t] : by_id) {
        for (const auto& [name, ref] : t.remotes) {
            any_remote = true;
            if (!shadow::read_shadow(dir, name, ref)) missing.push_back(std::format("{} -> {}/{}", t.id, name, ref));
        }
    }
    if (!any_remote) return Finding{"shadow_files", true, "no remotes in use (sync not configured)"};
    if (!missing.empty()) return Finding{"shadow_files", false, std::format("{} missing/invalid shadow file(s): {}", missing.size(), sample_repr(missing))};
    return Finding{"shadow_files", true, "all linked remotes have shadow files"};
}

bool github_token_authenticated(const std::string& token, const Json& remote_config) {
    std::string base_url = remote_config.contains("url") ? json::py_str(remote_config["url"]) : "https://api.github.com";
    while (!base_url.empty() && base_url.back() == '/') base_url.pop_back();
    auto resp = http::perform(http::Request{
        .method = "GET",
        .url = base_url + "/user",
        .headers = {{"Accept", "application/vnd.github+json"}, {"Authorization", "Bearer " + token}},
        .timeout = std::chrono::seconds(10),
    });
    return resp && resp->status >= 200 && resp->status < 300;
}

bool gh_cli_authenticated() {
    if (!subprocess::which("gh")) return false;
    auto result = subprocess::run({"gh", "auth", "status"}, std::chrono::seconds(10));
    return result && result->returncode == 0;
}

bool github_authenticated(const Json& remote_config) {
    for (const auto* var : {"GITHUB_TOKEN", "ROHRPOST_GITHUB_TOKEN"}) {
        const auto token = io::getenv(var);
        if (token && !token->empty()) {
            const std::string_view trimmed = py::strip(*token);
            if (!trimmed.empty()) return github_token_authenticated(std::string(trimmed), remote_config);
            break;  // Python: `token and token.strip()` falls through to gh when blank
        }
    }
    return gh_cli_authenticated();
}

bool remote_authenticated(const std::string& name, const Json& remote_config) {
    std::string remote_type = remote_config.contains("type") ? json::py_str(remote_config["type"]) : name;
    for (char& c : remote_type) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    if (remote_type == "github") return github_authenticated(remote_config);
    Json token_env;
    if (remote_config.contains("token_env") && !remote_config["token_env"].is_null() && !(remote_config["token_env"].is_string() && remote_config["token_env"].get<std::string>().empty())) {
        token_env = remote_config["token_env"];
    } else if (remote_config.contains("credential_env")) {
        token_env = remote_config["credential_env"];
    }
    if (token_env.is_string() && !py::strip(token_env.get<std::string>()).empty()) {
        const auto value = io::getenv(py::strip(token_env.get<std::string>()));
        return value && !value->empty();
    }
    return false;
}

Finding check_remote_credentials(const fs::path& dir) {
    Config config;
    try {
        config = load_config(dir);
    } catch (const ConfigError& exc) {
        return Finding{"remote_credentials", false, std::format("cannot load remotes: {}", exc.what())};
    }
    if (config.remotes.empty()) return Finding{"remote_credentials", true, "no remotes configured"};
    std::vector<std::string> missing;
    for (const auto& [name, remote_config] : config.remotes) {
        if (!remote_authenticated(name, remote_config)) missing.push_back(name);
    }
    if (!missing.empty()) {
        std::sort(missing.begin(), missing.end());
        return Finding{"remote_credentials", false, std::format("no authenticated credential source for: {}", join(missing, ", "))};
    }
    return Finding{"remote_credentials", true, std::format("authenticated credential source present for {} remote(s)", config.remotes.size())};
}

void print_report(const std::vector<Finding>& findings) {
    const bool all_ok = std::all_of(findings.begin(), findings.end(), [](const Finding& f) { return f.ok; });
    io::println(all_ok ? "rp doctor: all clear" : "rp doctor: problems found");
    for (const auto& f : findings) io::println(std::format("  [{}] {}: {}", f.ok ? "ok " : "XX ", f.check, f.detail));
    if (all_ok) {
        io::println("Nothing stuck in the tube.");
    } else {
        const auto bad = std::count_if(findings.begin(), findings.end(), [](const Finding& f) { return !f.ok; });
        io::println(std::format("{} {} need attention.", bad, bad != 1 ? "checks" : "check"));
    }
}

}  // namespace

Json Finding::to_mapping() const {
    Json m = Json::object();
    m["check"] = check;
    m["ok"] = ok;
    m["detail"] = detail;
    return m;
}

std::vector<Finding> run_checks(const fs::path& dir) {
    auto [events, errors] = store::read_events_lenient(dir);
    const bool log_ok = errors.empty();
    return {
        check_log_parses(events, errors),
        check_no_duplicate_ids(log_ok, events),
        check_references_resolve(dir, log_ok),
        check_no_cycles(dir, log_ok),
        check_gitattributes(dir),
        check_snapshot_matches(dir, log_ok),
        check_shadow_files(dir, log_ok),
        check_remote_credentials(dir),
    };
}

int run(const fs::path& dir, bool json_output) {
    const auto checks = run_checks(dir);
    const bool all_ok = std::all_of(checks.begin(), checks.end(), [](const Finding& f) { return f.ok; });
    if (json_output) {
        Json arr = Json::array();
        for (const auto& f : checks) arr.push_back(f.to_mapping());
        io::println(json::dumps(arr, json::kPretty));
        return all_ok ? 0 : 1;
    }
    print_report(checks);
    return all_ok ? 0 : 1;
}

}  // namespace rp::doctor
