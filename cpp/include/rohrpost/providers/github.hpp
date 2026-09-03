// GitHub provider: sync tickets <-> GitHub issues (spec §8.5; mirrors
// src/rohrpost/providers/github.py). The `gh` CLI is the preferred transport;
// the REST API (Bearer token from GITHUB_TOKEN / ROHRPOST_GITHUB_TOKEN) is the
// fallback when `gh` is absent or errors.
#pragma once

#include "rohrpost/providers/provider.hpp"
#include "rohrpost/util.hpp"

#include <functional>
#include <optional>
#include <string>
#include <vector>

namespace rp::providers {

class GitHubProvider : public Provider {
public:
    /// `gh_runner` returns gh's stdout for the given arguments (injectable for tests).
    using GhRunner = std::function<std::optional<std::string>(const std::vector<std::string>&)>;

    explicit GitHubProvider(Json config, EnvLookup env = nullptr, GhRunner gh_runner = nullptr,
                            std::optional<bool> prefer_gh = std::nullopt);

    [[nodiscard]] std::string remote() const override { return "github"; }
    [[nodiscard]] Json fetch(std::string_view ref) override;
    [[nodiscard]] Json push(std::string_view ref, const Json& fields) override;

    /// The flat `{field: value}` payload as repeated `gh api -f` arguments.
    [[nodiscard]] static std::vector<std::string> gh_field_args(const Json& payload);

private:
    Json config_;
    std::string repo_;
    std::string base_url_;
    Json fields_;
    EnvLookup env_;
    GhRunner gh_runner_;
    bool prefer_gh_;

    [[nodiscard]] std::optional<Json> try_gh(const std::vector<std::string>& args) const;
    [[nodiscard]] std::vector<std::pair<std::string, std::string>> headers() const;
    [[nodiscard]] std::string issue_path(std::string_view ref) const;
    [[nodiscard]] Json to_remote(const Json& local_fields) const;
    [[nodiscard]] Json to_local(const Json& issue) const;
    [[nodiscard]] Json scalar_map() const;
    [[nodiscard]] Json status_map() const;
};

}  // namespace rp::providers
