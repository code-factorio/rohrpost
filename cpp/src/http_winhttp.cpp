#include "rohrpost/http.hpp"

#include <cstddef>
#include <expected>
#include <iterator>
#include <string>

#include <windows.h>
#include <winhttp.h>

namespace rp::http {
namespace {

std::wstring widen(const std::string& text) {
    if (text.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring out(static_cast<std::size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), n);
    return out;
}

struct HandleCloser {
    HINTERNET handle;
    ~HandleCloser() {
        if (handle != nullptr) WinHttpCloseHandle(handle);
    }
};

std::string last_error() {
    return "WinHTTP error " + std::to_string(GetLastError());
}

}  // namespace

std::expected<Response, std::string> perform(const Request& request) {
    URL_COMPONENTS parts{};
    parts.dwStructSize = sizeof parts;
    wchar_t host[256]{};
    wchar_t path[4096]{};
    parts.lpszHostName = host;
    parts.dwHostNameLength = static_cast<DWORD>(std::size(host));
    parts.lpszUrlPath = path;
    parts.dwUrlPathLength = static_cast<DWORD>(std::size(path));
    const std::wstring url = widen(request.url);
    if (!WinHttpCrackUrl(url.c_str(), static_cast<DWORD>(url.size()), 0, &parts)) return std::unexpected("invalid URL");

    HandleCloser session{WinHttpOpen(L"rohrpost/" RP_VERSION, WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0)};
    if (session.handle == nullptr) return std::unexpected(last_error());
    const auto ms = static_cast<int>(request.timeout.count());
    WinHttpSetTimeouts(session.handle, ms, ms, ms, ms);
    HandleCloser connection{WinHttpConnect(session.handle, host, parts.nPort, 0)};
    if (connection.handle == nullptr) return std::unexpected(last_error());
    const DWORD flags = parts.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0;
    HandleCloser req{WinHttpOpenRequest(connection.handle, widen(request.method).c_str(), path, nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, flags)};
    if (req.handle == nullptr) return std::unexpected(last_error());

    std::wstring header_block;
    for (const auto& [name, value] : request.headers) header_block += widen(name + ": " + value) + L"\r\n";
    const bool has_body = request.method != "GET" && !request.body.empty();
    if (!WinHttpSendRequest(req.handle, header_block.empty() ? WINHTTP_NO_ADDITIONAL_HEADERS : header_block.c_str(),
                            header_block.empty() ? 0 : static_cast<DWORD>(-1),
                            has_body ? const_cast<char*>(request.body.data()) : WINHTTP_NO_REQUEST_DATA,
                            has_body ? static_cast<DWORD>(request.body.size()) : 0,
                            has_body ? static_cast<DWORD>(request.body.size()) : 0, 0)) {
        return std::unexpected(last_error());
    }
    if (!WinHttpReceiveResponse(req.handle, nullptr)) return std::unexpected(last_error());

    Response response;
    DWORD status = 0;
    DWORD size = sizeof status;
    WinHttpQueryHeaders(req.handle, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &status, &size, WINHTTP_NO_HEADER_INDEX);
    response.status = static_cast<int>(status);
    for (;;) {
        DWORD available = 0;
        if (!WinHttpQueryDataAvailable(req.handle, &available) || available == 0) break;
        std::string chunk(available, '\0');
        DWORD read = 0;
        if (!WinHttpReadData(req.handle, chunk.data(), available, &read)) break;
        response.body.append(chunk.data(), read);
    }
    return response;
}

}  // namespace rp::http
