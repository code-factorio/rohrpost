#include "rohrpost/argparse.hpp"

#include "rohrpost/io.hpp"
#include "rohrpost/pyfmt.hpp"

#include <algorithm>
#include <format>
#include <regex>

namespace rp::argparse {
namespace {

constexpr int kMaxHelpPosition = 24;
constexpr int kIndent = 2;

/// `len(str)`: code points, not bytes (the description holds an em dash).
int text_len(std::string_view text) {
    int n = 0;
    for (const char c : text) {
        if ((static_cast<unsigned char>(c) & 0xc0u) != 0x80u) ++n;
    }
    return n;
}

std::string upper(std::string text) {
    for (char& c : text) {
        if (c >= 'a' && c <= 'z') c = static_cast<char>(c - 'a' + 'A');
    }
    return text;
}

std::string join(const std::vector<std::string>& parts, std::string_view sep) {
    std::string out;
    for (std::size_t i = 0; i < parts.size(); ++i) {
        if (i) out += sep;
        out += parts[i];
    }
    return out;
}

/// argparse's `_get_default_metavar_for_optional/positional` plus choices.
std::string metavar_of(const Argument& a) {
    if (!a.choices.empty()) return "{" + join(a.choices, ",") + "}";
    if (a.metavar) return *a.metavar;
    if (a.option_strings.empty()) return a.dest;
    return upper(a.dest);
}

/// `_format_args`: the metavar as it appears in usage.
std::string format_args(const Argument& a) {
    const std::string mv = metavar_of(a);
    switch (a.nargs) {
        case NArgs::One: return mv;
        case NArgs::Optional: return "[" + mv + "]";
        case NArgs::ZeroOrMore: return "[" + mv + " ...]";
        case NArgs::Parser: return mv + " ...";
    }
    return mv;
}

bool takes_value(const Argument& a) {
    return a.action == Action::Store || a.action == Action::Append;
}

/// `_format_action_invocation`.
std::string invocation_of(const Argument& a) {
    if (a.option_strings.empty()) return metavar_of(a);
    if (!takes_value(a)) return join(a.option_strings, ", ");
    return join(a.option_strings, ", ") + " " + format_args(a);
}

/// textwrap.wrap with argparse's whitespace collapsing. Words are split after
/// hyphens between letters (textwrap's hyphenated-word rule).
std::vector<std::string> wrap(std::string_view text, int width) {
    // Collapse whitespace runs and strip.
    std::string collapsed;
    bool in_space = false;
    for (const char c : text) {
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v') {
            in_space = true;
            continue;
        }
        if (in_space && !collapsed.empty()) collapsed.push_back(' ');
        in_space = false;
        collapsed.push_back(c);
    }
    // Chunks: words, further split after a hyphen that follows a letter and
    // precedes a word part containing a letter.
    std::vector<std::string> chunks;
    std::size_t start = 0;
    while (start < collapsed.size()) {
        auto end = collapsed.find(' ', start);
        if (end == std::string::npos) end = collapsed.size();
        std::string word = collapsed.substr(start, end - start);
        std::string current;
        for (std::size_t i = 0; i < word.size(); ++i) {
            current.push_back(word[i]);
            if (word[i] == '-' && i > 0 && i + 1 < word.size()) {
                const auto is_letter = [](char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_'; };
                const auto is_word = [&](char c) { return is_letter(c) || (c >= '0' && c <= '9'); };
                bool rest_has_letter = false;
                for (std::size_t j = i + 1; j < word.size() && is_word(word[j]); ++j) {
                    if (is_letter(word[j])) rest_has_letter = true;
                }
                if (is_letter(word[i - 1]) && is_word(word[i + 1]) && rest_has_letter) {
                    chunks.push_back(current);
                    current.clear();
                }
            }
        }
        if (!current.empty()) chunks.push_back(current);
        chunks.push_back(" ");
        start = end + 1;
    }
    if (!chunks.empty() && chunks.back() == " ") chunks.pop_back();
    // Greedy fill (textwrap.wrap): a chunk longer than the whole width is
    // broken to fill the remaining space, preferring a hyphen when present.
    std::vector<std::string> lines;
    std::string line;
    std::size_t i = 0;
    while (i < chunks.size()) {
        std::string chunk = chunks[i];
        if (line.empty() && chunk == " ") {
            ++i;
            continue;
        }
        if (text_len(line) + text_len(chunk) <= width) {
            line += chunk;
            ++i;
            continue;
        }
        if (text_len(chunk) > width) {
            const int space_left = width - text_len(line);
            if (space_left > 0) {
                int end = space_left;
                const auto hyphen = chunk.rfind('-', static_cast<std::size_t>(space_left) - 1);
                if (hyphen != std::string::npos && hyphen > 0 && static_cast<int>(hyphen) < space_left) {
                    bool non_hyphen_before = false;
                    for (std::size_t j = 0; j < hyphen; ++j) non_hyphen_before = non_hyphen_before || chunk[j] != '-';
                    if (non_hyphen_before) end = static_cast<int>(hyphen) + 1;
                }
                // Take `end` code points off the front of the chunk.
                std::size_t cut = 0;
                int taken = 0;
                while (cut < chunk.size() && taken < end) {
                    ++cut;
                    while (cut < chunk.size() && (static_cast<unsigned char>(chunk[cut]) & 0xc0u) == 0x80u) ++cut;
                    ++taken;
                }
                line += chunk.substr(0, cut);
                chunks[i] = chunk.substr(cut);
                if (chunks[i].empty()) ++i;
            }
        }
        while (!line.empty() && line.back() == ' ') line.pop_back();
        if (!line.empty()) lines.push_back(line);
        line.clear();
    }
    while (!line.empty() && line.back() == ' ') line.pop_back();
    if (!line.empty()) lines.push_back(line);
    return lines;
}

/// The regex that argparse builds for a positional/optional nargs pattern.
std::string nargs_pattern(const Argument& a) {
    const bool option = !a.option_strings.empty();
    switch (a.nargs) {
        case NArgs::One: return option ? "([A])" : "(-*A-*)";
        case NArgs::Optional: return option ? "(A?)" : "(-*A?-*)";
        case NArgs::ZeroOrMore: return option ? "(A*)" : "(-*[A-]*)";
        case NArgs::Parser: return option ? "(A[AO]*)" : "(-*A[-AO]*)";
    }
    return "([A])";
}


std::string arg_name(const Argument& a) {
    if (!a.option_strings.empty()) return join(a.option_strings, "/");
    return metavar_of(a);
}

const std::regex kNegativeNumber(R"(^-\d+$|^-\d*\.\d+$)");

}  // namespace

int help_width() {
    int columns = 80;
    if (const auto env = io::getenv("COLUMNS")) {
        if (const auto parsed = py::parse_int(*env); parsed && *parsed > 0) columns = static_cast<int>(*parsed);
    }
    return std::max(columns - 2, 11);
}

// --- Namespace ---------------------------------------------------------------

bool Namespace::has(const std::string& dest) const { return values_.contains(dest); }

std::optional<std::string> Namespace::get_str(const std::string& dest) const {
    const auto it = values_.find(dest);
    if (it == values_.end() || !it->is_string()) return std::nullopt;
    return it->get<std::string>();
}

std::optional<std::int64_t> Namespace::get_int(const std::string& dest) const {
    const auto it = values_.find(dest);
    if (it == values_.end() || !it->is_number_integer()) return std::nullopt;
    return it->get<std::int64_t>();
}

bool Namespace::get_bool(const std::string& dest) const {
    const auto it = values_.find(dest);
    return it != values_.end() && it->is_boolean() && it->get<bool>();
}

std::optional<std::vector<std::string>> Namespace::get_list(const std::string& dest) const {
    const auto it = values_.find(dest);
    if (it == values_.end() || !it->is_array()) return std::nullopt;
    std::vector<std::string> out;
    for (const auto& v : *it) out.push_back(v.get<std::string>());
    return out;
}

void Namespace::set(const std::string& dest, Json value) { values_[dest] = std::move(value); }

const Json& Namespace::raw(const std::string& dest) const {
    static const Json null;
    const auto it = values_.find(dest);
    return it == values_.end() ? null : *it;
}

// --- Parser ------------------------------------------------------------------

Parser::Parser(std::string prog, std::string description)
    : prog_(std::move(prog)), description_(std::move(description)) {
    Argument help;
    help.option_strings = {"-h", "--help"};
    help.dest = "help";
    help.action = Action::Help;
    help.help = "show this help message and exit";
    arguments_.push_back(std::move(help));
}

Argument& Parser::add_argument(Argument arg) {
    if (arg.dest.empty() && !arg.option_strings.empty()) {
        // dest from the first long option, else the short one.
        std::string source = arg.option_strings.front();
        for (const auto& s : arg.option_strings) {
            if (s.starts_with("--")) {
                source = s;
                break;
            }
        }
        std::string dest = source.substr(source.find_first_not_of('-'));
        std::replace(dest.begin(), dest.end(), '-', '_');
        arg.dest = dest;
    }
    if (arg.action == Action::Version && arg.help.empty()) arg.help = "show program's version number and exit";
    if (arg.option_strings.empty() && arg.nargs == NArgs::One) arg.required = true;
    arguments_.push_back(std::move(arg));
    return arguments_.back();
}

Parser& Parser::add_subparser(const std::string& name, const std::string& help, const std::string& dest,
                              const std::string& metavar) {
    if (subcommands_.empty()) {
        sub_dest_ = dest;
        sub_metavar_ = metavar;
        Argument sub;
        sub.dest = dest;
        sub.nargs = NArgs::Parser;
        sub.metavar = metavar;
        sub.required = false;
        arguments_.push_back(std::move(sub));
    }
    subcommands_.push_back(Subcommand{name, help, std::make_unique<Parser>(prog_ + " " + name)});
    return *subcommands_.back().parser;
}

std::vector<const Argument*> Parser::positionals() const {
    std::vector<const Argument*> out;
    for (const auto& a : arguments_) {
        if (a.option_strings.empty()) out.push_back(&a);
    }
    return out;
}

std::vector<const Argument*> Parser::optionals() const {
    std::vector<const Argument*> out;
    for (const auto& a : arguments_) {
        if (!a.option_strings.empty()) out.push_back(&a);
    }
    return out;
}

const Argument* Parser::find_option(std::string_view option) const {
    for (const auto& a : arguments_) {
        for (const auto& s : a.option_strings) {
            if (s == option) return &a;
        }
    }
    return nullptr;
}

ParseError Parser::error(const std::string& message) const {
    return ParseError{format_usage(), prog_, message};
}

std::string Parser::format_usage() const {
    const int text_width = std::max(help_width() - 0, 11);
    const std::string prefix = "usage: ";
    std::vector<std::string> opt_parts;
    for (const auto* a : optionals()) {
        std::string part = a->option_strings.front();
        if (takes_value(*a)) part += " " + format_args(*a);
        opt_parts.push_back("[" + part + "]");
    }
    std::vector<std::string> pos_parts;
    for (const auto* a : positionals()) pos_parts.push_back(format_args(*a));

    std::vector<std::string> all_parts;
    all_parts.push_back(prog_);
    all_parts.insert(all_parts.end(), opt_parts.begin(), opt_parts.end());
    all_parts.insert(all_parts.end(), pos_parts.begin(), pos_parts.end());
    std::string usage = join(all_parts, " ");

    if (static_cast<int>(prefix.size() + usage.size()) > text_width) {
        const auto get_lines = [&](const std::vector<std::string>& parts, const std::string& indent,
                                   const std::string* pfx) {
            std::vector<std::string> lines;
            std::vector<std::string> line;
            int line_len = pfx ? static_cast<int>(pfx->size()) - 1 : static_cast<int>(indent.size()) - 1;
            for (const auto& part : parts) {
                if (line_len + 1 + static_cast<int>(part.size()) > text_width && !line.empty()) {
                    lines.push_back(indent + join(line, " "));
                    line.clear();
                    line_len = static_cast<int>(indent.size()) - 1;
                }
                line.push_back(part);
                line_len += static_cast<int>(part.size()) + 1;
            }
            if (!line.empty()) lines.push_back(indent + join(line, " "));
            if (pfx) lines.front() = lines.front().substr(indent.size());
            return lines;
        };
        std::vector<std::string> lines;
        if (static_cast<double>(prefix.size() + prog_.size()) <= 0.75 * text_width) {
            const std::string indent(prefix.size() + prog_.size() + 1, ' ');
            if (!opt_parts.empty()) {
                std::vector<std::string> first{prog_};
                first.insert(first.end(), opt_parts.begin(), opt_parts.end());
                lines = get_lines(first, indent, &prefix);
                auto more = get_lines(pos_parts, indent, nullptr);
                lines.insert(lines.end(), more.begin(), more.end());
            } else if (!pos_parts.empty()) {
                std::vector<std::string> first{prog_};
                first.insert(first.end(), pos_parts.begin(), pos_parts.end());
                lines = get_lines(first, indent, &prefix);
            } else {
                lines = {prog_};
            }
        } else {
            const std::string indent(prefix.size(), ' ');
            std::vector<std::string> parts = opt_parts;
            parts.insert(parts.end(), pos_parts.begin(), pos_parts.end());
            lines = get_lines(parts, indent, nullptr);
            if (lines.size() > 1) {
                lines.clear();
                auto o = get_lines(opt_parts, indent, nullptr);
                auto p = get_lines(pos_parts, indent, nullptr);
                lines.insert(lines.end(), o.begin(), o.end());
                lines.insert(lines.end(), p.begin(), p.end());
            }
            lines.insert(lines.begin(), prog_);
        }
        usage = join(lines, "\n");
    }
    return prefix + usage + "\n";
}

std::string Parser::format_help() const {
    const int width = help_width();
    // action_max_length: invocation length + indent (subcommand entries at indent 4).
    int action_max_length = 0;
    for (const auto& a : arguments_) {
        action_max_length = std::max(action_max_length, static_cast<int>(invocation_of(a).size()) + kIndent);
        if (a.nargs == NArgs::Parser) {
            for (const auto& sub : subcommands_) {
                action_max_length = std::max(action_max_length, static_cast<int>(sub.name.size()) + kIndent * 2);
            }
        }
    }
    const int max_help_position = std::min(kMaxHelpPosition, std::max(width - 20, kIndent * 2));
    const int help_position = std::min(action_max_length + 2, max_help_position);
    const int help_width = std::max(width - help_position, 11);

    const auto format_item = [&](const std::string& invocation, const std::string& help, int indent) {
        std::string out;
        const int action_width = help_position - indent - 2;
        std::vector<std::string> help_lines;
        if (!help.empty()) help_lines = wrap(help, help_width);
        if (help.empty()) {
            out += std::string(static_cast<std::size_t>(indent), ' ') + invocation + "\n";
        } else if (static_cast<int>(invocation.size()) <= action_width) {
            out += std::string(static_cast<std::size_t>(indent), ' ');
            out += invocation;
            out += std::string(static_cast<std::size_t>(action_width - static_cast<int>(invocation.size())) + 2, ' ');
            out += help_lines.front() + "\n";
            for (std::size_t i = 1; i < help_lines.size(); ++i) {
                out += std::string(static_cast<std::size_t>(help_position), ' ') + help_lines[i] + "\n";
            }
        } else {
            out += std::string(static_cast<std::size_t>(indent), ' ') + invocation + "\n";
            for (const auto& line : help_lines) {
                out += std::string(static_cast<std::size_t>(help_position), ' ') + line + "\n";
            }
        }
        return out;
    };

    std::string out = format_usage();
    if (!description_.empty()) {
        // argparse fills the description to the full width (indent 0).
        out += "\n";
        for (const auto& line : wrap(description_, width)) out += line + "\n";
    }
    const auto pos = positionals();
    if (!pos.empty()) {
        out += "\npositional arguments:\n";
        for (const auto* a : pos) {
            out += format_item(invocation_of(*a), a->help, kIndent);
            if (a->nargs == NArgs::Parser) {
                for (const auto& sub : subcommands_) out += format_item(sub.name, sub.help, kIndent * 2);
            }
        }
    }
    out += "\noptions:\n";
    for (const auto* a : optionals()) out += format_item(invocation_of(*a), a->help, kIndent);
    return out;
}

struct Parser::ParseState {
    const Parser& parser;
    std::vector<std::string> args;
    std::string pattern;  // 'A' argument, 'O' option, '-' the first "--"
    Namespace ns;
    std::vector<std::string> extras;
    std::vector<const Argument*> seen;
};

Namespace Parser::parse_args(const std::vector<std::string>& args) const {
    std::vector<std::string> extras;
    Namespace ns = parse_known_args(args, extras);
    if (!extras.empty()) throw error("unrecognized arguments: " + join(extras, " "));
    return ns;
}

Namespace Parser::parse_known_args(const std::vector<std::string>& args, std::vector<std::string>& extras) const {
    // --- classify every argument string ------------------------------------
    struct Optional {
        const Argument* action;  // nullptr for an unknown option
        std::string option_string;
        std::optional<std::string> explicit_arg;
        std::string ambiguous;  // non-empty: "could match --a, --b"
    };
    std::vector<std::optional<Optional>> option_of(args.size());
    std::string pattern;
    bool seen_double_dash = false;
    for (std::size_t i = 0; i < args.size(); ++i) {
        const std::string& arg = args[i];
        if (seen_double_dash) {
            pattern.push_back('A');
            continue;
        }
        if (arg == "--") {
            seen_double_dash = true;
            pattern.push_back('-');
            continue;
        }
        // _parse_optional
        std::optional<Optional> found;
        if (arg.empty() || arg[0] != '-') {
            pattern.push_back('A');
            continue;
        }
        if (const Argument* exact = find_option(arg)) {
            found = Optional{exact, arg, std::nullopt, ""};
        } else if (arg.size() == 1) {
            pattern.push_back('A');
            continue;
        } else {
            const auto eq = arg.find('=');
            const Argument* with_eq = eq == std::string::npos ? nullptr : find_option(arg.substr(0, eq));
            if (with_eq != nullptr) {
                found = Optional{with_eq, arg.substr(0, eq), arg.substr(eq + 1), ""};
            } else {
                // _get_option_tuples: prefix matches
                std::vector<Optional> tuples;
                if (arg.size() > 1 && arg[1] == '-') {
                    const std::string option_prefix = eq == std::string::npos ? arg : arg.substr(0, eq);
                    const std::optional<std::string> explicit_arg = eq == std::string::npos ? std::nullopt : std::optional(arg.substr(eq + 1));
                    for (const auto& a : arguments_) {
                        for (const auto& s : a.option_strings) {
                            if (s.starts_with(option_prefix)) tuples.push_back(Optional{&a, s, explicit_arg, ""});
                        }
                    }
                } else {
                    const std::string short_prefix = arg.substr(0, 2);
                    const std::string short_explicit = arg.substr(2);
                    for (const auto& a : arguments_) {
                        for (const auto& s : a.option_strings) {
                            if (s == short_prefix) tuples.push_back(Optional{&a, s, short_explicit, ""});
                            else if (s.starts_with(arg)) tuples.push_back(Optional{&a, s, std::nullopt, ""});
                        }
                    }
                }
                if (tuples.size() > 1) {
                    std::vector<std::string> names;
                    for (const auto& t : tuples) names.push_back(t.option_string);
                    found = Optional{nullptr, arg, std::nullopt, std::format("ambiguous option: {} could match {}", arg.substr(0, eq == std::string::npos ? arg.size() : eq), join(names, ", "))};
                } else if (tuples.size() == 1) {
                    found = tuples.front();
                } else if (std::regex_match(arg, kNegativeNumber)) {
                    pattern.push_back('A');
                    continue;
                } else if (arg.find(' ') != std::string::npos) {
                    pattern.push_back('A');
                    continue;
                } else {
                    found = Optional{nullptr, arg, std::nullopt, ""};
                }
            }
        }
        option_of[i] = found;
        pattern.push_back('O');
    }

    Namespace ns;
    // Defaults.
    for (const auto& a : arguments_) {
        if (a.action == Action::Help || a.action == Action::Version) continue;
        if (a.action == Action::StoreTrue) ns.set(a.dest, false);
        else if (a.nargs == NArgs::ZeroOrMore && a.option_strings.empty()) ns.set(a.dest, Json::array());
        else ns.set(a.dest, Json());
    }
    std::vector<const Argument*> seen_actions;
    std::vector<const Argument*> pending = positionals();

    const auto convert = [&](const Argument& a, const std::string& value) -> Json {
        if (a.type == Type::Int) {
            const auto parsed = py::parse_int(value);
            if (!parsed) throw error(std::format("argument {}: invalid int value: {}", arg_name(a), py::repr(value)));
            return Json(*parsed);
        }
        if (!a.choices.empty() && std::find(a.choices.begin(), a.choices.end(), value) == a.choices.end()) {
            std::vector<std::string> quoted;
            for (const auto& c : a.choices) quoted.push_back(py::repr(c));
            throw error(std::format("argument {}: invalid choice: {} (choose from {})", arg_name(a), py::repr(value), join(quoted, ", ")));
        }
        return Json(value);
    };

    const auto take_action = [&](const Argument& a, const std::vector<std::string>& values) {
        seen_actions.push_back(&a);
        switch (a.action) {
            case Action::Help: throw ExitWithOutput{format_help()};
            case Action::Version: throw ExitWithOutput{a.version.value_or("") + "\n"};
            case Action::StoreTrue: ns.set(a.dest, true); break;
            case Action::Append: {
                Json list = ns.raw(a.dest).is_array() ? ns.raw(a.dest) : Json::array();
                list.push_back(convert(a, values.front()));
                ns.set(a.dest, list);
                break;
            }
            case Action::Store: {
                if (a.nargs == NArgs::One) ns.set(a.dest, convert(a, values.front()));
                else if (a.nargs == NArgs::Optional) {
                    if (!values.empty()) ns.set(a.dest, convert(a, values.front()));
                } else if (a.nargs == NArgs::ZeroOrMore) {
                    Json list = Json::array();
                    for (const auto& v : values) list.push_back(convert(a, v));
                    ns.set(a.dest, list);
                }
                break;
            }
        }
    };

    // --- consume positionals: _match_arguments_partial over the pattern -----
    const auto consume_positionals = [&](std::size_t start_index) -> std::size_t {
        const std::string selected = pattern.substr(start_index);
        std::vector<std::size_t> counts;
        for (std::size_t n = pending.size(); n > 0; --n) {
            std::string re;
            for (std::size_t i = 0; i < n; ++i) re += nargs_pattern(*pending[i]);
            std::smatch m;
            if (std::regex_search(selected, m, std::regex(re), std::regex_constants::match_continuous)) {
                for (std::size_t i = 1; i < m.size(); ++i) counts.push_back(static_cast<std::size_t>(m[i].length()));
                const auto end = static_cast<std::size_t>(m[0].length());
                if (end < selected.size() && selected[end] == 'O') {
                    while (!counts.empty() && counts.back() == 0) counts.pop_back();
                }
                break;
            }
        }
        std::size_t index = start_index;
        for (std::size_t i = 0; i < counts.size(); ++i) {
            const Argument& a = *pending[i];
            std::vector<std::string> values(args.begin() + static_cast<std::ptrdiff_t>(index),
                                            args.begin() + static_cast<std::ptrdiff_t>(index + counts[i]));
            if (a.nargs == NArgs::Parser) {
                if (pattern[index] == '-') values.erase(values.begin());
            } else if (pattern.find('-', index) != std::string::npos && pattern.find('-', index) < index + counts[i]) {
                values.erase(std::find(values.begin(), values.end(), "--"));
            }
            index += counts[i];
            if (a.nargs == NArgs::Parser) {
                // Subparser dispatch: the first value is the command name.
                const std::string& name = values.front();
                const auto it = std::find_if(subcommands_.begin(), subcommands_.end(), [&](const Subcommand& s) { return s.name == name; });
                if (it == subcommands_.end()) {
                    std::vector<std::string> quoted;
                    for (const auto& s : subcommands_) quoted.push_back(py::repr(s.name));
                    throw error(std::format("argument {}: invalid choice: {} (choose from {})", sub_metavar_, py::repr(name), join(quoted, ", ")));
                }
                seen_actions.push_back(&a);
                ns.set(sub_dest_, name);
                std::vector<std::string> rest(values.begin() + 1, values.end());
                std::vector<std::string> sub_extras;
                Namespace sub_ns = it->parser->parse_known_args(rest, sub_extras);
                // Merge the subnamespace into ours.
                for (const auto& sa : it->parser->arguments_) {
                    if (sa.action == Action::Help || sa.action == Action::Version) continue;
                    ns.set(sa.dest, sub_ns.raw(sa.dest));
                }
                extras.insert(extras.end(), sub_extras.begin(), sub_extras.end());
            } else {
                take_action(a, values);
            }
        }
        pending.erase(pending.begin(), pending.begin() + static_cast<std::ptrdiff_t>(counts.size()));
        return index;
    };

    // --- consume an optional -------------------------------------------------
    const auto consume_optional = [&](std::size_t start_index) -> std::size_t {
        const Optional& opt = *option_of[start_index];
        if (!opt.ambiguous.empty()) throw error(opt.ambiguous);
        if (opt.action == nullptr) {
            extras.push_back(args[start_index]);
            return start_index + 1;
        }
        const Argument& a = *opt.action;
        std::size_t stop = start_index + 1;
        std::vector<std::string> values;
        if (opt.explicit_arg) {
            if (!takes_value(a)) {
                // argparse would try to chain single-dash flags (`-hx`); rp has
                // no chainable flags, so every explicit value is an error.
                throw error(std::format("argument {}: ignored explicit argument {}", arg_name(a), py::repr(*opt.explicit_arg)));
            }
            values.push_back(*opt.explicit_arg);
        } else if (takes_value(a)) {
            // match_argument: exactly one 'A' must follow.
            if (stop >= pattern.size() || pattern[stop] != 'A') {
                throw error(std::format("argument {}: expected one argument", arg_name(a)));
            }
            values.push_back(args[stop]);
            ++stop;
        }
        take_action(a, values);
        return stop;
    };

    // --- the alternating loop ------------------------------------------------
    std::vector<std::size_t> option_indices;
    for (std::size_t i = 0; i < pattern.size(); ++i) {
        if (pattern[i] == 'O') option_indices.push_back(i);
    }
    std::size_t start_index = 0;
    const std::size_t max_option_index = option_indices.empty() ? 0 : option_indices.back();
    if (!option_indices.empty()) {
        while (start_index <= max_option_index) {
            std::size_t next_option = start_index;
            while (next_option <= max_option_index && pattern[next_option] != 'O') ++next_option;
            if (start_index != next_option) {
                const std::size_t positionals_end = consume_positionals(start_index);
                if (positionals_end > start_index) {
                    start_index = positionals_end;
                    continue;
                }
                start_index = positionals_end;
            }
            if (start_index < next_option) {
                for (std::size_t i = start_index; i < next_option; ++i) extras.push_back(args[i]);
                start_index = next_option;
            }
            start_index = consume_optional(start_index);
        }
    }
    const std::size_t stop_index = consume_positionals(start_index);
    for (std::size_t i = stop_index; i < args.size(); ++i) extras.push_back(args[i]);

    // Required arguments.
    std::vector<std::string> missing;
    for (const auto& a : arguments_) {
        if (!a.required) continue;
        if (std::find(seen_actions.begin(), seen_actions.end(), &a) == seen_actions.end()) missing.push_back(arg_name(a));
    }
    if (!missing.empty()) throw error("the following arguments are required: " + join(missing, ", "));
    return ns;
}

}  // namespace rp::argparse
