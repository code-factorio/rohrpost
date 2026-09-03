#include "rohrpost/entropy.hpp"

#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>

#if defined(_WIN32)
// windows.h must precede bcrypt.h, and bcrypt.h uses NTSTATUS from winternl.h.
#include <windows.h>
#include <winternl.h>
#include <bcrypt.h>
#elif defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__)
#include <cstdlib>
#else
#include <cerrno>
#include <fcntl.h>
#include <sys/random.h>
#include <unistd.h>
#endif

namespace rp::entropy {

void fill(std::span<std::uint8_t> out) {
#if defined(_WIN32)
    const NTSTATUS status = BCryptGenRandom(nullptr, out.data(), static_cast<ULONG>(out.size()),
                                            BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    if (status != 0) throw std::runtime_error("BCryptGenRandom failed");
#elif defined(__APPLE__) || defined(__FreeBSD__) || defined(__OpenBSD__)
    arc4random_buf(out.data(), out.size());
#else
    std::size_t done = 0;
    while (done < out.size()) {
        const ssize_t n = getrandom(out.data() + done, out.size() - done, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            break;
        }
        done += static_cast<std::size_t>(n);
    }
    if (done < out.size()) {
        // Fallback for kernels without getrandom.
        const int fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
        if (fd < 0) throw std::runtime_error("no entropy source available");
        while (done < out.size()) {
            const ssize_t n = read(fd, out.data() + done, out.size() - done);
            if (n <= 0) {
                close(fd);
                throw std::runtime_error("short read from /dev/urandom");
            }
            done += static_cast<std::size_t>(n);
        }
        close(fd);
    }
#endif
}

std::uint64_t randbits(int bits) {
    std::uint8_t bytes[8];
    fill(bytes);
    std::uint64_t value = 0;
    for (const auto b : bytes) value = (value << 8) | b;
    if (bits >= 64) return value;
    return value & ((std::uint64_t{1} << bits) - 1);
}

}  // namespace rp::entropy
