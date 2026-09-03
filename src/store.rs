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

/// How to treat a final line that has no trailing newline and does not decode.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Tail {
    /// Skip it: a lock-free reader may have caught another writer mid-append
    /// (spec §7), and every completed line ends with a newline.
    Tolerate,
    /// Report it: `rp doctor` wants to know about a truncated file.
    Report,
}
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
    let _guard = file_lock(rohrpost_dir)?;
    append_event_locked(rohrpost_dir, event)
}

/// Append one event; the caller holds the store lock (see [`file_lock`]).
///
/// The append is **one** `write` call. A short write is rolled back (the file
/// is truncated to its previous length) and reported, never resumed: a second
/// write could interleave with another writer and a process exit between the
/// two would leave a permanent half-line (spec §7).
pub fn append_event_locked(rohrpost_dir: &Path, event: &Event) -> Result<()> {
    let mut line = event.encode();
    line.push('\n');
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
    let written = loop {
        match file.write(line.as_bytes()) {
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            other => break other,
        }
    };
    match written {
        Ok(n) if n == line.len() => Ok(()),
        outcome => {
            // Roll back the partial line: a half-written tail would otherwise
            // fail to decode on every future read (spec §3 principle 5).
            let _ = file.set_len(before);
            Err(match outcome {
                Ok(n) => Error::Store(format!(
                    "short write to {}: wrote {n} of {} bytes; rolled back",
                    log.display(),
                    line.len()
                )),
                Err(e) => io_error("cannot append to", &log, &e),
            })
        }
    }
}

/// Archive files (oldest first) then the live log — the fold's read order (§6).
fn all_log_files(rohrpost_dir: &Path) -> Result<Vec<PathBuf>> {
    let mut files = paths::archive_files(rohrpost_dir)?;
    files.push(paths::log_path(rohrpost_dir));
    Ok(files)
}

/// Decode every line of every file, collecting malformed lines as messages.
fn decode_files(files: &[PathBuf], tail: Tail) -> Result<(Vec<Event>, Vec<String>)> {
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
        let segments: Vec<&[u8]> = bytes.split(|&b| b == b'\n').collect();
        // Only the bytes after the final newline can be an in-flight append.
        let unterminated = segments.len() - 1;
        for (index, raw) in segments.into_iter().enumerate() {
            let trimmed = trim_ascii(raw);
            if trimmed.is_empty() {
                continue;
            }
            let lineno = index + 1;
            if index == unterminated
                && tail == Tail::Tolerate
                && decode_line_bytes(trimmed).is_err()
            {
                continue;
            }
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

fn decode_line_bytes(line: &[u8]) -> std::result::Result<Event, String> {
    let text = std::str::from_utf8(line).map_err(|_| "not valid UTF-8".to_string())?;
    decode_line(text)
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
    let (events, errors) = decode_files(&all_log_files(rohrpost_dir)?, Tail::Tolerate)?;
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
    let (events, errors) = decode_files(&[paths::log_path(rohrpost_dir)], Tail::Tolerate)?;
    if let Some(first) = errors.first() {
        return Err(Error::Store(format!(
            "malformed event log ({} bad line(s)): {first}",
            errors.len()
        )));
    }
    Ok(events)
}

/// Like [`read_events`] but returns malformed lines instead of failing (for
/// `rp doctor`), including an unterminated final line.
pub fn read_events_lenient(rohrpost_dir: &Path) -> Result<(Vec<Event>, Vec<String>)> {
    decode_files(&all_log_files(rohrpost_dir)?, Tail::Report)
}

/// Atomically replace `path` with `content` (write a sibling temp file, rename over).
pub fn write_atomic(path: &Path, content: &[u8]) -> Result<()> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, content).map_err(|e| io_error("cannot write", &tmp, &e))?;
    std::fs::rename(&tmp, path).map_err(|e| io_error("cannot replace", path, &e))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_store() -> PathBuf {
        static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "rp-store-{}-{}-{}",
            std::process::id(),
            N.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
            crate::time::now_ms()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn strict_reads_tolerate_an_in_flight_tail_but_doctor_sees_it() {
        let dir = temp_store();
        let event = decode_line(
            r#"{"id":"01K","ts":"2026-01-01T00:00:00.000Z","ticket":"a1b2c3","op":"create","actor":"u","set":{"title":"t"}}"#,
        )
        .unwrap();
        append_event(&dir, &event).unwrap();
        let log = paths::log_path(&dir);
        let mut bytes = std::fs::read(&log).unwrap();
        bytes.extend_from_slice(br#"{"id":"01L","ts":"2026-01-01T00:00:01.000Z","tick"#);
        std::fs::write(&log, &bytes).unwrap();

        assert_eq!(
            read_events(&dir).unwrap().len(),
            1,
            "the partial tail is skipped"
        );
        let (_, errors) = read_events_lenient(&dir).unwrap();
        assert_eq!(errors.len(), 1, "but reported to doctor");

        // A malformed line that *is* terminated is corruption, not an in-flight write.
        bytes.push(b'\n');
        std::fs::write(&log, &bytes).unwrap();
        assert!(read_events(&dir).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_lock_is_exclusive_across_handles() {
        let dir = temp_store();
        let guard = file_lock(&dir).unwrap();
        let other = File::open(paths::lock_path(&dir)).unwrap();
        assert!(matches!(
            other.try_lock(),
            Err(std::fs::TryLockError::WouldBlock)
        ));
        drop(guard);
        assert!(other.try_lock().is_ok());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
