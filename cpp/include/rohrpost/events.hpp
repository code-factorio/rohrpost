// Event log primitives: the append-only envelope that is the source of truth
// (mirrors src/rohrpost/events.py).
//
// The encoded line must stay byte-identical to the reference's msgspec output:
// field order id, ts, ticket, op, actor, then the op-dependent payloads that
// are present, compact separators, raw UTF-8.
#pragma once

#include "rohrpost/json.hpp"

#include <array>
#include <expected>
#include <optional>
#include <string>
#include <string_view>

namespace rp {

/// The closed set of operations an event can record (spec §5.2).
inline constexpr std::array<std::string_view, 6> kOps = {"create", "set", "comment", "link", "unlink", "synced"};

/// `synced` is a remote-level watermark; the envelope still needs a ticket string.
inline constexpr std::string_view kSyncTicket = "__sync__";

/// One line in `log.jsonl` — an immutable, append-only mutation record.
struct Event {
    std::string id;
    std::string ts;
    std::string ticket;
    std::string op;
    std::string actor;
    // Op-dependent payloads — present only when the op needs them.
    std::optional<Json> set;  // always an object when present
    std::optional<std::string> text;
    std::optional<std::string> remote;
    std::optional<std::string> ref;
    std::optional<std::string> at;
    std::optional<std::string> reason;

    bool operator==(const Event&) const = default;
};

/// Serialise an event to a single JSONL line (no trailing newline).
[[nodiscard]] std::string encode(const Event& event);

/// The event as a JSON object with only its present fields (`rp log --json`).
[[nodiscard]] Json to_json(const Event& event);

/// Decode one JSONL line. The error mirrors msgspec's message.
[[nodiscard]] std::expected<Event, std::string> decode_line(std::string_view line);

}  // namespace rp
