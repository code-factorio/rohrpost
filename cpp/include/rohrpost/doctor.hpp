// `rp doctor` — integrity and configuration checks (spec §10.1; mirrors
// src/rohrpost/doctor.py). Each check isolates its failures.
#pragma once

#include "rohrpost/json.hpp"

#include <filesystem>
#include <string>
#include <vector>

namespace rp::doctor {

/// One doctor result; `ok` is true for a passing check.
struct Finding {
    std::string check;
    bool ok;
    std::string detail;
    [[nodiscard]] Json to_mapping() const;
};

/// Run every check. Returns the findings in report order.
[[nodiscard]] std::vector<Finding> run_checks(const std::filesystem::path& rohrpost_dir);

/// Run all checks and print the report (text or JSON). Returns the exit code.
int run(const std::filesystem::path& rohrpost_dir, bool json_output);

}  // namespace rp::doctor
