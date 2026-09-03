// A minimal HTTPS client for the GitHub REST fallback and `rp doctor`'s
// credential probe. libcurl on POSIX, WinHTTP on Windows; either backend
// implements this one function.
#pragma once

#include <chrono>
#include <expected>
#include <string>
#include <utility>
#include <vector>

namespace rp::http {

struct Response {
    int status = 0;
    std::string body;
};

struct Request {
    std::string method = "GET";
    std::string url;
    std::vector<std::pair<std::string, std::string>> headers;
    std::string body;  // sent for PATCH/POST
    std::chrono::milliseconds timeout{30000};
};

/// Perform one request. The error is a transport-level description.
[[nodiscard]] std::expected<Response, std::string> perform(const Request& request);

}  // namespace rp::http
