// Sync providers (spec §8.5): adapt a remote tracker to a flat
// `{local_field: value}` map so the sync round stays provider-agnostic.
#pragma once

#include "rohrpost/json.hpp"

#include <string>
#include <string_view>

namespace rp::providers {

class Provider {
public:
    virtual ~Provider() = default;
    [[nodiscard]] virtual std::string remote() const = 0;
    /// The remote item as `{local_field: value}`. Throws RemoteItemNotFoundError.
    [[nodiscard]] virtual Json fetch(std::string_view ref) = 0;
    /// Write `{local_field: value}` back; returns the resulting remote fields.
    [[nodiscard]] virtual Json push(std::string_view ref, const Json& fields) = 0;
};

}  // namespace rp::providers
