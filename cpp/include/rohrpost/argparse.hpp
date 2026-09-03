// A faithful subset of Python's argparse, as used by the reference CLI.
//
// Agents read `rp <command> --help` and parse `rp: error: ...` lines, so the
// usage layout, help wrapping, option abbreviation, positional consumption
// and error wording follow CPython 3.14's argparse for the argument shapes
// the `rp` parser declares (store / store_true / append / version / help,
// nargs None / `?` / `*`, one level of subparsers).
#pragma once

#include "rohrpost/json.hpp"

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace rp::argparse {

enum class Action { Store, StoreTrue, Append, Version, Help };
enum class Type { String, Int };
enum class NArgs { One, Optional, ZeroOrMore, Parser };

struct Argument {
    std::vector<std::string> option_strings;  // empty for positionals
    std::string dest;
    Action action = Action::Store;
    Type type = Type::String;
    NArgs nargs = NArgs::One;
    std::optional<std::string> metavar;
    std::string help;
    std::vector<std::string> choices;
    bool required = false;
    std::optional<std::string> version;  // for Action::Version
};

/// Raised for `-h`/`--version`: the text to print on stdout, then exit 0.
struct ExitWithOutput {
    std::string text;
};

/// A usage error: usage text + `prog: error: message`, exit 2.
struct ParseError {
    std::string usage;
    std::string prog;
    std::string message;
};

/// The parsed values: dest -> value (null when unset, bool, int, string, list).
class Namespace {
public:
    [[nodiscard]] bool has(const std::string& dest) const;
    [[nodiscard]] std::optional<std::string> get_str(const std::string& dest) const;
    [[nodiscard]] std::optional<std::int64_t> get_int(const std::string& dest) const;
    [[nodiscard]] bool get_bool(const std::string& dest) const;
    [[nodiscard]] std::optional<std::vector<std::string>> get_list(const std::string& dest) const;
    void set(const std::string& dest, Json value);
    [[nodiscard]] const Json& raw(const std::string& dest) const;

private:
    Json values_ = Json::object();
};

class Parser {
public:
    Parser(std::string prog, std::string description = "");

    /// Register an argument. Positionals have no option strings.
    Argument& add_argument(Argument arg);
    /// Register a subcommand parser; the root gains a `<metavar>` positional.
    Parser& add_subparser(const std::string& name, const std::string& help, const std::string& dest = "command",
                          const std::string& metavar = "<command>");

    /// Parse, throwing ExitWithOutput or ParseError.
    [[nodiscard]] Namespace parse_args(const std::vector<std::string>& args) const;

    [[nodiscard]] std::string format_usage() const;
    [[nodiscard]] std::string format_help() const;
    [[nodiscard]] const std::string& prog() const { return prog_; }

private:
    struct Subcommand {
        std::string name;
        std::string help;
        std::unique_ptr<Parser> parser;
    };
    std::string prog_;
    std::string description_;
    std::vector<Argument> arguments_;  // in declaration order
    std::vector<Subcommand> subcommands_;
    std::string sub_dest_;
    std::string sub_metavar_;

    struct ParseState;
    Namespace parse_known_args(const std::vector<std::string>& args, std::vector<std::string>& extras) const;
    [[nodiscard]] std::vector<const Argument*> positionals() const;
    [[nodiscard]] std::vector<const Argument*> optionals() const;
    [[nodiscard]] const Argument* find_option(std::string_view option) const;
    [[nodiscard]] ParseError error(const std::string& message) const;
    friend struct ParserAccess;
};

/// The terminal width argparse formats to (`COLUMNS` or 80) minus two.
[[nodiscard]] int help_width();

}  // namespace rp::argparse
