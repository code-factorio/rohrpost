#include "rohrpost/json.hpp"

#include "rohrpost/pyfmt.hpp"

#include <algorithm>
#include <format>
#include <vector>

namespace rp::json {
namespace {

void escape_string(std::string& out, std::string_view text, bool ensure_ascii) {
    out.push_back('"');
    std::size_t pos = 0;
    while (pos < text.size()) {
        const std::size_t seq_start = pos;
        const auto decoded = py::decode_utf8(text, pos);
        if (!decoded) {
            // Invalid UTF-8 cannot come from our own writers; pass the byte through.
            out.push_back(text[seq_start]);
            pos = seq_start + 1;
            continue;
        }
        const char32_t cp = *decoded;
        switch (cp) {
            case '"': out += "\\\""; continue;
            case '\\': out += "\\\\"; continue;
            case '\n': out += "\\n"; continue;
            case '\r': out += "\\r"; continue;
            case '\t': out += "\\t"; continue;
            case '\b': out += "\\b"; continue;
            case '\f': out += "\\f"; continue;
            default: break;
        }
        if (cp < 0x20) {
            out += std::format("\\u{:04x}", static_cast<unsigned>(cp));
        } else if (cp < 0x7f || (cp == 0x7f && !ensure_ascii)) {
            out.push_back(static_cast<char>(cp));
        } else if (!ensure_ascii) {
            out.append(text.substr(seq_start, pos - seq_start));
        } else if (cp < 0x10000) {
            out += std::format("\\u{:04x}", static_cast<unsigned>(cp));
        } else {
            const char32_t v = cp - 0x10000;
            out += std::format("\\u{:04x}\\u{:04x}", 0xd800 + (v >> 10), 0xdc00 + (v & 0x3ff));
        }
    }
    out.push_back('"');
}

void write_value(std::string& out, const Json& value, const Style& style, int depth);

void write_newline(std::string& out, const Style& style, int depth) {
    out.push_back('\n');
    out.append(static_cast<std::size_t>(style.indent * depth), ' ');
}

void write_object(std::string& out, const Json& value, const Style& style, int depth) {
    if (value.empty()) {
        out += "{}";
        return;
    }
    std::vector<const Json::object_t::value_type*> entries;
    entries.reserve(value.size());
    for (const auto& item : value.items()) {
        // items() yields proxies; take the underlying object entries instead.
        (void)item;
    }
    const auto& obj = value.get_ref<const Json::object_t&>();
    for (const auto& entry : obj) entries.push_back(&entry);
    if (style.sort_keys) {
        std::stable_sort(entries.begin(), entries.end(),
                         [](const auto* a, const auto* b) { return a->first < b->first; });
    }
    out.push_back('{');
    bool first = true;
    for (const auto* entry : entries) {
        if (!first) {
            out.push_back(',');
            if (style.indent == 0 && style.spaces) out.push_back(' ');
        }
        first = false;
        if (style.indent > 0) write_newline(out, style, depth + 1);
        escape_string(out, entry->first, style.ensure_ascii);
        out.push_back(':');
        if (style.spaces) out.push_back(' ');
        write_value(out, entry->second, style, depth + 1);
    }
    if (style.indent > 0) write_newline(out, style, depth);
    out.push_back('}');
}

void write_array(std::string& out, const Json& value, const Style& style, int depth) {
    if (value.empty()) {
        out += "[]";
        return;
    }
    out.push_back('[');
    bool first = true;
    for (const auto& item : value) {
        if (!first) {
            out.push_back(',');
            if (style.indent == 0 && style.spaces) out.push_back(' ');
        }
        first = false;
        if (style.indent > 0) write_newline(out, style, depth + 1);
        write_value(out, item, style, depth + 1);
    }
    if (style.indent > 0) write_newline(out, style, depth);
    out.push_back(']');
}

void write_value(std::string& out, const Json& value, const Style& style, int depth) {
    switch (value.type()) {
        case Json::value_t::null: out += "null"; break;
        case Json::value_t::boolean: out += value.get<bool>() ? "true" : "false"; break;
        case Json::value_t::number_integer: out += std::to_string(value.get<std::int64_t>()); break;
        case Json::value_t::number_unsigned: out += std::to_string(value.get<std::uint64_t>()); break;
        case Json::value_t::number_float: out += py::float_repr(value.get<double>()); break;
        case Json::value_t::string: escape_string(out, value.get_ref<const std::string&>(), style.ensure_ascii); break;
        case Json::value_t::object: write_object(out, value, style, depth); break;
        case Json::value_t::array: write_array(out, value, style, depth); break;
        default: out += "null"; break;
    }
}

}  // namespace

std::string dumps(const Json& value, const Style& style) {
    std::string out;
    write_value(out, value, style, 0);
    return out;
}

std::expected<Json, std::string> parse(std::string_view text) {
    // A byte-order mark is not JSON to Python's decoder; nlohmann would skip it.
    if (text.starts_with("\xef\xbb\xbf")) return std::unexpected("invalid character (byte 0)");
    const Json parsed = Json::parse(text, nullptr, false, false);
    if (parsed.is_discarded()) {
        // Re-run with exceptions to harvest the parser's message.
        try {
            [[maybe_unused]] const Json checked = Json::parse(text, nullptr, true, false);
        } catch (const Json::exception& exc) {
            std::string msg = exc.what();
            // Trim nlohmann's "[json.exception.parse_error.101] parse error at line 1, column N: " prefix.
            const auto colon = msg.find(": ");
            if (colon != std::string::npos) msg = msg.substr(colon + 2);
            return std::unexpected(msg);
        }
        return std::unexpected("invalid JSON");
    }
    return parsed;
}

bool py_equal(const Json& a, const Json& b) {
    if (a.is_number() && b.is_number()) {
        if (a.is_number_float() || b.is_number_float()) return a.get<double>() == b.get<double>();
        if (a.is_number_unsigned() || b.is_number_unsigned()) {
            if (a.is_number_integer() && a.get<std::int64_t>() < 0) return false;
            if (b.is_number_integer() && b.get<std::int64_t>() < 0) return false;
            return a.get<std::uint64_t>() == b.get<std::uint64_t>();
        }
        return a.get<std::int64_t>() == b.get<std::int64_t>();
    }
    if (a.is_boolean() && b.is_number()) return py_equal(Json(a.get<bool>() ? 1 : 0), b);
    if (a.is_number() && b.is_boolean()) return py_equal(a, Json(b.get<bool>() ? 1 : 0));
    if (a.type() != b.type()) return false;
    if (a.is_array()) {
        if (a.size() != b.size()) return false;
        for (std::size_t i = 0; i < a.size(); ++i) {
            if (!py_equal(a[i], b[i])) return false;
        }
        return true;
    }
    if (a.is_object()) {
        if (a.size() != b.size()) return false;
        for (const auto& [k, v] : a.items()) {
            const auto it = b.find(k);
            if (it == b.end() || !py_equal(v, *it)) return false;
        }
        return true;
    }
    return a == b;
}

std::string py_repr(const Json& value) {
    switch (value.type()) {
        case Json::value_t::null: return "None";
        case Json::value_t::boolean: return value.get<bool>() ? "True" : "False";
        case Json::value_t::number_integer: return std::to_string(value.get<std::int64_t>());
        case Json::value_t::number_unsigned: return std::to_string(value.get<std::uint64_t>());
        case Json::value_t::number_float: return py::float_repr(value.get<double>());
        case Json::value_t::string: return py::repr(value.get_ref<const std::string&>());
        case Json::value_t::array: {
            std::string out = "[";
            bool first = true;
            for (const auto& item : value) {
                if (!first) out += ", ";
                first = false;
                out += py_repr(item);
            }
            return out + "]";
        }
        case Json::value_t::object: {
            std::string out = "{";
            bool first = true;
            for (const auto& [k, v] : value.items()) {
                if (!first) out += ", ";
                first = false;
                out += py::repr(k);
                out += ": ";
                out += py_repr(v);
            }
            return out + "}";
        }
        default: return "None";
    }
}

std::string py_str(const Json& value) {
    if (value.is_string()) return value.get<std::string>();
    return py_repr(value);
}

}  // namespace rp::json
