#include "rohrpost/providers/github.hpp"

#include "rohrpost/errors.hpp"
#include "rohrpost/http.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/pyfmt.hpp"
#include "rohrpost/subprocess.hpp"

#include <algorithm>
#include <chrono>
#include <format>

namespace rp::providers {
namespace {

std::optional<std::string> token_of(const EnvLookup& env) {
    if (const auto t = env("GITHUB_TOKEN"); t && !t->empty()) return t;
    if (const auto t = env("ROHRPOST_GITHUB_TOKEN"); t && !t->empty()) return t;
    return std::nullopt;
}

std::string rstrip_slash(std::string text) {
    while (!text.empty() && text.back() == '/') text.pop_back();
    return text;
}

/// httpx's `raise_for_status` message shape, so the failure reads the same.
[[noreturn]] void raise_for_status(int status, const std::string& url) {
    const char* kind = status >= 500 ? "Server error" : "Client error";
    throw RohrpostError(std::format("{} '{}' for url '{}'", kind, status, url));
}

}  // namespace

GitHubProvider::GitHubProvider(Json config, EnvLookup env, GhRunner gh_runner, std::optional<bool> prefer_gh)
    : config_(std::move(config)),
      env_(env ? std::move(env) : EnvLookup([](std::string_view name) { return io::getenv(name); })),
      gh_runner_(std::move(gh_runner)) {
    repo_ = config_.contains("repo") ? json::py_str(config_["repo"]) : "";
    base_url_ = rstrip_slash(config_.contains("url") ? json::py_str(config_["url"]) : "https://api.github.com");
    fields_ = (config_.contains("fields") && config_["fields"].is_object()) ? config_["fields"] : Json::object();
    prefer_gh_ = prefer_gh.has_value() ? *prefer_gh : subprocess::which("gh").has_value();
}

std::optional<Json> GitHubProvider::try_gh(const std::vector<std::string>& args) const {
    std::optional<std::string> stdout_text;
    if (gh_runner_) {
        stdout_text = gh_runner_(args);
        if (!stdout_text) return std::nullopt;
    } else {
        std::vector<std::string> argv{"gh"};
        argv.insert(argv.end(), args.begin(), args.end());
        auto proc = subprocess::run(argv, std::chrono::seconds(30));
        if (!proc) {
            if (proc.error() == subprocess::Failure::Timeout) {
                throw RohrpostError("Command '['gh', ...]' timed out after 30 seconds");
            }
            return std::nullopt;  // gh missing (FileNotFoundError)
        }
        if (proc->returncode != 0) return std::nullopt;  // present but errored (e.g. not authenticated)
        stdout_text = proc->stdout_bytes;
    }
    if (py::strip(*stdout_text).empty()) return Json::object();
    auto parsed = json::parse(*stdout_text);
    if (!parsed) return std::nullopt;
    return std::move(*parsed);
}

std::vector<std::pair<std::string, std::string>> GitHubProvider::headers() const {
    std::vector<std::pair<std::string, std::string>> h{{"Accept", "application/vnd.github+json"}};
    if (const auto token = token_of(env_)) h.emplace_back("Authorization", "Bearer " + *token);
    return h;
}

std::string GitHubProvider::issue_path(std::string_view ref) const {
    return std::format("repos/{}/issues/{}", repo_, ref);
}

Json GitHubProvider::scalar_map() const {
    Json out = Json::object();
    for (const auto& [name, target] : fields_.items()) {
        if (target.is_string()) out[name] = target;
    }
    return out;
}

Json GitHubProvider::status_map() const {
    const auto it = fields_.find("status");
    return (it != fields_.end() && it->is_object()) ? *it : Json::object();
}

Json GitHubProvider::to_remote(const Json& local_fields) const {
    Json payload = Json::object();
    for (const auto& [local_name, remote_name] : scalar_map().items()) {
        if (local_fields.contains(local_name)) payload[remote_name.get<std::string>()] = local_fields[local_name];
    }
    if (local_fields.contains("labels") && fields_.contains("labels")) {
        Json labels = Json::array();
        if (local_fields["labels"].is_array()) {
            for (const auto& l : local_fields["labels"]) labels.push_back(l);
        }
        payload["labels"] = labels;
    }
    if (local_fields.contains("status")) {
        const std::string status = json::py_str(local_fields["status"]);
        const Json sm = status_map();
        if (sm.contains(status)) payload["state"] = sm[status];
    }
    return payload;
}

Json GitHubProvider::to_local(const Json& issue) const {
    Json local = Json::object();
    if (!issue.is_object()) return local;
    for (const auto& [local_name, remote_name] : scalar_map().items()) {
        const std::string rn = remote_name.get<std::string>();
        if (issue.contains(rn)) local[local_name] = issue[rn];
    }
    if (fields_.contains("labels") && issue.contains("labels")) {
        std::vector<std::string> names;
        if (issue["labels"].is_array()) {
            for (const auto& label : issue["labels"]) {
                if (label.is_object() && label.contains("name")) names.push_back(json::py_str(label["name"]));
                else names.push_back("");
            }
        }
        std::sort(names.begin(), names.end());
        local["labels"] = names;
    }
    if (issue.contains("state") && !issue["state"].is_null()) {
        const std::string state = json::py_str(issue["state"]);
        for (const auto& [status, mapped] : status_map().items()) {
            if (json::py_equal(mapped, Json(state))) {
                local["status"] = status;
                break;
            }
        }
    }
    return local;
}

Json GitHubProvider::fetch(std::string_view ref) {
    if (prefer_gh_) {
        if (auto data = try_gh({"api", issue_path(ref)})) return to_local(*data);
    }
    const std::string url = base_url_ + "/" + issue_path(ref);
    auto resp = http::perform(http::Request{.method = "GET", .url = url, .headers = headers()});
    if (!resp) throw RohrpostError(std::format("GitHub request failed: {}", resp.error()));
    if (resp->status == 404) throw RemoteItemNotFoundError(std::format("GitHub issue {} no longer exists", ref));
    if (resp->status >= 400) raise_for_status(resp->status, url);
    auto body = json::parse(resp->body);
    if (!body) throw RohrpostError("GitHub returned invalid JSON");
    return to_local(*body);
}

std::vector<std::string> GitHubProvider::gh_field_args(const Json& payload) {
    std::vector<std::string> args;
    for (const auto& [key, value] : payload.items()) {
        if (value.is_array()) {
            for (const auto& item : value) {
                args.push_back("-f");
                args.push_back(std::format("{}[]={}", key, json::py_str(item)));
            }
        } else {
            args.push_back("-f");
            args.push_back(std::format("{}={}", key, json::py_str(value)));
        }
    }
    return args;
}

Json GitHubProvider::push(std::string_view ref, const Json& fields) {
    const Json payload = to_remote(fields);
    if (prefer_gh_) {
        std::vector<std::string> args{"api", "-X", "PATCH", issue_path(ref)};
        const auto extra = gh_field_args(payload);
        args.insert(args.end(), extra.begin(), extra.end());
        if (auto data = try_gh(args)) return to_local(*data);
    }
    const std::string url = base_url_ + "/" + issue_path(ref);
    auto h = headers();
    h.emplace_back("Content-Type", "application/json");
    auto resp = http::perform(http::Request{.method = "PATCH", .url = url, .headers = h, .body = json::dumps(payload, json::kCompact)});
    if (!resp) throw RohrpostError(std::format("GitHub request failed: {}", resp.error()));
    if (resp->status >= 400) raise_for_status(resp->status, url);
    auto body = json::parse(resp->body);
    if (!body) throw RohrpostError("GitHub returned invalid JSON");
    return to_local(*body);
}

}  // namespace rp::providers
