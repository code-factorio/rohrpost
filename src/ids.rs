//! Identifiers: ticket ids and event ULIDs (spec §5.1).
//!
//! * **Ticket ids** — 6 lowercase Crockford base32 characters from 30 random
//!   bits (~1e9 values; the collision domain is one repository). The display
//!   prefix (`FAC-a1b2c3`) never enters the log; `normalize_id` accepts both.
//! * **Event ids** — ULIDs: 26 uppercase Crockford base32 characters encoding
//!   a 48-bit millisecond timestamp and 80 bits of randomness, so they sort by
//!   creation time and give the fold a deterministic tiebreak.
//!
//! Entropy comes from the standard library's OS-seeded [`RandomState`]: each
//! draw hashes a fresh counter under a key the OS supplied (`getrandom`,
//! `BCryptGenRandom` or `arc4random` depending on platform). Ids need
//! collision resistance, not secrecy, and this is uniform, unpredictable and
//! dependency-free on all three platforms.

use std::hash::{BuildHasher, RandomState};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::error::{Error, Result};

const TICKET_ALPHABET: &[u8; 32] = b"0123456789abcdefghjkmnpqrstvwxyz";
const ULID_ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
pub const TICKET_LENGTH: usize = 6;
pub const ULID_LENGTH: usize = 26;
const TIMESTAMP_BITS: u32 = 48;
const RANDOMNESS_BITS: u32 = 80;

static DRAWS: AtomicU64 = AtomicU64::new(0);

/// 64 random-looking bits. See the module docs for why this is enough.
fn random_u64() -> u64 {
    let counter = DRAWS.fetch_add(1, Ordering::Relaxed);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    RandomState::new().hash_one((counter, nanos, std::process::id()))
}

fn encode_base32(mut value: u128, length: usize, alphabet: &[u8; 32]) -> String {
    let mut buf = vec![b'0'; length];
    for slot in buf.iter_mut().rev() {
        *slot = alphabet[(value & 0x1F) as usize];
        value >>= 5;
    }
    String::from_utf8(buf).expect("base32 alphabet is ASCII")
}

/// A fresh 6-char lowercase base32 ticket id, e.g. `a1b2c3`.
pub fn new_ticket_id() -> String {
    let bits = (random_u64() as u128) & ((1u128 << (TICKET_LENGTH * 5)) - 1);
    encode_base32(bits, TICKET_LENGTH, TICKET_ALPHABET)
}

/// True if `value` is a bare 6-char lowercase base32 ticket id.
pub fn is_valid_ticket_id(value: &str) -> bool {
    value.len() == TICKET_LENGTH
        && value.bytes().all(|b| {
            matches!(b, b'0'..=b'9' | b'a'..=b'h' | b'j' | b'k' | b'm' | b'n' | b'p'..=b't' | b'v'..=b'z')
        })
}

/// A fresh time-ordered ULID for `timestamp_ms` (the current time when `None`).
pub fn new_ulid(timestamp_ms: Option<u64>) -> Result<String> {
    let ts = timestamp_ms.unwrap_or_else(crate::time::now_ms);
    if ts >= (1u64 << TIMESTAMP_BITS) {
        return Err(Error::Id(format!(
            "timestamp out of range for a 48-bit ULID: {ts}"
        )));
    }
    let random =
        ((random_u64() as u128) << 64 | random_u64() as u128) & ((1u128 << RANDOMNESS_BITS) - 1);
    let value = ((ts as u128) << RANDOMNESS_BITS) | random;
    Ok(encode_base32(value, ULID_LENGTH, ULID_ALPHABET))
}

/// True if `value` is a well-formed 26-char Crockford-base32 ULID.
pub fn is_valid_ulid(value: &str) -> bool {
    value.len() == ULID_LENGTH
        && value.bytes().all(|b| {
            matches!(b, b'0'..=b'9' | b'A'..=b'H' | b'J' | b'K' | b'M' | b'N' | b'P'..=b'T' | b'V'..=b'Z')
        })
}

/// Render a bare id with its display prefix: `FAC-a1b2c3`.
pub fn render_id(prefix: &str, ticket_id: &str) -> String {
    format!("{prefix}-{ticket_id}")
}

/// The bare id inside `a1b2c3` or `PREFIX-a1b2c3`, borrowed; `None` if invalid.
pub fn bare_slice(value: &str) -> Option<&str> {
    let candidate = value.rsplit('-').next().unwrap_or(value);
    is_valid_ticket_id(candidate).then_some(candidate)
}

/// The bare ticket id from either `a1b2c3` or `PREFIX-a1b2c3`.
pub fn normalize_id(value: &str) -> Result<String> {
    bare_slice(value)
        .map(str::to_string)
        .ok_or_else(|| Error::Id(format!("not a valid ticket id: '{value}'")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ticket_ids_are_six_lowercase_base32_chars_and_vary() {
        let ids: std::collections::HashSet<String> = (0..64).map(|_| new_ticket_id()).collect();
        assert!(ids.iter().all(|id| is_valid_ticket_id(id)), "{ids:?}");
        assert!(ids.len() > 60, "too many collisions: {}", ids.len());
    }

    #[test]
    fn ulids_encode_the_timestamp_and_sort_by_it() {
        let a = new_ulid(Some(1_767_225_600_000)).unwrap();
        let b = new_ulid(Some(1_767_225_600_001)).unwrap();
        assert!(is_valid_ulid(&a) && is_valid_ulid(&b));
        assert!(a < b);
        assert_eq!(&a[..10], &new_ulid(Some(1_767_225_600_000)).unwrap()[..10]);
        assert!(new_ulid(Some(1 << 48)).is_err());
    }

    #[test]
    fn normalize_accepts_bare_and_rendered_ids() {
        assert_eq!(normalize_id("a1b2c3").unwrap(), "a1b2c3");
        assert_eq!(normalize_id("FAC-a1b2c3").unwrap(), "a1b2c3");
        assert_eq!(normalize_id("my-proj-a1b2c3").unwrap(), "a1b2c3");
        for bad in ["A1B2C3", "a1b2c", "a1b2c3d", "a1b2cl", "", "FAC-"] {
            assert!(normalize_id(bad).is_err(), "accepted {bad:?}");
        }
    }
}
