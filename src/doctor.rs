//! `rp doctor` — integrity and configuration checks (spec §10.1).
//!
//! A list of independent checks, each returning a finding; any failing finding
//! is a non-zero exit. Checks degrade rather than crash: an unparseable log is
//! reported by `log_parses` and the dependent checks report themselves as
//! skipped. This is the one place the pneumatic metaphor is allowed out.

use std::collections::HashSet;
use std::path::Path;

use crate::error::Result;
use crate::events::Event;
use crate::fold::{find_cycle, fold};
use crate::json::{self, Json};
use crate::paths;
use crate::store;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    pub check: &'static str,
    pub ok: bool,
    pub detail: String,
}

impl Finding {
    fn new(check: &'static str, ok: bool, detail: impl Into<String>) -> Finding {
        Finding {
            check,
            ok,
            detail: detail.into(),
        }
    }

    pub fn to_json(&self) -> Json {
        Json::Obj(vec![
            ("check".into(), json::s(self.check)),
            ("ok".into(), Json::Bool(self.ok)),
            ("detail".into(), json::s(&self.detail)),
        ])
    }
}

/// Run every check. Failing findings have `ok == false`.
pub fn run(rohrpost_dir: &Path) -> Result<Vec<Finding>> {
    let (events, errors) = store::read_events_lenient(rohrpost_dir)?;
    let log_ok = errors.is_empty();
    let by_id = fold(&events);
    let skipped = |check| Finding::new(check, true, "skipped (log unparseable)");

    let mut findings = vec![if log_ok {
        Finding::new(
            "log_parses",
            true,
            format!("{} event(s) parsed cleanly", events.len()),
        )
    } else {
        Finding::new(
            "log_parses",
            false,
            format!("{} malformed line(s); first: {}", errors.len(), errors[0]),
        )
    }];

    findings.push(if log_ok {
        check_duplicate_ids(&events)
    } else {
        skipped("no_duplicate_ids")
    });

    findings.push(if log_ok {
        let mut missing: Vec<String> = Vec::new();
        for t in by_id.values() {
            if let Some(parent) = &t.parent
                && !by_id.contains_key(parent)
            {
                missing.push(format!("{} -> parent {parent}", t.id));
            }
            missing.extend(
                t.blocked_by
                    .iter()
                    .filter(|d| !by_id.contains_key(*d))
                    .map(|d| format!("{} -> blocked_by {d}", t.id)),
            );
        }
        if missing.is_empty() {
            Finding::new(
                "references_resolve",
                true,
                "all parent/blocked_by references resolve",
            )
        } else {
            let sample: Vec<&str> = missing.iter().take(3).map(String::as_str).collect();
            Finding::new(
                "references_resolve",
                false,
                format!(
                    "{} dangling reference(s): {}",
                    missing.len(),
                    sample.join(", ")
                ),
            )
        }
    } else {
        skipped("references_resolve")
    });

    findings.push(if log_ok {
        match find_cycle(&by_id) {
            Some(cycle) => Finding::new(
                "no_cycles",
                false,
                format!("dependency cycle: {}", cycle.join(" -> ")),
            ),
            None => Finding::new("no_cycles", true, "no dependency cycles"),
        }
    } else {
        skipped("no_cycles")
    });

    findings.push(check_gitattributes(rohrpost_dir));

    if log_ok {
        let legacy = events
            .iter()
            .filter(|e| matches!(e.op.as_str(), "link" | "unlink" | "synced"))
            .count();
        if legacy > 0 {
            findings.push(Finding::new(
                "legacy_sync_events",
                true,
                format!("{legacy} link/unlink/synced event(s) from the removed sync layer are kept but ignored"),
            ));
        }
    }
    Ok(findings)
}

fn check_duplicate_ids(events: &[Event]) -> Finding {
    let mut seen: HashSet<&str> = HashSet::new();
    let mut dupes: HashSet<&str> = HashSet::new();
    for ev in events {
        if !seen.insert(&ev.id) {
            dupes.insert(&ev.id);
        }
    }
    if dupes.is_empty() {
        Finding::new(
            "no_duplicate_ids",
            true,
            format!("{} unique event id(s)", seen.len()),
        )
    } else {
        Finding::new(
            "no_duplicate_ids",
            false,
            format!("{} duplicate event id(s) after merge", dupes.len()),
        )
    }
}

fn check_gitattributes(rohrpost_dir: &Path) -> Finding {
    let path = paths::repo_root(rohrpost_dir).join(".gitattributes");
    let Ok(bytes) = std::fs::read(&path) else {
        return Finding::new(
            "gitattributes",
            false,
            ".gitattributes missing the required rules",
        );
    };
    let missing: Vec<&str> = paths::GITATTRIBUTES_RULES
        .iter()
        .copied()
        .filter(|rule| !bytes.windows(rule.len()).any(|w| w == rule.as_bytes()))
        .collect();
    if missing.is_empty() {
        Finding::new("gitattributes", true, "merge and line-ending rules present")
    } else {
        Finding::new(
            "gitattributes",
            false,
            format!("missing rule(s): {}", missing.join("; ")),
        )
    }
}

/// The human report.
pub fn render_report(findings: &[Finding]) -> String {
    let bad = findings.iter().filter(|f| !f.ok).count();
    let mut out = String::new();
    out.push_str(if bad == 0 {
        "rp doctor: all clear\n"
    } else {
        "rp doctor: problems found\n"
    });
    for f in findings {
        out.push_str(&format!(
            "  [{}] {}: {}\n",
            if f.ok { "ok " } else { "XX " },
            f.check,
            f.detail
        ));
    }
    if bad == 0 {
        out.push_str("Nothing stuck in the tube.\n");
    } else {
        out.push_str(&format!(
            "{bad} {} need attention.\n",
            if bad == 1 { "check" } else { "checks" }
        ));
    }
    out
}
