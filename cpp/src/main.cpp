// Process entry point: UTF-8 argv on every platform, then the CLI.
#include "rohrpost/cli.hpp"
#include "rohrpost/io.hpp"

#include <exception>
#include <string>
#include <vector>

#if defined(_WIN32)
#include <windows.h>
#include <shellapi.h>
#endif

namespace {

std::vector<std::string> collect_args(int argc, char** argv) {
    std::vector<std::string> args;
#if defined(_WIN32)
    (void)argv;
    int wargc = 0;
    wchar_t** wargv = CommandLineToArgvW(GetCommandLineW(), &wargc);
    if (wargv != nullptr) {
        for (int i = 1; i < wargc; ++i) {
            const int n = WideCharToMultiByte(CP_UTF8, 0, wargv[i], -1, nullptr, 0, nullptr, nullptr);
            std::string arg(static_cast<std::size_t>(n > 0 ? n - 1 : 0), '\0');
            if (n > 1) WideCharToMultiByte(CP_UTF8, 0, wargv[i], -1, arg.data(), n, nullptr, nullptr);
            args.push_back(std::move(arg));
        }
        LocalFree(wargv);
    } else {
        for (int i = 1; i < argc; ++i) args.emplace_back(argv[i]);
    }
#else
    for (int i = 1; i < argc; ++i) args.emplace_back(argv[i]);
#endif
    return args;
}

}  // namespace

int main(int argc, char** argv) {
    rp::io::init_streams();
    int code = 1;
    try {
        code = rp::cli::main(collect_args(argc, argv));
    } catch (const std::exception& exc) {
        rp::io::flush_stdout();
        rp::io::eprintln(std::string("rp: internal error: ") + exc.what());
        code = 1;
    }
    rp::io::flush_stdout();
    return code;
}
