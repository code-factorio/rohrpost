#include "rohrpost/store.hpp"

#include "rohrpost/errors.hpp"
#include "rohrpost/io.hpp"
#include "rohrpost/paths.hpp"
#include "rohrpost/pyfmt.hpp"

#include <cerrno>
#include <cstring>
#include <format>

#if defined(_WIN32)
#include <fcntl.h>
#include <io.h>
#include <share.h>
#include <sys/locking.h>
#include <sys/stat.h>
#else
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace rp::store {
namespace {

namespace fs = std::filesystem;

#if defined(_WIN32)
constexpr long kLockBytes = 1;  // lock and unlock must name the identical range

int open_lock_file(const fs::path& path) {
    int fd = -1;
    // "a+": read/write, create if missing, never truncate; binary so the CRT
    // does not touch bytes.
    _wsopen_s(&fd, path.c_str(), _O_RDWR | _O_CREAT | _O_APPEND | _O_BINARY, _SH_DENYNO, _S_IREAD | _S_IWRITE);
    return fd;
}
#else
int open_lock_file(const fs::path& path) {
    return ::open(path.c_str(), O_RDWR | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
}
#endif

}  // namespace

FileLock::FileLock(const fs::path& rohrpost_dir) {
    const fs::path lock = paths::lock_path(rohrpost_dir);
    fd_ = open_lock_file(lock);
    if (fd_ < 0) {
        throw StoreError(std::format("cannot open {}: {}", io::path_str(lock), std::strerror(errno)));
    }
#if defined(_WIN32)
    // Byte range [0, 1) from the start of the file, exactly like msvcrt.locking
    // in the reference; LK_LOCK retries once a second for ~10 attempts.
    _lseek(fd_, 0, SEEK_SET);
    if (_locking(fd_, _LK_LOCK, kLockBytes) != 0) {
        const int err = errno;
        _close(fd_);
        fd_ = -1;
        throw StoreError(std::format(
            "could not lock {} within the ~10s wait budget (is another rp process holding it?): [Errno {}] {}",
            io::path_str(rohrpost_dir), err, std::strerror(err)));
    }
#else
    while (::flock(fd_, LOCK_EX) != 0) {
        if (errno == EINTR) continue;
        const int err = errno;
        ::close(fd_);
        fd_ = -1;
        throw StoreError(std::format("could not lock {}: {}", io::path_str(rohrpost_dir), std::strerror(err)));
    }
#endif
    locked_ = true;
}

FileLock::~FileLock() {
    if (fd_ < 0) return;
#if defined(_WIN32)
    if (locked_) {
        _lseek(fd_, 0, SEEK_SET);
        _locking(fd_, _LK_UNLCK, kLockBytes);
    }
    _close(fd_);
#else
    if (locked_) ::flock(fd_, LOCK_UN);
    ::close(fd_);
#endif
}

void append_event(const fs::path& rohrpost_dir, const Event& event) {
    const std::string line = encode(event) + "\n";
    FileLock lock(rohrpost_dir);
    const fs::path log = paths::log_path(rohrpost_dir);
#if defined(_WIN32)
    int fd = -1;
    _wsopen_s(&fd, log.c_str(), _O_WRONLY | _O_CREAT | _O_APPEND | _O_BINARY, _SH_DENYNO, _S_IREAD | _S_IWRITE);
    if (fd < 0) throw StoreError(std::format("cannot open {}: {}", io::path_str(log), std::strerror(errno)));
    const int written = _write(fd, line.data(), static_cast<unsigned>(line.size()));
    if (written < 0 || static_cast<std::size_t>(written) != line.size()) {
        const long long size = _filelengthi64(fd);
        const long long partial = written < 0 ? 0 : written;
        _chsize_s(fd, size - partial);
        _close(fd);
        throw StoreError(std::format(
            "short write to {}: wrote {} of {} bytes; appending the remainder would break single-write atomicity (§7)",
            io::path_str(log), partial, line.size()));
    }
    _close(fd);
#else
    const int fd = ::open(log.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0) throw StoreError(std::format("cannot open {}: {}", io::path_str(log), std::strerror(errno)));
    const ssize_t written = ::write(fd, line.data(), line.size());
    if (written < 0 || static_cast<std::size_t>(written) != line.size()) {
        // Roll back the partial bytes: a trailing half-line would otherwise fail
        // to decode on every future read (§3 principle 5).
        struct stat st{};
        const ssize_t partial = written < 0 ? 0 : written;
        if (::fstat(fd, &st) == 0) {
            const off_t truncated = st.st_size - static_cast<off_t>(partial);
            if (::ftruncate(fd, truncated) != 0) { /* best effort */ }
        }
        ::close(fd);
        throw StoreError(std::format(
            "short write to {}: wrote {} of {} bytes; appending the remainder would break single-write atomicity (§7)",
            io::path_str(log), partial, line.size()));
    }
    // os_sync: deliberately a no-op — git is the backup tier (see the reference).
    ::close(fd);
#endif
}

namespace {

/// Decode every line of every file (archive then log). Line numbers run
/// continuously across files, like the reference's chained iterator.
std::pair<std::vector<Event>, std::vector<std::string>> decode_all(const fs::path& rohrpost_dir) {
    std::vector<Event> events;
    std::vector<std::string> errors;
    std::size_t lineno = 0;
    std::vector<fs::path> files = paths::archive_files(rohrpost_dir);
    files.push_back(paths::log_path(rohrpost_dir));
    for (const auto& path : files) {
        std::error_code ec;
        if (!fs::is_regular_file(path, ec)) continue;
        auto content = io::read_file(path);
        if (!content) throw StoreError(std::format("cannot read {}: {}", io::path_str(path), content.error()));
        for (const auto raw : py::split_lines(*content)) {
            ++lineno;
            if (const auto bad = py::validate_utf8(raw)) {
                errors.push_back(std::format("line {}: {}", lineno, bad->message(raw)));
                continue;
            }
            const std::string_view stripped = py::strip(raw);
            if (stripped.empty()) continue;
            auto decoded = decode_line(stripped);
            if (decoded) {
                events.push_back(std::move(*decoded));
            } else {
                // `stripped[:80]!r` — the first 80 code points, Python-repr'd.
                std::size_t pos = 0;
                std::size_t count = 0;
                while (pos < stripped.size() && count < 80) {
                    if (!py::decode_utf8(stripped, pos)) ++pos;
                    ++count;
                }
                errors.push_back(std::format("line {}: {}: {}", lineno, decoded.error(), py::repr(stripped.substr(0, pos))));
            }
        }
    }
    return {std::move(events), std::move(errors)};
}

}  // namespace

std::vector<Event> read_events(const fs::path& rohrpost_dir) {
    auto [events, errors] = decode_all(rohrpost_dir);
    if (!errors.empty()) {
        throw StoreError(std::format("malformed event log ({} bad line(s)): {}", errors.size(), errors.front()));
    }
    return std::move(events);
}

std::pair<std::vector<Event>, std::vector<std::string>> read_events_lenient(const fs::path& rohrpost_dir) {
    return decode_all(rohrpost_dir);
}

std::size_t event_count(const fs::path& rohrpost_dir) {
    std::size_t count = 0;
    std::vector<fs::path> files = paths::archive_files(rohrpost_dir);
    files.push_back(paths::log_path(rohrpost_dir));
    for (const auto& path : files) {
        std::error_code ec;
        if (!fs::is_regular_file(path, ec)) continue;
        auto content = io::read_file(path);
        if (!content) continue;
        for (const auto raw : py::split_lines(*content)) {
            if (!py::strip(raw).empty()) ++count;
        }
    }
    return count;
}

}  // namespace rp::store
