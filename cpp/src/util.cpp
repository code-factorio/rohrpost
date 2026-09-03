#include "rohrpost/util.hpp"

#include "rohrpost/io.hpp"
#include "rohrpost/pyfmt.hpp"
#include "rohrpost/subprocess.hpp"

#include <chrono>

#if !defined(_WIN32)
#include <optional>
#include <pwd.h>
#include <string>
#include <string_view>
#include <unistd.h>
#endif

namespace rp {
namespace {

/// Best-effort `git config user.email`. Cached; nullopt if git is missing or unset.
std::optional<std::string> git_email() {
    static const std::optional<std::string> cached = [] () -> std::optional<std::string> {
        auto result = subprocess::run({"git", "config", "user.email"}, std::chrono::seconds(5));
        if (!result) return std::nullopt;
        std::string email(py::strip(result->stdout_bytes));
        if (email.empty()) return std::nullopt;
        return email;
    }();
    return cached;
}

/// `getpass.getuser()`: the login environment variables, then the account database.
std::optional<std::string> login_name() {
    for (const auto* var : {"LOGNAME", "USER", "LNAME", "USERNAME"}) {
        const auto value = io::getenv(var);
        if (value && !value->empty()) return value;
    }
#if !defined(_WIN32)
    if (const passwd* pw = ::getpwuid(::getuid()); pw != nullptr && pw->pw_name != nullptr) {
        return std::string(pw->pw_name);
    }
#endif
    return std::nullopt;
}

}  // namespace

std::string resolve_actor(std::optional<std::string> explicit_actor, const EnvLookup& env) {
    if (explicit_actor && !explicit_actor->empty()) return *explicit_actor;
    const EnvLookup lookup = env ? env : EnvLookup([](std::string_view name) { return io::getenv(name); });

    if (const auto actor = lookup("ROHRPOST_ACTOR"); actor && !actor->empty()) return *actor;
    if (const auto runner = lookup("ROHRPOST_RUNNER"); runner && !runner->empty()) {
        const auto batch = lookup("ROHRPOST_BATCH");
        if (batch && !batch->empty()) return "runner/" + *runner + "@" + *batch;
        return "runner/" + *runner;
    }
    if (const auto email = git_email()) return "user/" + *email;
    if (const auto login = login_name()) return "user/" + *login;
    return "user/unknown";
}

}  // namespace rp
