//! Domain errors. One enum, so the CLI can map every failure to an exit code:
//! `Usage` is exit 2 (argparse convention), everything else exit 1.

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// A CLI usage mistake: conflicting flags, unreadable input file, bad option.
    Usage(String),
    /// A malformed ticket or event id.
    Id(String),
    /// Log/store integrity failure: parse error, lock failure, I/O.
    Store(String),
    /// `config.toml` is malformed or holds invalid values.
    Config(String),
    /// Ticket-level problem: bad status, bad field, invalid template.
    Ticket(String),
    /// A referenced ticket does not exist in the folded log.
    NotFound(String),
}

impl Error {
    pub fn message(&self) -> &str {
        match self {
            Error::Usage(m)
            | Error::Id(m)
            | Error::Store(m)
            | Error::Config(m)
            | Error::Ticket(m)
            | Error::NotFound(m) => m,
        }
    }

    /// Process exit code the CLI uses for this error.
    pub fn exit_code(&self) -> i32 {
        match self {
            Error::Usage(_) => 2,
            _ => 1,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.message())
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

/// Shorthand for the common "I/O on path X failed" store error.
pub fn io_error(context: &str, path: &std::path::Path, err: &std::io::Error) -> Error {
    Error::Store(format!("{context} {}: {err}", path.display()))
}
