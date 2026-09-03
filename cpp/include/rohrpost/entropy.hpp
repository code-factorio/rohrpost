// Operating-system entropy for ids (the `secrets` module in the reference).
#pragma once

#include <cstddef>
#include <cstdint>
#include <span>

namespace rp::entropy {

/// Fill `out` from the platform CSPRNG. Throws std::runtime_error if none is available.
void fill(std::span<std::uint8_t> out);

/// `secrets.randbits(bits)` for bits <= 64.
[[nodiscard]] std::uint64_t randbits(int bits);

}  // namespace rp::entropy
