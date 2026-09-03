#include "rohrpost/compact.hpp"

#include "rohrpost/config.hpp"
#include "rohrpost/errors.hpp"
#include "rohrpost/fold.hpp"
#include "rohrpost/ids.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/paths.hpp"
#include "rohrpost/pyfmt.hpp"
#include "rohrpost/store.hpp"
#include "rohrpost/subprocess.hpp"
#include "rohrpost/timeutil.hpp"

#include <algorithm>
#include <chrono>
#include <format>
#include <map>
#include <set>

namespace rp::compact {
namespace {

namespace fs = std::filesystem;

std::int64_t parse_ts(const std::string& ts) {
    const auto parsed = timeutil::parse_ts(ts);
    if (!parsed) throw StoreError(std::format("Invalid isoformat string: {}", py::repr(ts)));
    return *parsed;
}

/// `(is_dirty, current_branch)`; `(false, "")` without git.
std::pair<bool, std::string> git_state(const fs::path& repo_root) {
    const std::string root = io::path_str(repo_root);
    auto status = subprocess::run({"git", "-C", root, "status", "--porcelain"}, std::chrono::seconds(10));
    if (!status) return {false, ""};
    const bool dirty = !py::strip(status->stdout_bytes).empty();
    auto branch = subprocess::run({"git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"}, std::chrono::seconds(10));
    if (!branch) return {false, ""};
    return {dirty, std::string(py::strip(branch->stdout_bytes))};
}

/// A refusal reason if compaction must not proceed, else nullopt.
std::optional<std::string> guard(const fs::path& repo_root, bool force, const std::optional<std::string>& default_branch) {
    if (force) return std::nullopt;
    const auto [dirty, branch] = git_state(repo_root);
    if (branch.empty()) return std::nullopt;  // outside git — nothing to protect
    if (dirty) return "refusing to compact: working tree is dirty (use --force to override)";
    const std::string expected = default_branch.value_or("main");
    if (branch != expected) {
        return std::format("refusing to compact: HEAD is on {}, not {} (use --force to override)", py::repr(branch), py::repr(expected));
    }
    return std::nullopt;
}

std::string ticket_of(const Event& ev) {
    try {
        return ids::normalize_id(ev.ticket);
    } catch (const StoreError&) {
        return ev.ticket;  // mirrors the reference: IdError propagates
    }
}

bool is_terminal_set(const Event& e) {
    if (e.op != "set" || !e.set || e.set->empty()) return false;
    const auto it = e.set->find("status");
    return it != e.set->end() && it->is_string() && is_terminal(it->get<std::string>());
}

std::set<std::string> archivable_ids(const std::vector<Event>& events, const TicketMap& by_id, std::int64_t cutoff_ms) {
    std::set<std::string> out;
    for (const auto& [tid, ticket] : by_id) {
        if (!is_terminal(ticket.status)) continue;
        std::optional<std::int64_t> latest;
        for (const auto& e : events) {
            if (is_terminal_set(e) && ticket_of(e) == tid) {
                const std::int64_t t = parse_ts(e.ts);
                if (!latest || t > *latest) latest = t;
            }
        }
        if (latest && *latest < cutoff_ms) out.insert(tid);
    }
    return out;
}

void sort_events(std::vector<Event>& events) {
    std::stable_sort(events.begin(), events.end(), [](const Event& a, const Event& b) {
        return a.ts != b.ts ? a.ts < b.ts : a.id < b.id;
    });
}

void rewrite_log(const fs::path& dir, std::vector<Event> keep) {
    sort_events(keep);
    std::string payload;
    for (const auto& e : keep) {
        payload += encode(e);
        payload.push_back('\n');
    }
    const fs::path log = paths::log_path(dir);
    fs::path tmp = log;
    tmp.replace_extension(".jsonl.tmp");
    io::write_file_atomic(log, tmp, payload);
}

void append_archive(const fs::path& dir, const std::string& bucket, std::vector<Event> evs) {
    const fs::path adir = paths::archive_dir(dir);
    std::error_code ec;
    fs::create_directories(adir, ec);
    sort_events(evs);
    std::string payload;
    for (const auto& e : evs) {
        payload += encode(e);
        payload.push_back('\n');
    }
    io::append_file(adir / io::path_from_utf8(bucket), payload);
}

void fail(const std::string& message, bool json_output) {
    if (json_output) {
        Json obj = Json::object();
        obj["error"] = message;
        io::println(json::dumps(obj, json::kPretty));
    } else {
        io::eprintln(std::format("rp compact: {}", message));
    }
}

void report(const CompactResult& result, bool json_output) {
    if (json_output) {
        io::println(json::dumps(result.to_mapping(), json::kPretty));
        return;
    }
    io::println(std::format("Compacted: archived {} event(s), kept {}.", result.archived, result.remaining));
    if (!result.archive_files.empty()) {
        std::string joined;
        for (std::size_t i = 0; i < result.archive_files.size(); ++i) {
            if (i) joined += ", ";
            joined += result.archive_files[i];
        }
        io::println("  archive files: " + joined);
    }
}

}  // namespace

Json CompactResult::to_mapping() const {
    Json m = Json::object();
    m["archived"] = archived;
    m["remaining"] = remaining;
    m["archive_files"] = archive_files;
    return m;
}

std::string quarter_bucket(const std::string& ts) {
    const auto civil = timeutil::to_civil(parse_ts(ts));
    const unsigned quarter = (civil.month - 1) / 3 + 1;
    return std::format("log-{}-Q{}.jsonl", civil.year, quarter);
}

int run(const fs::path& dir, const Options& options) {
    const Config config = load_config(dir);
    if (const auto refusal = guard(dir.parent_path(), options.force, config.default_branch)) {
        fail(*refusal, options.json_output);
        return 1;
    }
    const std::int64_t now_ms = options.now_ms.value_or(timeutil::now_epoch_ms());
    const std::int64_t cutoff = now_ms - static_cast<std::int64_t>(options.archive_after_days) * 86'400'000LL;

    std::vector<Event> keep;
    std::map<std::string, std::vector<Event>> buckets;  // sorted by bucket name
    {
        // Read and partition while holding the same lock as the rewrite.
        store::FileLock lock(dir);
        const std::vector<Event> events_all = store::read_events(dir);
        const TicketMap by_id = fold(events_all);
        const auto archivable = archivable_ids(events_all, by_id, cutoff);
        for (const auto& ev : events_all) {
            if (archivable.contains(ticket_of(ev))) buckets[quarter_bucket(ev.ts)].push_back(ev);
            else keep.push_back(ev);
        }
        rewrite_log(dir, keep);
        for (auto& [bucket, evs] : buckets) append_archive(dir, bucket, evs);
    }
    // Drop the stale snapshot so the next read regenerates it (best-effort).
    std::error_code ec;
    fs::remove(paths::snapshot_path(dir), ec);

    CompactResult result{0, keep.size(), {}};
    for (const auto& [bucket, evs] : buckets) {
        result.archived += evs.size();
        result.archive_files.push_back(bucket);
    }
    report(result, options.json_output);
    return 0;
}

}  // namespace rp::compact
