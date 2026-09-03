//! The append-only event log: read, append, lock (spec §7).
//!
//! Every append happens under an exclusive lock on `.rohrpost/.lock` and is one
//! write of one line in append mode, so two writers never interleave
//! half-lines. The lock is the standard library's [`File::lock`]: `flock` on
//! Unix, `LockFileEx` on Windows — advisory, blocking, released when the holder
//! exits. Append mode is `O_APPEND` on Unix and `FILE_APPEND_DATA` on Windows.
//! A write that fails part-way is rolled back (the file is truncated to its
//! previous length) rather than resumed, so the log never keeps a corrupt tail.
//!
//! Reads are lock-free: the log is strictly append-only and every event carries
//! a unique id, so a partial final line (a truly concurrent writer) fails to
//! decode and is skipped by the lenient reader; the fold deduplicates.

use std::fs::{File, OpenOptions};
use std::io::Write as _;
use std::path::{Path, PathBuf};

use crate::error::{Error, Result, io_error};
use crate::events::{Event, decode_line};
use crate::paths;

/// An exclusive lock on `.rohrpost/.lock`, released on drop.
pub struct LockGuard {
    file: File,
}

impl Drop for LockGuard {
    fn drop(&mut self) {
        // Closing the file releases the lock too; the explicit unlock is hygiene.
        let _ = self.file.unlock();
    }
}

/// Take the exclusive store lock (blocks until the current holder releases).
/// Do not nest two calls on the same directory: the second blocks forever.
pub fn file_lock(rohrpost_dir: &Path) -> Result<LockGuard> {
    let path = paths::lock_path(rohrpost_dir);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)
        .map_err(|e| io_error("cannot open lock file", &path, &e))?;
    file.lock()
        .map_err(|e| io_error("cannot lock", &path, &e))?;
    Ok(LockGuard { file })
}

/// Append one event as a single JSONL line under the store lock.
pub fn append_event(rohrpost_dir: &Path, event: &Event) -> Result<()> {
    let mut line = event.encode();
    line.push('\n');
    let _guard = file_lock(rohrpost_dir)?;
    let log = paths::log_path(rohrpost_dir);
    let mut file = OpenOptions::new()
        .append(true)
        .create(true)
        .open(&log)
        .map_err(|e| io_error("cannot open", &log, &e))?;
    let before = file
        .metadata()
        .map_err(|e| io_error("cannot stat", &log, &e))?
        .len();
    if let Err(e) = file.write_all(line.as_bytes()) {
        // Roll back a partial line: a half-written tail would fail to decode on
        // every future read (spec §3 principle 5). Best-effort.
        let _ = file.set_len(before);
        return Err(io_error("short write to", &log, &e));
    }
    Ok(())
}

/// Archive files (oldest first) then the live log — the fold's read order (§6).
fn all_log_files(rohrpost_dir: &Path) -> Result<Vec<PathBuf>> {
    let mut files = paths::archive_files(rohrpost_dir)?;
    files.push(paths::log_path(rohrpost_dir));
    Ok(files)
}

/// Decode every line of every file, collecting malformed lines as messages.
fn decode_files(files: &[PathBuf]) -> Result<(Vec<Event>, Vec<String>)> {
    let mut events = Vec::new();
    let mut errors = Vec::new();
    for path in files {
        let bytes = match std::fs::read(path) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) => return Err(io_error("cannot read", path, &e)),
        };
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        for (index, raw) in bytes.split(|&b| b == b'\n').enumerate() {
            let trimmed = trim_ascii(raw);
            if trimmed.is_empty() {
                continue;
            }
            let lineno = index + 1;
            match std::str::from_utf8(trimmed) {
                Err(_) => errors.push(format!("{name} line {lineno}: not valid UTF-8")),
                Ok(text) => match decode_line(text) {
                    Ok(event) => events.push(event),
                    Err(msg) => {
                        let preview: String = text.chars().take(80).collect();
                        errors.push(format!("{name} line {lineno}: {msg}: '{preview}'"));
                    }
                },
            }
        }
    }
    Ok((events, errors))
}

fn trim_ascii(bytes: &[u8]) -> &[u8] {
    let is_ws = |b: &u8| matches!(b, b' ' | b'\t' | b'\r' | b'\n');
    let start = bytes.iter().position(|b| !is_ws(b)).unwrap_or(bytes.len());
    let end = bytes
        .iter()
        .rposition(|b| !is_ws(b))
        .map_or(start, |i| i + 1);
    &bytes[start..end]
}

/// Every event from archive then log, in file order. Errors on the first
/// malformed line: a corrupt log is a loud failure. Duplicates are not removed
/// here; the fold deduplicates by event id.
pub fn read_events(rohrpost_dir: &Path) -> Result<Vec<Event>> {
    let (events, errors) = decode_files(&all_log_files(rohrpost_dir)?)?;
    if let Some(first) = errors.first() {
        return Err(Error::Store(format!(
            "malformed event log ({} bad line(s)): {first}",
            errors.len()
        )));
    }
    Ok(events)
}

/// Only the live `log.jsonl` (no archive), strictly decoded. Compaction moves
/// events out of this file and must never re-archive what is already archived.
pub fn read_live_events(rohrpost_dir: &Path) -> Result<Vec<Event>> {
    let (events, errors) = decode_files(&[paths::log_path(rohrpost_dir)])?;
    if let Some(first) = errors.first() {
        return Err(Error::Store(format!(
            "malformed event log ({} bad line(s)): {first}",
            errors.len()
        )));
    }
    Ok(events)
}

/// Like [`read_events`] but returns malformed lines instead of failing (for `rp doctor`).
pub fn read_events_lenient(rohrpost_dir: &Path) -> Result<(Vec<Event>, Vec<String>)> {
    decode_files(&all_log_files(rohrpost_dir)?)
}

/// Atomically replace `path` with `content` (write a sibling temp file, rename over).
pub fn write_atomic(path: &Path, content: &[u8]) -> Result<()> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, content).map_err(|e| io_error("cannot write", &tmp, &e))?;
    std::fs::rename(&tmp, path).map_err(|e| io_error("cannot replace", path, &e))
}
