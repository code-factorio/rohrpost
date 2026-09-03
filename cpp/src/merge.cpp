#include "rohrpost/merge.hpp"

#include "rohrpost/entropy.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/subprocess.hpp"

#include <algorithm>
#include <chrono>
#include <exception>
#include <filesystem>
#include <format>
#include <optional>
#include <set>
#include <string>
#include <system_error>
#include <utility>

namespace rp::merge {
namespace {

namespace fs = std::filesystem;

Json get_or_null(const Json& obj, const std::string& name) {
    const auto it = obj.find(name);
    return it == obj.end() ? Json() : *it;
}

bool is_setlike(const Json& v) { return v.is_null() || v.is_array(); }
bool is_strlike(const Json& v) { return v.is_null() || v.is_string(); }

std::set<std::string> as_set(const Json& v) {
    std::set<std::string> out;
    if (v.is_array()) {
        for (const auto& item : v) out.insert(json::py_str(item));
    }
    return out;
}

Json sorted_json(const std::set<std::string>& s) {
    Json arr = Json::array();
    for (const auto& item : s) arr.push_back(item);
    return arr;
}

void apply_conflict_policy(MergeResult& r, const std::string& name, const Json& local, const Json& remote, Policy policy) {
    if (policy == Policy::Local) {
        r.resolved.push_back(FieldConflict{name, local, remote, std::nullopt});
        r.local_won[name] = local;
    } else if (policy == Policy::Remote) {
        r.resolved.push_back(FieldConflict{name, local, remote, std::nullopt});
        r.remote_won[name] = remote;
    } else {
        r.conflicts.push_back(FieldConflict{name, local, remote, std::nullopt});
    }
}

std::string str_or_empty(const Json& v) {
    // `str(b or "")`: None/empty -> "", else the string itself.
    if (v.is_null()) return "";
    return json::py_str(v);
}

void merge_body(MergeResult& r, const std::string& name, const Json& b, const Json& lv, const Json& rv, Policy policy) {
    auto [merged, conflict] = merge_text(str_or_empty(b), str_or_empty(lv), str_or_empty(rv));
    if (conflict) {
        if (policy == Policy::Flag) {
            r.remote_won[name] = merged;
            r.conflicts.push_back(FieldConflict{name, lv, rv, Json(merged)});
            return;
        }
        apply_conflict_policy(r, name, lv, rv, policy);
        return;
    }
    if (!json::py_equal(Json(merged), lv)) r.remote_won[name] = merged;
    if (!json::py_equal(Json(merged), rv)) r.local_won[name] = merged;
}

void merge_scalar(MergeResult& r, const std::string& name, const Json& b, const Json& lv, const Json& rv, Policy policy) {
    if (json::py_equal(lv, b)) r.remote_won[name] = rv;
    else if (json::py_equal(rv, b)) r.local_won[name] = lv;
    else apply_conflict_policy(r, name, lv, rv, policy);
}

void merge_set(MergeResult& r, const std::string& name, const Json& base, const Json& local, const Json& remote) {
    const auto base_set = as_set(base);
    const auto local_set = as_set(local);
    const auto remote_set = as_set(remote);
    std::set<std::string> merged;
    // (base - (base - local) - (base - remote)) | (local - base) | (remote - base)
    for (const auto& item : base_set) {
        if (local_set.contains(item) && remote_set.contains(item)) merged.insert(item);
    }
    for (const auto& item : local_set) {
        if (!base_set.contains(item)) merged.insert(item);
    }
    for (const auto& item : remote_set) {
        if (!base_set.contains(item)) merged.insert(item);
    }
    const Json value = sorted_json(merged);
    if (merged != local_set) r.remote_won[name] = value;
    if (merged != remote_set) r.local_won[name] = value;
}

}  // namespace

std::pair<std::string, bool> merge_text(const std::string& base, const std::string& local, const std::string& remote) {
    std::error_code ec;
    const fs::path tmp = fs::temp_directory_path(ec) / io::path_from_utf8(std::format("rohrpost-merge-{:016x}", entropy::randbits(64)));
    if (ec || !fs::create_directories(tmp, ec)) return {local, local != remote};
    const fs::path base_p = tmp / "base";
    const fs::path local_p = tmp / "local";
    const fs::path remote_p = tmp / "remote";
    try {
        io::write_file(base_p, base);
        io::write_file(local_p, local);
        io::write_file(remote_p, remote);
    } catch (const std::exception&) {
        fs::remove_all(tmp, ec);
        return {local, local != remote};
    }
    auto proc = subprocess::run({"git", "merge-file", "-p", io::path_str(local_p), io::path_str(base_p), io::path_str(remote_p)},
                                std::chrono::minutes(5));
    fs::remove_all(tmp, ec);
    // git merge-file: 0 = clean, 1 = conflicts, >1 = error/usage.
    if (!proc || proc->returncode > 1 || proc->returncode < 0) return {local, local != remote};
    return {proc->stdout_bytes, proc->returncode == 1};
}

MergeResult three_way(const Json& base, const Json& local, const Json& remote, Policy policy) {
    MergeResult result;
    std::set<std::string> names;
    for (const auto* src : {&base, &local, &remote}) {
        if (src->is_object()) {
            for (const auto& [k, v] : src->items()) names.insert(k);
        }
    }
    for (const auto& name : names) {
        const Json b = get_or_null(base, name);
        const Json lv = get_or_null(local, name);
        const Json rv = get_or_null(remote, name);
        if (name == "labels" && is_setlike(b) && is_setlike(lv) && is_setlike(rv)) {
            merge_set(result, name, b, lv, rv);
            continue;
        }
        if (json::py_equal(lv, rv)) continue;
        if (name == kBodyField && is_strlike(b) && is_strlike(lv) && is_strlike(rv)) merge_body(result, name, b, lv, rv, policy);
        else merge_scalar(result, name, b, lv, rv, policy);
    }
    return result;
}

}  // namespace rp::merge
