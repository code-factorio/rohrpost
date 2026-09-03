// The `rp` command-line entry point (mirrors src/rohrpost/cli.py).
//
// Every mutation goes through the api module — this is only an argument
// adapter plus output rendering (spec §10). `--json` is honoured on every
// command; the default is readable text that respects NO_COLOR and non-tty
// streams. Exit codes: 0 success, 1 domain failure, 2 usage error.
#pragma once

#include <string>
#include <vector>

namespace rp::cli {

/// Run the `rp` CLI. Returns the process exit code.
int main(const std::vector<std::string>& argv);

}  // namespace rp::cli
