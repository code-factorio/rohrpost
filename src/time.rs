//! Timestamps: RFC 3339, UTC, millisecond precision (`2026-08-11T09:20:14.221Z`).
//!
//! `now_ts` is strictly increasing per process: two events written in the same
//! millisecond would otherwise tie on `ts` and fall back to the ULID's random
//! suffix for order, reordering append-only things like comments. Bumping the
//! millisecond on collision keeps insertion order deterministic; cross-process
//! ordering still relies on the ULID tiebreak (spec §6).

use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

static LAST_MS: Mutex<u64> = Mutex::new(0);

/// Wall-clock milliseconds since the Unix epoch.
pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// The current timestamp string, strictly increasing within this process.
pub fn now_ts() -> String {
    let mut ms = now_ms();
    let mut last = LAST_MS
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if ms <= *last {
        ms = *last + 1;
    }
    *last = ms;
    format_ts(ms)
}

/// Days since 1970-01-01 → (year, month, day). Howard Hinnant's algorithm.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// (year, month, day) → days since 1970-01-01. Inverse of `civil_from_days`.
fn days_from_civil(year: i64, month: u32, day: u32) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = if month > 2 { month - 3 } else { month + 9 } as i64;
    let doy = (153 * mp + 2) / 5 + i64::from(day) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Render milliseconds since the epoch as `YYYY-MM-DDTHH:MM:SS.mmmZ`.
pub fn format_ts(ms: u64) -> String {
    let secs = (ms / 1000) as i64;
    let millis = ms % 1000;
    let days = secs.div_euclid(86_400);
    let sod = secs.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{millis:03}Z",
        sod / 3600,
        (sod % 3600) / 60,
        sod % 60
    )
}

/// Parse an RFC 3339 timestamp to milliseconds since the epoch (UTC).
///
/// Accepts `Z` or a `±HH:MM` offset, and any number of fractional digits
/// (truncated to milliseconds). Returns `None` on anything malformed.
pub fn parse_ts(text: &str) -> Option<i64> {
    let b = text.as_bytes();
    if b.len() < 20
        || b[4] != b'-'
        || b[7] != b'-'
        || (b[10] != b'T' && b[10] != b't' && b[10] != b' ')
        || b[13] != b':'
        || b[16] != b':'
    {
        return None;
    }
    let num = |from: usize, to: usize| -> Option<i64> {
        let s = std::str::from_utf8(&b[from..to]).ok()?;
        if !s.bytes().all(|c| c.is_ascii_digit()) {
            return None;
        }
        s.parse().ok()
    };
    let (year, month, day) = (num(0, 4)?, num(5, 7)? as u32, num(8, 10)? as u32);
    let (hour, minute, second) = (num(11, 13)?, num(14, 16)?, num(17, 19)?);
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return None;
    }
    let mut pos = 19;
    let mut millis: i64 = 0;
    if b.get(pos) == Some(&b'.') {
        pos += 1;
        let start = pos;
        while pos < b.len() && b[pos].is_ascii_digit() {
            pos += 1;
        }
        if pos == start {
            return None;
        }
        let digits = &text[start..pos];
        let padded: String = digits
            .chars()
            .chain(std::iter::repeat('0'))
            .take(3)
            .collect();
        millis = padded.parse().ok()?;
    }
    let offset_secs: i64 = match b.get(pos) {
        Some(b'Z' | b'z') if pos + 1 == b.len() => 0,
        Some(sign @ (b'+' | b'-')) if pos + 6 == b.len() && b[pos + 3] == b':' => {
            let oh = num(pos + 1, pos + 3)?;
            let om = num(pos + 4, pos + 6)?;
            let total = oh * 3600 + om * 60;
            if *sign == b'+' { total } else { -total }
        }
        _ => return None,
    };
    let days = days_from_civil(year, month, day);
    let secs = days * 86_400 + hour * 3600 + minute * 60 + second - offset_secs;
    Some(secs * 1000 + millis)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn formats_like_the_spec_example() {
        assert_eq!(format_ts(1_786_440_014_221), "2026-08-11T09:20:14.221Z");
        assert_eq!(format_ts(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(format_ts(951_782_400_000), "2000-02-29T00:00:00.000Z");
    }

    #[test]
    fn parse_inverts_format_and_handles_offsets() {
        for ms in [0u64, 1_786_440_014_221, 4_102_444_800_999] {
            assert_eq!(parse_ts(&format_ts(ms)), Some(ms as i64));
        }
        assert_eq!(parse_ts("2026-08-11T09:20:14Z"), Some(1_786_440_014_000));
        assert_eq!(parse_ts("2026-08-11T09:20:14.2Z"), Some(1_786_440_014_200));
        assert_eq!(
            parse_ts("2026-08-11T11:20:14.221+02:00"),
            Some(1_786_440_014_221)
        );
        for bad in [
            "2026-08-11",
            "2026-13-01T00:00:00Z",
            "2026-08-11T09:20:14",
            "garbage",
        ] {
            assert_eq!(parse_ts(bad), None, "{bad}");
        }
    }

    #[test]
    fn now_ts_is_strictly_increasing() {
        let a = now_ts();
        let b = now_ts();
        let c = now_ts();
        assert!(a < b && b < c, "{a} {b} {c}");
    }
}
