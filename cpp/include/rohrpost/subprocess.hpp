// Child processes: run a command with captured output and a timeout.
//
// The reference shells out to `git` (actor email, compaction guard, text
// merge) and `gh` (GitHub transport). Arguments are passed as an argv vector,
// never through a shell.
#pragma once

#include <chrono>
#include <expected>
#include <optional>
#include <string>
#include <vector>

namespace rp::subprocess {

struct Completed {
    int returncode = 0;
    std::string stdout_bytes;
    std::string stderr_bytes;
};

enum class Failure {
    NotFound,  // the executable does not exist (FileNotFoundError)
    Timeout,   // the deadline passed (TimeoutExpired)
    Spawn,     // any other OS error
};

/// Run `argv[0]` (resolved through PATH) with the remaining arguments. stdin
/// is the null device; stdout and stderr are captured as raw bytes.
[[nodiscard]] std::expected<Completed, Failure> run(const std::vector<std::string>& argv,
                                                    std::chrono::milliseconds timeout);

/// `shutil.which`: the path of an executable on PATH, or nullopt.
[[nodiscard]] std::optional<std::string> which(const std::string& name);

}  // namespace rp::subprocess
