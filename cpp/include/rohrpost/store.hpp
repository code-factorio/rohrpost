// The append-only event log store: read, append and lock `log.jsonl`
// (mirrors src/rohrpost/store.py, spec §7).
//
// Every append happens inside an exclusive lock on `.rohrpost/.lock` and is
// one write of one line in append mode. Reads are lock-free and tolerate a
// torn tail. The lock primitive is the same the reference uses on each
// platform (flock on POSIX, the CRT byte-range lock on Windows), so a native
// `rp` and a Python `rp` exclude each other on the same repository.
#pragma once

#include "rohrpost/events.hpp"

#include <cstddef>
#include <filesystem>
#include <string>
#include <utility>
#include <vector>

namespace rp::store {

/// RAII exclusive lock on `.rohrpost/.lock`, held for the object's lifetime.
/// Callers must not nest two locks on the same directory.
class FileLock {
public:
    explicit FileLock(const std::filesystem::path& rohrpost_dir);
    ~FileLock();
    FileLock(const FileLock&) = delete;
    FileLock& operator=(const FileLock&) = delete;

private:
    int fd_ = -1;
    bool locked_ = false;
};

/// Append one event as a single JSONL line under the lock. Throws StoreError.
void append_event(const std::filesystem::path& rohrpost_dir, const Event& event);

/// Every event from archive then log, in file order. Throws StoreError on the
/// first malformed line.
[[nodiscard]] std::vector<Event> read_events(const std::filesystem::path& rohrpost_dir);

/// Like read_events but returns `(events, errors)` instead of throwing.
[[nodiscard]] std::pair<std::vector<Event>, std::vector<std::string>> read_events_lenient(
    const std::filesystem::path& rohrpost_dir);

/// Count of non-blank lines across archive + log.
[[nodiscard]] std::size_t event_count(const std::filesystem::path& rohrpost_dir);

}  // namespace rp::store
