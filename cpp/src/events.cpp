#include "rohrpost/events.hpp"

#include "rohrpost/pyfmt.hpp"

#include <algorithm>
#include <expected>
#include <format>
#include <optional>
#include <string>
#include <string_view>
#include <utility>

namespace rp {
namespace {

std::string type_name(const Json& value) {
    switch (value.type()) {
        case Json::value_t::null: return "null";
        case Json::value_t::boolean: return "bool";
        case Json::value_t::number_integer:
        case Json::value_t::number_unsigned: return "int";
        case Json::value_t::number_float: return "float";
        case Json::value_t::string: return "str";
        case Json::value_t::array: return "array";
        case Json::value_t::object: return "object";
        default: return "unknown";
    }
}

std::expected<std::string, std::string> required_str(const Json& obj, const char* key) {
    const auto it = obj.find(key);
    if (it == obj.end()) return std::unexpected(std::format("Object missing required field `{}`", key));
    if (!it->is_string()) return std::unexpected(std::format("Expected `str`, got `{}` - at `$.{}`", type_name(*it), key));
    return it->get<std::string>();
}

std::expected<std::optional<std::string>, std::string> optional_str(const Json& obj, const char* key) {
    const auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return std::optional<std::string>{};
    if (!it->is_string()) return std::unexpected(std::format("Expected `str | null`, got `{}` - at `$.{}`", type_name(*it), key));
    return std::optional<std::string>(it->get<std::string>());
}

}  // namespace

Json to_json(const Event& event) {
    Json obj = Json::object();
    obj["id"] = event.id;
    obj["ts"] = event.ts;
    obj["ticket"] = event.ticket;
    obj["op"] = event.op;
    obj["actor"] = event.actor;
    if (event.set) obj["set"] = *event.set;
    if (event.text) obj["text"] = *event.text;
    if (event.remote) obj["remote"] = *event.remote;
    if (event.ref) obj["ref"] = *event.ref;
    if (event.at) obj["at"] = *event.at;
    if (event.reason) obj["reason"] = *event.reason;
    return obj;
}

std::string encode(const Event& event) {
    return json::dumps(to_json(event), json::kCompact);
}

std::expected<Event, std::string> decode_line(std::string_view line) {
    auto parsed = json::parse(line);
    if (!parsed) return std::unexpected("JSON is malformed: " + parsed.error());
    const Json& obj = *parsed;
    if (!obj.is_object()) return std::unexpected(std::format("Expected `object`, got `{}`", type_name(obj)));

    Event event;
    auto id = required_str(obj, "id");
    if (!id) return std::unexpected(id.error());
    auto ts = required_str(obj, "ts");
    if (!ts) return std::unexpected(ts.error());
    auto ticket = required_str(obj, "ticket");
    if (!ticket) return std::unexpected(ticket.error());
    auto op = required_str(obj, "op");
    if (!op) return std::unexpected(op.error());
    auto actor = required_str(obj, "actor");
    if (!actor) return std::unexpected(actor.error());
    if (std::find(kOps.begin(), kOps.end(), *op) == kOps.end()) {
        return std::unexpected(std::format("Invalid enum value {} - at `$.op`", py::repr(*op)));
    }
    event.id = std::move(*id);
    event.ts = std::move(*ts);
    event.ticket = std::move(*ticket);
    event.op = std::move(*op);
    event.actor = std::move(*actor);

    if (const auto it = obj.find("set"); it != obj.end() && !it->is_null()) {
        if (!it->is_object()) return std::unexpected(std::format("Expected `object | null`, got `{}` - at `$.set`", type_name(*it)));
        event.set = *it;
    }
    for (const auto* key : {"text", "remote", "ref", "at", "reason"}) {
        auto value = optional_str(obj, key);
        if (!value) return std::unexpected(value.error());
        if (std::string_view(key) == "text") event.text = *value;
        else if (std::string_view(key) == "remote") event.remote = *value;
        else if (std::string_view(key) == "ref") event.ref = *value;
        else if (std::string_view(key) == "at") event.at = *value;
        else event.reason = *value;
    }
    return event;
}

}  // namespace rp
