//! `rp stats` — repository statistics derived straight from the event log.
//!
//! The instrumentation behind spec §13.2 (inline vs sidecar bodies): body and
//! event-line byte distributions, the share of appends whose single line
//! exceeds the classic 4096-byte pipe/atomic-write budget, and the measured cost
//! of a cold fold. Everything but `fold_ms` is computed from the log, so the
//! numbers are retroactive and free at append time.

use std::path::Path;
use std::time::Instant;

use crate::error::Result;
use crate::fold::fold;
use crate::json::{Json, Key};
use crate::store;

/// The atomic-write budget the §13.2 thresholds are calibrated against.
/// (POSIX guarantees at least 512 for pipes; Linux reports 4096; Windows has no
/// equivalent constant. A fixed value keeps the signal comparable across hosts.)
pub const PIPE_BUF: usize = 4096;

fn percentile(sorted: &[usize], point: f64) -> i64 {
    match sorted.len() {
        0 => 0,
        1 => sorted[0] as i64,
        n => {
            let rank = point / 100.0 * (n - 1) as f64;
            let lo = rank.floor() as usize;
            let hi = (lo + 1).min(n - 1);
            let frac = rank - lo as f64;
            (sorted[lo] as f64 * (1.0 - frac) + sorted[hi] as f64 * frac).round() as i64
        }
    }
}

fn distribution(mut samples: Vec<usize>) -> Vec<(Key, Json)> {
    samples.sort_unstable();
    let mut dist: Vec<(Key, Json)> = [50.0, 90.0, 95.0, 99.0]
        .iter()
        .map(|p| {
            (
                Key::Owned(format!("p{}", *p as u32)),
                Json::Int(percentile(&samples, *p)),
            )
        })
        .collect();
    dist.push((
        "max".into(),
        Json::Int(samples.last().copied().unwrap_or(0) as i64),
    ));
    dist.push(("count".into(), Json::Int(samples.len() as i64)));
    dist
}

fn median_cold_fold_ms(rohrpost_dir: &Path, runs: usize) -> Result<f64> {
    let mut timings: Vec<f64> = Vec::with_capacity(runs);
    for _ in 0..runs.max(1) {
        let start = Instant::now();
        let events = store::read_events(rohrpost_dir)?;
        std::hint::black_box(fold(&events));
        timings.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    timings.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let mid = timings.len() / 2;
    let median = if timings.len().is_multiple_of(2) {
        (timings[mid - 1] + timings[mid]) / 2.0
    } else {
        timings[mid]
    };
    Ok((median * 1000.0).round() / 1000.0)
}

/// Compute the §13.2 decision signals from the live log.
pub fn compute_stats(rohrpost_dir: &Path, fold_runs: usize) -> Result<Json> {
    let events = store::read_events(rohrpost_dir)?;
    let mut line_bytes = Vec::with_capacity(events.len());
    let mut body_bytes = Vec::new();
    let mut over_pipe_buf = 0usize;
    let mut set_events = 0usize;
    for ev in &events {
        let line_len = ev.encode().len() + 1; // + the newline the append writes
        line_bytes.push(line_len);
        if line_len > PIPE_BUF {
            over_pipe_buf += 1;
        }
        if let Some(body) = ev
            .set
            .as_ref()
            .and_then(|s| s.iter().find(|(k, _)| k.as_ref() == "body"))
            .and_then(|(_, v)| v.as_str())
        {
            body_bytes.push(body.len());
        }
        if ev.op == "create" || ev.op == "set" {
            set_events += 1;
        }
    }
    let lock_share_pct = if set_events == 0 {
        0.0
    } else {
        (10_000.0 * over_pipe_buf as f64 / set_events as f64).round() / 100.0
    };
    let mut line_dist = distribution(line_bytes);
    line_dist.push(("over_pipe_buf".into(), Json::Int(over_pipe_buf as i64)));
    line_dist.push(("lock_share_pct".into(), Json::Float(lock_share_pct)));
    Ok(Json::Obj(vec![
        ("tickets".into(), Json::Int(fold(&events).len() as i64)),
        ("events".into(), Json::Int(events.len() as i64)),
        ("pipe_buf".into(), Json::Int(PIPE_BUF as i64)),
        ("body_bytes".into(), Json::Obj(distribution(body_bytes))),
        ("event_line_bytes".into(), Json::Obj(line_dist)),
        (
            "fold_ms".into(),
            Json::Float(median_cold_fold_ms(rohrpost_dir, fold_runs)?),
        ),
    ]))
}

/// The human summary.
pub fn render(data: &Json) -> String {
    let int = |obj: &Json, key: &str| obj.get(key).and_then(Json::as_i64).unwrap_or(0);
    let body = data.get("body_bytes").cloned().unwrap_or(Json::obj());
    let line = data.get("event_line_bytes").cloned().unwrap_or(Json::obj());
    let lock_share = match line.get("lock_share_pct") {
        Some(Json::Float(f)) => format!("{f}"),
        Some(Json::Int(i)) => format!("{i}"),
        _ => "0".into(),
    };
    let fold_ms = match data.get("fold_ms") {
        Some(Json::Float(f)) => format!("{f}"),
        Some(Json::Int(i)) => format!("{i}"),
        _ => "0".into(),
    };
    format!(
        "events: {}  tickets: {}  PIPE_BUF: {}\n\
         body bytes:       p50 {}  p90 {}  p95 {}  p99 {}  max {}  (n={})\n\
         event line bytes: p50 {}  p95 {}  max {}  over PIPE_BUF: {} ({lock_share}% of set events)\n\
         cold fold: {fold_ms} ms (median)\n",
        int(data, "events"),
        int(data, "tickets"),
        int(data, "pipe_buf"),
        int(&body, "p50"),
        int(&body, "p90"),
        int(&body, "p95"),
        int(&body, "p99"),
        int(&body, "max"),
        int(&body, "count"),
        int(&line, "p50"),
        int(&line, "p95"),
        int(&line, "max"),
        int(&line, "over_pipe_buf"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn percentiles_interpolate() {
        let s = vec![1usize, 2, 3, 4, 5];
        assert_eq!(percentile(&s, 50.0), 3);
        assert_eq!(percentile(&s, 100.0), 5);
        assert_eq!(percentile(&s, 25.0), 2);
        assert_eq!(percentile(&[], 50.0), 0);
        assert_eq!(percentile(&[7], 99.0), 7);
    }
}
