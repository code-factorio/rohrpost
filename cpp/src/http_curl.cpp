#include "rohrpost/http.hpp"

#include <curl/curl.h>

#include <memory>
#include <mutex>

namespace rp::http {
namespace {

std::size_t collect(char* ptr, std::size_t size, std::size_t nmemb, void* userdata) {
    static_cast<std::string*>(userdata)->append(ptr, size * nmemb);
    return size * nmemb;
}

void ensure_global_init() {
    static std::once_flag once;
    std::call_once(once, [] { curl_global_init(CURL_GLOBAL_DEFAULT); });
}

}  // namespace

std::expected<Response, std::string> perform(const Request& request) {
    ensure_global_init();
    std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> curl(curl_easy_init(), &curl_easy_cleanup);
    if (!curl) return std::unexpected("curl_easy_init failed");

    Response response;
    curl_easy_setopt(curl.get(), CURLOPT_URL, request.url.c_str());
    curl_easy_setopt(curl.get(), CURLOPT_WRITEFUNCTION, collect);
    curl_easy_setopt(curl.get(), CURLOPT_WRITEDATA, &response.body);
    curl_easy_setopt(curl.get(), CURLOPT_TIMEOUT_MS, static_cast<long>(request.timeout.count()));
    curl_easy_setopt(curl.get(), CURLOPT_FOLLOWLOCATION, 0L);
    curl_easy_setopt(curl.get(), CURLOPT_USERAGENT, "rohrpost/" RP_VERSION);
    if (request.method != "GET") {
        curl_easy_setopt(curl.get(), CURLOPT_CUSTOMREQUEST, request.method.c_str());
        curl_easy_setopt(curl.get(), CURLOPT_POSTFIELDS, request.body.c_str());
        curl_easy_setopt(curl.get(), CURLOPT_POSTFIELDSIZE, static_cast<long>(request.body.size()));
    }
    struct curl_slist* headers = nullptr;
    for (const auto& [name, value] : request.headers) {
        headers = curl_slist_append(headers, (name + ": " + value).c_str());
    }
    if (headers != nullptr) curl_easy_setopt(curl.get(), CURLOPT_HTTPHEADER, headers);
    const CURLcode rc = curl_easy_perform(curl.get());
    if (headers != nullptr) curl_slist_free_all(headers);
    if (rc != CURLE_OK) return std::unexpected(curl_easy_strerror(rc));
    long status = 0;
    curl_easy_getinfo(curl.get(), CURLINFO_RESPONSE_CODE, &status);
    response.status = static_cast<int>(status);
    return response;
}

}  // namespace rp::http
