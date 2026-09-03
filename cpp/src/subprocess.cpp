#include "rohrpost/subprocess.hpp"

#include "rohrpost/io.hpp"

#include <filesystem>

#if defined(_WIN32)
#include <thread>
#include <windows.h>
#else
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstddef>
#include <expected>
#include <fcntl.h>
#include <iterator>
#include <optional>
#include <poll.h>
#include <spawn.h>
#include <string>
#include <sys/wait.h>
#include <system_error>
#include <unistd.h>
#include <vector>
extern char** environ;
#endif

namespace rp::subprocess {
namespace fs = std::filesystem;

#if defined(_WIN32)
namespace {

std::wstring widen(const std::string& text) {
    if (text.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring out(static_cast<std::size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), n);
    return out;
}

/// Quote one argument the way the MSVCRT command-line parser expects
/// (the algorithm Python's subprocess.list2cmdline implements).
void quote_arg(std::wstring& out, const std::wstring& arg) {
    if (!arg.empty() && arg.find_first_of(L" \t\"") == std::wstring::npos) {
        out += arg;
        return;
    }
    out.push_back(L'"');
    std::size_t backslashes = 0;
    for (const wchar_t c : arg) {
        if (c == L'\\') {
            ++backslashes;
            continue;
        }
        if (c == L'"') {
            out.append(backslashes * 2 + 1, L'\\');
            out.push_back(L'"');
            backslashes = 0;
            continue;
        }
        out.append(backslashes, L'\\');
        backslashes = 0;
        out.push_back(c);
    }
    out.append(backslashes * 2, L'\\');
    out.push_back(L'"');
}

void drain(HANDLE pipe, std::string& out) {
    char buf[65536];
    for (;;) {
        DWORD n = 0;
        if (!ReadFile(pipe, buf, sizeof buf, &n, nullptr) || n == 0) break;
        out.append(buf, n);
    }
}

}  // namespace

std::expected<Completed, Failure> run(const std::vector<std::string>& argv, std::chrono::milliseconds timeout) {
    if (argv.empty()) return std::unexpected(Failure::Spawn);
    if (!which(argv.front())) return std::unexpected(Failure::NotFound);

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof sa;
    sa.bInheritHandle = TRUE;
    HANDLE out_r = nullptr, out_w = nullptr, err_r = nullptr, err_w = nullptr;
    if (!CreatePipe(&out_r, &out_w, &sa, 0)) return std::unexpected(Failure::Spawn);
    if (!CreatePipe(&err_r, &err_w, &sa, 0)) {
        CloseHandle(out_r);
        CloseHandle(out_w);
        return std::unexpected(Failure::Spawn);
    }
    SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(err_r, HANDLE_FLAG_INHERIT, 0);
    HANDLE null_in = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, nullptr);

    std::wstring cmdline;
    for (std::size_t i = 0; i < argv.size(); ++i) {
        if (i > 0) cmdline.push_back(L' ');
        quote_arg(cmdline, widen(argv[i]));
    }

    STARTUPINFOW si{};
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = null_in;
    si.hStdOutput = out_w;
    si.hStdError = err_w;
    PROCESS_INFORMATION pi{};
    const BOOL ok = CreateProcessW(nullptr, cmdline.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);
    CloseHandle(out_w);
    CloseHandle(err_w);
    if (null_in != INVALID_HANDLE_VALUE) CloseHandle(null_in);
    if (!ok) {
        const DWORD err = GetLastError();
        CloseHandle(out_r);
        CloseHandle(err_r);
        return std::unexpected(err == ERROR_FILE_NOT_FOUND ? Failure::NotFound : Failure::Spawn);
    }

    Completed result;
    std::thread out_thread([&] { drain(out_r, result.stdout_bytes); });
    std::thread err_thread([&] { drain(err_r, result.stderr_bytes); });
    const DWORD wait = WaitForSingleObject(pi.hProcess, static_cast<DWORD>(timeout.count()));
    bool timed_out = false;
    if (wait == WAIT_TIMEOUT) {
        timed_out = true;
        TerminateProcess(pi.hProcess, 1);
        WaitForSingleObject(pi.hProcess, INFINITE);
    }
    out_thread.join();
    err_thread.join();
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    CloseHandle(out_r);
    CloseHandle(err_r);
    if (timed_out) return std::unexpected(Failure::Timeout);
    result.returncode = static_cast<int>(code);
    return result;
}

