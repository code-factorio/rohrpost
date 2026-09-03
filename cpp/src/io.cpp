#include "rohrpost/io.hpp"

#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <expected>
#include <filesystem>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>

#if defined(_WIN32)
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#else
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace rp::io {
namespace {

std::string g_stdout_buffer;
constexpr std::size_t kFlushThreshold = 64 * 1024;

#if defined(_WIN32)
std::wstring to_wide(std::string_view text) {
    if (text.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring out(static_cast<std::size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), n);
    return out;
}

std::string from_wide(std::wstring_view text) {
    if (text.empty()) return {};
    const int n = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<std::size_t>(n), '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), n, nullptr, nullptr);
    return out;
}

/// Write UTF-8 text to a Windows handle: UTF-16 to a console, else bytes with
/// the text-mode `\n` -> `\r\n` translation Python applies to piped output.
void write_handle(HANDLE handle, std::string_view text) {
    DWORD mode = 0;
    if (GetConsoleMode(handle, &mode)) {
        const std::wstring wide = to_wide(text);
        std::size_t done = 0;
        while (done < wide.size()) {
            DWORD written = 0;
            if (!WriteConsoleW(handle, wide.data() + done, static_cast<DWORD>(wide.size() - done), &written, nullptr)) break;
            done += written;
        }
        return;
    }
    std::string translated;
    translated.reserve(text.size() + 16);
    for (const char c : text) {
        if (c == '\n') translated.push_back('\r');
        translated.push_back(c);
    }
    std::size_t done = 0;
    while (done < translated.size()) {
        DWORD written = 0;
        if (!WriteFile(handle, translated.data() + done, static_cast<DWORD>(translated.size() - done), &written, nullptr)) break;
        done += written;
    }
}
#else
void write_fd(int fd, std::string_view text) {
    std::size_t done = 0;
    while (done < text.size()) {
        const ssize_t n = ::write(fd, text.data() + done, text.size() - done);
        if (n < 0) {
            if (errno == EINTR) continue;
            break;
        }
        done += static_cast<std::size_t>(n);
    }
}
#endif

}  // namespace

void init_streams() {
#if defined(_WIN32)
    _setmode(_fileno(stdin), _O_BINARY);
#endif
}

void flush_stdout() {
    if (g_stdout_buffer.empty()) return;
#if defined(_WIN32)
    write_handle(GetStdHandle(STD_OUTPUT_HANDLE), g_stdout_buffer);
#else
    write_fd(1, g_stdout_buffer);
#endif
    g_stdout_buffer.clear();
}

void write_stdout(std::string_view text) {
    g_stdout_buffer.append(text);
    if (g_stdout_buffer.size() >= kFlushThreshold || (stdout_is_tty() && text.find('\n') != std::string_view::npos)) {
        flush_stdout();
    }
}

void write_stderr(std::string_view text) {
#if defined(_WIN32)
    write_handle(GetStdHandle(STD_ERROR_HANDLE), text);
#else
    write_fd(2, text);
#endif
}

void println(std::string_view text) {
    std::string line(text);
    line.push_back('\n');
    write_stdout(line);
}

void eprintln(std::string_view text) {
    std::string line(text);
    line.push_back('\n');
    write_stderr(line);
}

bool stdout_is_tty() {
#if defined(_WIN32)
    return _isatty(_fileno(stdout)) != 0;
#else
    return isatty(1) != 0;
#endif
}

std::string read_stdin() {
    std::string out;
    char buf[65536];
    for (;;) {
        const std::size_t n = std::fread(buf, 1, sizeof buf, stdin);
        if (n > 0) out.append(buf, n);
        if (n < sizeof buf) break;
    }
    return out;
}

std::expected<std::string, std::string> read_file(const std::filesystem::path& path) {
    std::error_code ec;
    if (std::filesystem::is_directory(path, ec)) {
#if defined(_WIN32)
        return std::unexpected(std::string("Permission denied"));
#else
        return std::unexpected(std::string(std::strerror(EISDIR)));
#endif
    }
#if defined(_WIN32)
    FILE* fh = _wfopen(path.c_str(), L"rb");
#else
    FILE* fh = std::fopen(path.c_str(), "rb");
#endif
    if (fh == nullptr) return std::unexpected(std::string(std::strerror(errno)));
    std::string out;
    char buf[65536];
    for (;;) {
        const std::size_t n = std::fread(buf, 1, sizeof buf, fh);
        if (n > 0) out.append(buf, n);
        if (n < sizeof buf) {
            if (std::ferror(fh)) {
                const int err = errno;
                std::fclose(fh);
                return std::unexpected(std::string(std::strerror(err)));
            }
            break;
        }
    }
    std::fclose(fh);
    return out;
}

namespace {
void write_mode(const std::filesystem::path& path, std::string_view data, const char* mode) {
#if defined(_WIN32)
    const std::wstring wmode = to_wide(mode);
    FILE* fh = _wfopen(path.c_str(), wmode.c_str());
#else
    FILE* fh = std::fopen(path.c_str(), mode);
#endif
    if (fh == nullptr) {
        throw std::runtime_error(std::string("cannot open ") + path_str(path) + ": " + std::strerror(errno));
    }
    if (!data.empty() && std::fwrite(data.data(), 1, data.size(), fh) != data.size()) {
        const int err = errno;
        std::fclose(fh);
        throw std::runtime_error(std::string("cannot write ") + path_str(path) + ": " + std::strerror(err));
    }
    std::fclose(fh);
}
}  // namespace

void write_file(const std::filesystem::path& path, std::string_view data) {
    write_mode(path, data, "wb");
}

void append_file(const std::filesystem::path& path, std::string_view data) {
    write_mode(path, data, "ab");
}

void write_file_atomic(const std::filesystem::path& path, const std::filesystem::path& tmp,
                       std::string_view data) {
    write_file(tmp, data);
    std::error_code ec;
    std::filesystem::rename(tmp, path, ec);
    if (ec) {
        std::filesystem::remove(tmp, ec);
        throw std::runtime_error("cannot replace " + path_str(path) + ": " + ec.message());
    }
}

std::optional<std::string> getenv(std::string_view name) {
#if defined(_WIN32)
    const std::wstring wname = to_wide(name);
    const DWORD n = GetEnvironmentVariableW(wname.c_str(), nullptr, 0);
    if (n == 0) return std::nullopt;
    std::wstring value(static_cast<std::size_t>(n), L'\0');
    const DWORD got = GetEnvironmentVariableW(wname.c_str(), value.data(), n);
    value.resize(got);
    return from_wide(value);
#else
    const std::string key(name);
    const char* value = std::getenv(key.c_str());
    if (value == nullptr) return std::nullopt;
    return std::string(value);
#endif
}

std::optional<std::int64_t> mtime_ns(const std::filesystem::path& path) {
#if defined(_WIN32)
    WIN32_FILE_ATTRIBUTE_DATA data{};
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &data)) return std::nullopt;
    if (data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) return std::nullopt;
    // FILETIME is 100 ns ticks since 1601-01-01; Python reports st_mtime_ns relative to 1970.
    const std::uint64_t ticks = (static_cast<std::uint64_t>(data.ftLastWriteTime.dwHighDateTime) << 32) | data.ftLastWriteTime.dwLowDateTime;
    const std::int64_t epoch_ticks = static_cast<std::int64_t>(ticks) - 116444736000000000LL;
    return epoch_ticks * 100;
#else
    struct stat st{};
    if (::stat(path.c_str(), &st) != 0) return std::nullopt;
    if (!S_ISREG(st.st_mode)) return std::nullopt;
#if defined(__APPLE__)
    return static_cast<std::int64_t>(st.st_mtimespec.tv_sec) * 1'000'000'000LL + st.st_mtimespec.tv_nsec;
#else
    return static_cast<std::int64_t>(st.st_mtim.tv_sec) * 1'000'000'000LL + st.st_mtim.tv_nsec;
#endif
#endif
}

std::string path_str(const std::filesystem::path& path) {
#if defined(_WIN32)
    return from_wide(path.native());
#else
    return path.native();
#endif
}

std::filesystem::path path_from_utf8(std::string_view text) {
#if defined(_WIN32)
    return std::filesystem::path(to_wide(text));
#else
    return std::filesystem::path(std::string(text));
#endif
}

}  // namespace rp::io
