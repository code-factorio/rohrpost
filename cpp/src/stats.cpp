#include "rohrpost/stats.hpp"

#include "rohrpost/fold.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/pyfmt.hpp"
#include "rohrpost/store.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <vector>

#if !defined(_WIN32)
#include <unistd.h>
#endif

namespace rp::stats {
namespace {

constexpr int kPercentilePoints[] = {50, 90, 95, 99};

std::int64_t percentile(const std::vector<std::int64_t>& sorted, double point) {
    if (sorted.empty()) return 0;
    if (sorted.size() == 1) return sorted.front();
    const double rank = point / 100.0 * static_cast<double>(sorted.size() - 1);
    const auto lo = static_cast<std::size_t>(rank);
    const std::size_t hi = std::min(lo + 1, sorted.size() - 1);
    const double frac = rank - static_cast<double>(lo);
    return py::round_half_even(static_cast<double>(sorted[lo]) * (1 - frac) + static_cast<double>(sorted[hi]) * frac);
}

Json distribution(std::vector<std::int64_t> samples) {
    Json dist = Json::object();
    if (samples.empty()) {
        for (const auto* key : {"p50", "p90", "p95", "p99", "max", "count"}) dist[key] = 0;
        return dist;
    }
    std::sort(samples.begin(), samples.end());
    for (const int point : kPercentilePoints) dist["p" + std::to_string(point)] = percentile(samples, point);
    dist["max"] = samples.back();
    dist["count"] = samples.size();
    return dist;
}

double median_cold_fold_ms(const std::filesystem::path& dir, int runs) {
    std::vector<double> timings;
    for (int i = 0; i < std::max(1, runs); ++i) {
        const auto start = std::chrono::steady_clock::now();
        (void)fold(store::read_events(dir));
        const auto elapsed = std::chrono::steady_clock::now() - start;
        timings.push_back(std::chrono::duration<double, std::milli>(elapsed).count());
    }
    std::sort(timings.begin(), timings.end());
    const std::size_t n = timings.size();
    const double median = n % 2 == 1 ? timings[n / 2] : (timings[n / 2 - 1] + timings[n / 2]) / 2.0;
    return py::round_digits(median, 3);
}

}  // namespace

long pipe_buf(const std::filesystem::path& path) {
#if defined(_WIN32)
    (void)path;
    return 4096;  // no PC_PIPE_BUF on Windows; the heuristic the thresholds use
#else
    const long value = ::pathconf(path.c_str(), _PC_PIPE_BUF);
    return value < 0 ? 4096 : value;
#endif
}

Json compute_stats(const std::filesystem::path& dir, int fold_runs) {
    const std::vector<Event> events = store::read_events(dir);
    const long buf = pipe_buf(dir);
    std::vector<std::int64_t> line_bytes;
    std::vector<std::int64_t> body_bytes;
    std::int64_t over_pipe_buf = 0;
    std::int64_t set_events = 0;
    for (const auto& ev : events) {
        const auto line_len = static_cast<std::int64_t>(encode(ev).size()) + 1;  // +1 trailing newline
        line_bytes.push_back(line_len);
        if (line_len > buf) ++over_pipe_buf;
        if (ev.set && !ev.set->empty()) {
            const auto it = ev.set->find("body");
            if (it != ev.set->end() && it->is_string()) body_bytes.push_back(static_cast<std::int64_t>(it->get_ref<const std::string&>().size()));
        }
        if (ev.op == "create" || ev.op == "set") ++set_events;
    }
    const double lock_share_pct = set_events ? py::round_digits(100.0 * static_cast<double>(over_pipe_buf) / static_cast<double>(set_events), 2) : 0.0;
    Json line_dist = distribution(line_bytes);
    line_dist["over_pipe_buf"] = over_pipe_buf;
    line_dist["lock_share_pct"] = lock_share_pct;
    Json out = Json::object();
    out["tickets"] = fold(events).size();
    out["events"] = events.size();
    out["pipe_buf"] = buf;
    out["body_bytes"] = distribution(body_bytes);
    out["event_line_bytes"] = line_dist;
    out["fold_ms"] = median_cold_fold_ms(dir, fold_runs);
    return out;
}

}  // namespace rp::stats