std::optional<std::string> which(const std::string& name) {
    const std::wstring wname = widen(name);
    std::vector<std::wstring> exts;
    if (name.find('.') != std::string::npos) exts.push_back(L"");
    const auto pathext = io::getenv("PATHEXT").value_or(".COM;.EXE;.BAT;.CMD");
    std::size_t start = 0;
    while (start <= pathext.size()) {
        const auto end = pathext.find(';', start);
        const std::string ext = pathext.substr(start, end == std::string::npos ? std::string::npos : end - start);
        if (!ext.empty()) exts.push_back(widen(ext));
        if (end == std::string::npos) break;
        start = end + 1;
    }
    for (const auto& ext : exts) {
        wchar_t buf[MAX_PATH * 4];
        const DWORD n = SearchPathW(nullptr, wname.c_str(), ext.empty() ? nullptr : ext.c_str(), static_cast<DWORD>(std::size(buf)), buf, nullptr);
        if (n > 0 && n < std::size(buf)) return io::path_str(fs::path(std::wstring(buf, n)));
    }
    return std::nullopt;
}

#else  // POSIX

namespace {

bool is_executable_file(const fs::path& p) {
    std::error_code ec;
    return fs::is_regular_file(p, ec) && ::access(p.c_str(), X_OK) == 0;
}

}  // namespace

std::optional<std::string> which(const std::string& name) {
    if (name.find('/') != std::string::npos) {
        return is_executable_file(name) ? std::optional<std::string>(name) : std::nullopt;
    }
    const std::string path = io::getenv("PATH").value_or("/usr/local/bin:/usr/bin:/bin");
    std::size_t start = 0;
    while (start <= path.size()) {
        const auto end = path.find(':', start);
        const std::string dir = path.substr(start, end == std::string::npos ? std::string::npos : end - start);
        if (!dir.empty()) {
            const fs::path candidate = fs::path(dir) / name;
            if (is_executable_file(candidate)) return candidate.string();
        }
        if (end == std::string::npos) break;
        start = end + 1;
    }
    return std::nullopt;
}

std::expected<Completed, Failure> run(const std::vector<std::string>& argv, std::chrono::milliseconds timeout) {
    if (argv.empty()) return std::unexpected(Failure::Spawn);
    const auto exe = which(argv.front());
    if (!exe) return std::unexpected(Failure::NotFound);

    int out_pipe[2];
    int err_pipe[2];
    if (::pipe(out_pipe) != 0) return std::unexpected(Failure::Spawn);
    if (::pipe(err_pipe) != 0) {
        ::close(out_pipe[0]);
        ::close(out_pipe[1]);
        return std::unexpected(Failure::Spawn);
    }
    for (const int fd : {out_pipe[0], out_pipe[1], err_pipe[0], err_pipe[1]}) ::fcntl(fd, F_SETFD, FD_CLOEXEC);

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0);
    posix_spawn_file_actions_adddup2(&actions, out_pipe[1], 1);
    posix_spawn_file_actions_adddup2(&actions, err_pipe[1], 2);

    std::vector<std::string> args = argv;
    std::vector<char*> cargv;
    cargv.reserve(args.size() + 1);
    for (auto& a : args) cargv.push_back(a.data());
    cargv.push_back(nullptr);

    pid_t pid = 0;
    const int rc = ::posix_spawn(&pid, exe->c_str(), &actions, nullptr, cargv.data(), environ);
    posix_spawn_file_actions_destroy(&actions);
    ::close(out_pipe[1]);
    ::close(err_pipe[1]);
    if (rc != 0) {
        ::close(out_pipe[0]);
        ::close(err_pipe[0]);
        return std::unexpected(rc == ENOENT ? Failure::NotFound : Failure::Spawn);
    }

    Completed result;
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    bool out_open = true;
    bool err_open = true;
    bool timed_out = false;
    char buf[65536];
    while (out_open || err_open) {
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) {
            timed_out = true;
            break;
        }
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now).count();
        struct pollfd fds[2];
        int n = 0;
        if (out_open) fds[n++] = {out_pipe[0], POLLIN, 0};
        if (err_open) fds[n++] = {err_pipe[0], POLLIN, 0};
        const int ready = ::poll(fds, static_cast<nfds_t>(n), static_cast<int>(remaining));
        if (ready < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (ready == 0) continue;
        for (int i = 0; i < n; ++i) {
            if (fds[i].revents == 0) continue;
            const ssize_t got = ::read(fds[i].fd, buf, sizeof buf);
            std::string& target = fds[i].fd == out_pipe[0] ? result.stdout_bytes : result.stderr_bytes;
            if (got > 0) {
                target.append(buf, static_cast<std::size_t>(got));
            } else {
                if (fds[i].fd == out_pipe[0]) out_open = false;
                else err_open = false;
            }
        }
    }
    ::close(out_pipe[0]);
    ::close(err_pipe[0]);
    if (timed_out) {
        ::kill(pid, SIGKILL);
        int status = 0;
        ::waitpid(pid, &status, 0);
        return std::unexpected(Failure::Timeout);
    }
    int status = 0;
    while (::waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return std::unexpected(Failure::Spawn);
    }
    if (WIFEXITED(status)) result.returncode = WEXITSTATUS(status);
    else if (WIFSIGNALED(status)) result.returncode = -WTERMSIG(status);
    return result;
}
#endif

}  // namespace rp::subprocess
