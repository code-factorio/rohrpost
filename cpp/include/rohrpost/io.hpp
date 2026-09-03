// Process I/O: UTF-8 stdout/stderr, binary stdin, whole-file reads and writes.
//
// The reference pins its streams to UTF-8 and, on Windows, writes text-mode
// newlines; this module reproduces that so the bytes agents see are the same
// on every platform.
#pragma once

#include <cstdint>
#include <expected>
#include <filesystem>
#include <string>
#include <string_view>

namespace rp::io {

/// One-time stream setup (binary stdin, UTF-8 console on Windows).
void init_streams();

void write_stdout(std::string_view text);
void write_stderr(std::string_view text);
/// Convenience: `print(*args)` — text plus a newline on stdout.
void println(std::string_view text = "");
void eprintln(std::string_view text = "");
void flush_stdout();

[[nodiscard]] bool stdout_is_tty();

/// Read all of stdin as bytes.
[[nodiscard]] std::string read_stdin();

/// Read a whole file as bytes. The error is the OS `strerror` text.
[[nodiscard]] std::expected<std::string, std::string> read_file(const std::filesystem::path& path);

/// Write bytes to a file (create/truncate). Throws std::runtime_error on failure.
void write_file(const std::filesystem::path& path, std::string_view data);

/// Append bytes to a file (create if missing). Throws std::runtime_error on failure.
void append_file(const std::filesystem::path& path, std::string_view data);

/// Write to `<path>.tmp`-style sibling then rename over `path` (atomic replace).
void write_file_atomic(const std::filesystem::path& path, const std::filesystem::path& tmp,
                       std::string_view data);

/// Environment variable lookup (UTF-8); nullopt when unset.
[[nodiscard]] std::optional<std::string> getenv(std::string_view name);

/// `st_mtime_ns`-like modification time, or nullopt if the path is not a file.
[[nodiscard]] std::optional<std::int64_t> mtime_ns(const std::filesystem::path& path);

/// Convert a filesystem path to the UTF-8 string `str(Path)` would give.
[[nodiscard]] std::string path_str(const std::filesystem::path& path);

/// Build a path from a UTF-8 string.
[[nodiscard]] std::filesystem::path path_from_utf8(std::string_view text);

}  // namespace rp::io
