// Domain errors raised by rohrpost (mirrors src/rohrpost/exceptions.py).
//
// The CLI maps UsageError to exit 2 and every other RohrpostError to exit 1,
// printing `rp: <message>` on stderr. The hierarchy is flat on purpose: the
// message is the contract, the type only decides the exit code.
#pragma once

#include <stdexcept>
#include <string>

namespace rp {

/// Base class for all errors raised by rohrpost.
class RohrpostError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

/// A ticket or event id is malformed or out of range.
class IdError : public RohrpostError {
public:
    using RohrpostError::RohrpostError;
};

/// Log/store integrity failures (parse errors, lock contention, ...).
class StoreError : public RohrpostError {
public:
    using RohrpostError::RohrpostError;
};

/// config.toml is missing, malformed or has invalid values.
class ConfigError : public RohrpostError {
public:
    using RohrpostError::RohrpostError;
};

/// Ticket-level problems: not found, bad status, cycle, etc.
class TicketError : public RohrpostError {
public:
    using RohrpostError::RohrpostError;
};

/// A referenced ticket id does not exist in the folded log.
class TicketNotFoundError : public TicketError {
public:
    using TicketError::TicketError;
};

/// CLI usage mistakes (conflicting flags, unreadable input files): exit 2.
class UsageError : public RohrpostError {
public:
    using RohrpostError::RohrpostError;
};

/// A linked item has been deleted from its remote tracker.
class RemoteItemNotFoundError : public RohrpostError {
public:
    using RohrpostError::RohrpostError;
};

}  // namespace rp
