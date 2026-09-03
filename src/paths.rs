//! The `.rohrpost/` layout (spec §4) and repository discovery.
//!
//! Discovery walks up from the current directory looking for `.rohrpost/`, the
//! way git finds `.git/`, so `rp` works from anywhere inside a repo. This is
//! the single place that builds these paths.

use std::path::{Path, PathBuf};

use crate::error::{Error, Result, io_error};

pub const ROHRPOST_DIR_NAME: &str = ".rohrpost";
pub const CONFIG_FILENAME: &str = "config.toml";
pub const LOG_FILENAME: &str = "log.jsonl";
pub const ARCHIVE_DIR_NAME: &str = "archive";
pub const TEMPLATES_DIR_NAME: &str = "templates";
pub const LOCK_FILENAME: &str = ".lock";

/// The committed `.gitattributes` rules (spec §4). `merge=union` keeps both
/// sides' appended lines; `text eol=lf` keeps the JSONL byte-identical across
/// platforms regardless of `core.autocrlf`.
pub const GITATTRIBUTES_RULES: &[&str] = &[
    ".rohrpost/log.jsonl          merge=union text eol=lf",
    ".rohrpost/archive/*.jsonl    merge=union text eol=lf",
];

pub fn config_path(dir: &Path) -> PathBuf {
    dir.join(CONFIG_FILENAME)
}
pub fn log_path(dir: &Path) -> PathBuf {
    dir.join(LOG_FILENAME)
}
pub fn archive_dir(dir: &Path) -> PathBuf {
    dir.join(ARCHIVE_DIR_NAME)
}
pub fn templates_dir(dir: &Path) -> PathBuf {
    dir.join(TEMPLATES_DIR_NAME)
}
pub fn lock_path(dir: &Path) -> PathBuf {
    dir.join(LOCK_FILENAME)
}

/// The repository root that owns a `.rohrpost/` dir (its parent).
pub fn repo_root(rohrpost_dir: &Path) -> &Path {
    rohrpost_dir.parent().unwrap_or(rohrpost_dir)
}

fn current_dir() -> Result<PathBuf> {
    std::env::current_dir()
        .map_err(|e| Error::Store(format!("cannot determine the current directory: {e}")))
}

/// Nearest ancestor of `start` (default cwd) containing `.git`, if any.
pub fn find_git_root(start: Option<&Path>) -> Result<Option<PathBuf>> {
    let here = match start {
        Some(p) => p.to_path_buf(),
        None => current_dir()?,
    };
    Ok(here
        .ancestors()
        .find(|dir| dir.join(".git").exists())
        .map(Path::to_path_buf))
}

/// Nearest ancestor of `start` (default cwd) containing `.rohrpost/`, if any.
pub fn find_rohrpost_dir(start: Option<&Path>) -> Result<Option<PathBuf>> {
    let here = match start {
        Some(p) => p.to_path_buf(),
        None => current_dir()?,
    };
    Ok(here
        .ancestors()
        .map(|dir| dir.join(ROHRPOST_DIR_NAME))
        .find(|candidate| candidate.is_dir()))
}

/// The `.rohrpost/` dir for the cwd, or a "run `rp init`" error.
pub fn require_rohrpost_dir() -> Result<PathBuf> {
    find_rohrpost_dir(None)?.ok_or_else(|| {
        Error::Store("not a rohrpost repository (no .rohrpost/ found). Run `rp init` first.".into())
    })
}

/// Sorted `archive/*.jsonl` files (oldest first by name). Empty if none.
pub fn archive_files(dir: &Path) -> Result<Vec<PathBuf>> {
    let adir = archive_dir(dir);
    if !adir.is_dir() {
        return Ok(Vec::new());
    }
    let mut files: Vec<PathBuf> = std::fs::read_dir(&adir)
        .map_err(|e| io_error("cannot list", &adir, &e))?
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|p| p.is_file() && p.extension().is_some_and(|ext| ext == "jsonl"))
        .collect();
    files.sort();
    Ok(files)
}

/// Create the directory scaffold and an empty log if missing (idempotent).
pub fn ensure_layout(dir: &Path) -> Result<()> {
    for d in [dir.to_path_buf(), archive_dir(dir), templates_dir(dir)] {
        std::fs::create_dir_all(&d).map_err(|e| io_error("cannot create", &d, &e))?;
    }
    let log = log_path(dir);
    std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(&log)
        .map_err(|e| io_error("cannot create", &log, &e))?;
    Ok(())
}

/// Append any of `lines` not already present to `path` (LF, UTF-8 bytes).
/// Returns whether the file changed. The file is user-owned and may predate
/// `rp`, so it is searched and extended as raw bytes, never re-encoded.
fn append_unique_lines(path: &Path, lines: &[&str]) -> Result<bool> {
    let existing = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(e) => return Err(io_error("cannot read", path, &e)),
    };
    let contains = |needle: &str| {
        existing
            .windows(needle.len())
            .any(|w| w == needle.as_bytes())
    };
    let missing: Vec<&str> = lines
        .iter()
        .copied()
        .filter(|line| !contains(line))
        .collect();
    if missing.is_empty() {
        return Ok(false);
    }
    let mut out = Vec::new();
    if !existing.is_empty() && !existing.ends_with(b"\n") {
        out.push(b'\n');
    }
    out.extend_from_slice(missing.join("\n").as_bytes());
    out.push(b'\n');
    use std::io::Write as _;
    let mut file = std::fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(path)
        .map_err(|e| io_error("cannot open", path, &e))?;
    file.write_all(&out)
        .map_err(|e| io_error("cannot write", path, &e))?;
    Ok(true)
}

/// Ensure the committed `.gitattributes` carries the merge and eol rules.
/// Idempotent; later lines win in gitattributes, so appending upgrades a file
/// that carries older rules.
pub fn write_gitattributes(repo_root: &Path) -> Result<bool> {
    append_unique_lines(&repo_root.join(".gitattributes"), GITATTRIBUTES_RULES)
}
