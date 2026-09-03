//! `rp compact` — archive terminal tickets' events and truncate the live log.
//!
//! Spec §6.1. Compaction is the one operation that rewrites the union-merged
//! `log.jsonl` instead of appending, so it is the one that can lose data if run
//! carelessly. It refuses unless the working tree is clean and `HEAD` is the
//! configured default branch (`--force` overrides; outside git there is nothing
//! to protect). It runs under the store lock so no appender races the rewrite,
//! and it appends to the archive *before* rewriting the log: an interruption
//! between the two steps leaves duplicated events (removed on read) rather
//! than lost ones.

use std::collections::BTreeMap;
use std::path::Path;

use crate::config::load_config;
use crate::error::{Error, Result, io_error};
use crate::events::Event;
use crate::fold::{TERMINAL, bare_id, fold};
use crate::json::{self, Json};
use crate::paths;
use crate::store;
use crate::time::{now_ms, parse_ts};
use crate::util::git_output;

/// Default retention before a terminal ticket's events are archived.
pub const DEFAULT_ARCHIVE_AFTER_DAYS: i64 = 90;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompactResult {
    pub archived: usize,
    pub remaining: usize,
    pub archive_files: Vec<String>,
}

impl CompactResult {
    pub fn to_json(&self) -> Json {
        Json::Obj(vec![
            ("archived".into(), Json::Int(self.archived as i64)),
            ("remaining".into(), Json::Int(self.remaining as i64)),
            (
                "archive_files".into(),
                json::str_list(self.archive_files.iter().cloned()),
            ),
        ])
    }
}

/// `log-<YYYY>-Q<N>.jsonl` for an event timestamp (unparseable → `log-unknown.jsonl`).
fn quarter_bucket(ts: &str) -> String {
    match parse_ts(ts) {
        Some(ms) => {
            let stamp = crate::time::format_ts(ms.max(0) as u64);
            let year = &stamp[0..4];
            let month: u32 = stamp[5..7].parse().unwrap_or(1);
            format!("log-{year}-Q{}.jsonl", (month - 1) / 3 + 1)
        }
        None => "log-unknown.jsonl".to_string(),
    }
}

/// A refusal reason if compaction must not proceed, else `None`.
fn guard(repo_root: &Path, force: bool, default_branch: Option<&str>) -> Option<String> {
    if force {
        return None;
    }
    let inside_git = git_output(Some(repo_root), &["rev-parse", "--is-inside-work-tree"])
        .as_deref()
        == Some("true");
    if !inside_git {
        return None; // outside git: nothing to protect
    }
    let dirty =
        git_output(Some(repo_root), &["status", "--porcelain"]).is_some_and(|s| !s.is_empty());
    if dirty {
        return Some("refusing to compact: working tree is dirty (use --force to override)".into());
    }
    // An unborn branch (no commits yet) has no name; git reports HEAD.
    let branch = git_output(Some(repo_root), &["rev-parse", "--abbrev-ref", "HEAD"])
        .unwrap_or_else(|| "HEAD".into());
    let expected = default_branch.unwrap_or("main");
    if branch != expected {
        return Some(format!(
            "refusing to compact: HEAD is on '{branch}', not '{expected}' (use --force to override)"
        ));
    }
    None
}

fn is_terminal_set(ev: &Event) -> bool {
    ev.op == "set"
        && ev
            .set
            .as_ref()
            .and_then(|s| s.iter().find(|(k, _)| k.as_ref() == "status"))
            .and_then(|(_, v)| v.as_str())
            .is_some_and(|s| TERMINAL.contains(&s))
}

/// Run compaction. `Err(Error::Ticket)` carries a refusal; other errors are I/O.
pub fn run(rohrpost_dir: &Path, archive_after_days: i64, force: bool) -> Result<CompactResult> {
    let config = load_config(rohrpost_dir)?;
    if let Some(reason) = guard(
        paths::repo_root(rohrpost_dir),
        force,
        config.default_branch.as_deref(),
    ) {
        return Err(Error::Ticket(reason));
    }
    let cutoff_ms = now_ms() as i64 - archive_after_days * 86_400_000;

    // Read and partition while holding the same lock as the rewrite, so no
    // append between the read and the rewrite can be lost. Terminal-ness is
    // judged over everything (archive included); only live events move.
    let _guard = store::file_lock(rohrpost_dir)?;
    let events = store::read_events(rohrpost_dir)?;
    let live = store::read_live_events(rohrpost_dir)?;
    let by_id = fold(&events);

    // Tickets terminal since before the cutoff (judged by their last terminal event).
    let mut last_terminal: BTreeMap<String, i64> = BTreeMap::new();
    for ev in events.iter().filter(|e| is_terminal_set(e)) {
        if let Some(ms) = parse_ts(&ev.ts) {
            let slot = last_terminal
                .entry(bare_id(&ev.ticket).to_string())
                .or_insert(i64::MIN);
            *slot = (*slot).max(ms);
        }
    }
    let archivable: std::collections::HashSet<&str> = by_id
        .values()
        .filter(|t| TERMINAL.contains(&t.status.as_str()))
        .filter(|t| last_terminal.get(&t.id).is_some_and(|ms| *ms < cutoff_ms))
        .map(|t| t.id.as_str())
        .collect();

    let mut keep: Vec<&Event> = Vec::new();
    let mut buckets: BTreeMap<String, Vec<&Event>> = BTreeMap::new();
    for ev in &live {
        if archivable.contains(bare_id(&ev.ticket)) {
            buckets.entry(quarter_bucket(&ev.ts)).or_default().push(ev);
        } else {
            keep.push(ev);
        }
    }
    let by_ts_id = |a: &&Event, b: &&Event| {
        (a.ts.as_str(), a.id.as_str()).cmp(&(b.ts.as_str(), b.id.as_str()))
    };

    // Archive first (append-only, dedup-safe on read), then rewrite the log.
    let adir = paths::archive_dir(rohrpost_dir);
    std::fs::create_dir_all(&adir).map_err(|e| io_error("cannot create", &adir, &e))?;
    for (bucket, evs) in buckets.iter_mut() {
        evs.sort_by(by_ts_id);
        let target = adir.join(bucket);
        let payload: String = evs.iter().map(|e| e.encode() + "\n").collect();
        use std::io::Write as _;
        let mut file = std::fs::OpenOptions::new()
            .append(true)
            .create(true)
            .open(&target)
            .map_err(|e| io_error("cannot open", &target, &e))?;
        file.write_all(payload.as_bytes())
            .map_err(|e| io_error("cannot write", &target, &e))?;
    }
    keep.sort_by(by_ts_id);
    let payload: String = keep.iter().map(|e| e.encode() + "\n").collect();
    store::write_atomic(&paths::log_path(rohrpost_dir), payload.as_bytes())?;

    Ok(CompactResult {
        archived: buckets.values().map(Vec::len).sum(),
        remaining: keep.len(),
        archive_files: buckets.keys().cloned().collect(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quarter_buckets_follow_the_event_timestamp() {
        assert_eq!(
            quarter_bucket("2026-02-01T00:00:00.000Z"),
            "log-2026-Q1.jsonl"
        );
        assert_eq!(
            quarter_bucket("2026-12-31T23:59:59.999Z"),
            "log-2026-Q4.jsonl"
        );
        assert_eq!(quarter_bucket("nonsense"), "log-unknown.jsonl");
    }
}
