//! A small JSON value type with a strict parser and two serialisers.
//!
//! Rohrpost's on-disk format is JSON Lines and its agent interface is `--json`,
//! so this module is load-bearing, and it is deliberately dependency-free.
//! Objects keep insertion order (a `Vec` of pairs) so that event lines and
//! `--json` output are stable and readable. The pretty serialiser matches
//! Python's `json.dumps(indent=2, ensure_ascii=False)` layout, which is what
//! the previous implementation emitted and what downstream tooling has seen.

use std::borrow::Cow;
use std::fmt::Write as _;

/// An object key. Keys rohrpost writes and reads constantly (`id`, `ts`,
/// `set`, `status`, …) are interned as `&'static str` so parsing a log line
/// does not allocate for them; anything else is an owned `String`.
pub type Key = Cow<'static, str>;

/// Keys the parser hands back without allocating.
const KNOWN_KEYS: &[&str] = &[
    "id",
    "ts",
    "ticket",
    "op",
    "actor",
    "set",
    "text",
    "reason",
    "remote",
    "ref",
    "at",
    "title",
    "type",
    "status",
    "priority",
    "parent",
    "assignee",
    "body",
    "labels",
    "labels+",
    "labels-",
    "blocked_by",
    "blocked_by+",
    "blocked_by-",
];

fn intern(key: &str) -> Key {
    match KNOWN_KEYS.iter().find(|k| **k == key) {
        Some(k) => Cow::Borrowed(k),
        None => Cow::Owned(key.to_string()),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(Key, Json)>),
}

impl Json {
    pub fn obj() -> Json {
        Json::Obj(Vec::new())
    }

    /// Look a key up in an object (linear scan: objects here are small).
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(pairs) => pairs
                .iter()
                .find(|(k, _)| k.as_ref() == key)
                .map(|(_, v)| v),
            _ => None,
        }
    }

    /// Insert or replace a key on an object; a no-op on non-objects.
    pub fn set(&mut self, key: &str, value: Json) {
        if let Json::Obj(pairs) = self {
            match pairs.iter_mut().find(|(k, _)| k.as_ref() == key) {
                Some(slot) => slot.1 = value,
                None => pairs.push((intern(key), value)),
            }
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(i) => Some(*i),
            Json::Float(f) if f.fract() == 0.0 && f.abs() < 9.0e15 => Some(*f as i64),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Json]> {
        match self {
            Json::Arr(items) => Some(items),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&[(Key, Json)]> {
        match self {
            Json::Obj(pairs) => Some(pairs),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Json::Null)
    }

    /// Compact single-line encoding (the JSONL event format).
    pub fn to_compact(&self) -> String {
        let mut out = String::new();
        write_compact(self, &mut out);
        out
    }

    /// Two-space indented encoding, Python `json.dumps(indent=2)` layout.
    pub fn to_pretty(&self) -> String {
        let mut out = String::new();
        write_pretty(self, 0, &mut out);
        out
    }
}

/// Build a `Json::Str` from anything string-like.
pub fn s(value: impl Into<String>) -> Json {
    Json::Str(value.into())
}

/// `Json::Str` for `Some`, `Json::Null` for `None`.
pub fn opt(value: Option<&str>) -> Json {
    value.map(s).unwrap_or(Json::Null)
}

/// A JSON array of strings.
pub fn str_list<I, T>(items: I) -> Json
where
    I: IntoIterator<Item = T>,
    T: Into<String>,
{
    Json::Arr(items.into_iter().map(s).collect())
}

// ---------------------------------------------------------------------------
// Serialisation.
// ---------------------------------------------------------------------------
fn write_str(text: &str, out: &mut String) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0C}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_float(value: f64, out: &mut String) {
    if !value.is_finite() {
        // JSON has no NaN/Infinity; null is the least surprising stand-in.
        out.push_str("null");
    } else if value.fract() == 0.0 && value.abs() < 1.0e16 {
        let _ = write!(out, "{value:.1}");
    } else {
        let _ = write!(out, "{value}");
    }
}

fn write_scalar(value: &Json, out: &mut String) -> bool {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Int(i) => {
            let _ = write!(out, "{i}");
        }
        Json::Float(f) => write_float(*f, out),
        Json::Str(text) => write_str(text, out),
        Json::Arr(_) | Json::Obj(_) => return false,
    }
    true
}

fn write_compact(value: &Json, out: &mut String) {
    if write_scalar(value, out) {
        return;
    }
    match value {
        Json::Arr(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_compact(item, out);
            }
            out.push(']');
        }
        Json::Obj(pairs) => {
            out.push('{');
            for (i, (key, item)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(key, out);
                out.push(':');
                write_compact(item, out);
            }
            out.push('}');
        }
        _ => unreachable!("scalars handled above"),
    }
}

fn indent(level: usize, out: &mut String) {
    for _ in 0..level {
        out.push_str("  ");
    }
}

fn write_pretty(value: &Json, level: usize, out: &mut String) {
    if write_scalar(value, out) {
        return;
    }
    match value {
        Json::Arr(items) if items.is_empty() => out.push_str("[]"),
        Json::Obj(pairs) if pairs.is_empty() => out.push_str("{}"),
        Json::Arr(items) => {
            out.push_str("[\n");
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                indent(level + 1, out);
                write_pretty(item, level + 1, out);
            }
            out.push('\n');
            indent(level, out);
            out.push(']');
        }
        Json::Obj(pairs) => {
            out.push_str("{\n");
            for (i, (key, item)) in pairs.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                indent(level + 1, out);
                write_str(key, out);
                out.push_str(": ");
                write_pretty(item, level + 1, out);
            }
            out.push('\n');
            indent(level, out);
            out.push('}');
        }
        _ => unreachable!("scalars handled above"),
    }
}

// ---------------------------------------------------------------------------
// Parsing.
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    pub offset: usize,
    pub message: String,
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} at byte {}", self.message, self.offset)
    }
}

/// Parse one complete JSON document; trailing non-whitespace is an error.
pub fn parse(text: &str) -> Result<Json, ParseError> {
    let mut parser = Parser {
        bytes: text.as_bytes(),
        pos: 0,
        depth: 0,
    };
    parser.skip_ws();
    let value = parser.value()?;
    parser.skip_ws();
    if parser.pos != parser.bytes.len() {
        return Err(parser.error("trailing characters after JSON value"));
    }
    Ok(value)
}

const MAX_DEPTH: usize = 256;

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
    depth: usize,
}

impl Parser<'_> {
    fn error(&self, message: &str) -> ParseError {
        ParseError {
            offset: self.pos,
            message: message.to_string(),
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn expect(&mut self, byte: u8) -> Result<(), ParseError> {
        if self.peek() == Some(byte) {
            self.pos += 1;
            Ok(())
        } else {
            Err(self.error(&format!("expected '{}'", byte as char)))
        }
    }

    fn value(&mut self) -> Result<Json, ParseError> {
        match self.peek() {
            None => Err(self.error("unexpected end of input")),
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => self.string().map(Json::Str),
            Some(b't') => self.literal("true", Json::Bool(true)),
            Some(b'f') => self.literal("false", Json::Bool(false)),
            Some(b'n') => self.literal("null", Json::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(_) => Err(self.error("unexpected character")),
        }
    }

    fn literal(&mut self, word: &str, value: Json) -> Result<Json, ParseError> {
        if self.bytes[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(value)
        } else {
            Err(self.error("invalid literal"))
        }
    }

    fn enter(&mut self) -> Result<(), ParseError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(self.error("nesting too deep"));
        }
        Ok(())
    }

    fn object(&mut self) -> Result<Json, ParseError> {
        self.enter()?;
        self.expect(b'{')?;
        let mut pairs = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(Json::Obj(pairs));
        }
        loop {
            self.skip_ws();
            if self.peek() != Some(b'"') {
                return Err(self.error("expected object key"));
            }
            let key = self.key_string()?;
            self.skip_ws();
            self.expect(b':')?;
            self.skip_ws();
            let value = self.value()?;
            pairs.push((key, value));
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    self.depth -= 1;
                    return Ok(Json::Obj(pairs));
                }
                _ => return Err(self.error("expected ',' or '}'")),
            }
        }
    }

    fn array(&mut self) -> Result<Json, ParseError> {
        self.enter()?;
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            self.depth -= 1;
            return Ok(Json::Arr(items));
        }
        loop {
            self.skip_ws();
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    self.depth -= 1;
                    return Ok(Json::Arr(items));
                }
                _ => return Err(self.error("expected ',' or ']'")),
            }
        }
    }

    fn hex4(&mut self) -> Result<u32, ParseError> {
        let end = self.pos + 4;
        let slice = self
            .bytes
            .get(self.pos..end)
            .ok_or_else(|| self.error("truncated \\u escape"))?;
        let text = std::str::from_utf8(slice).map_err(|_| self.error("bad \\u escape"))?;
        let value = u32::from_str_radix(text, 16).map_err(|_| self.error("bad \\u escape"))?;
        self.pos = end;
        Ok(value)
    }

    /// An object key: borrowed and interned when it has no escapes (the
    /// overwhelmingly common case), otherwise decoded like any string.
    fn key_string(&mut self) -> Result<Key, ParseError> {
        let start = self.pos + 1;
        let mut end = start;
        while let Some(b) = self.bytes.get(end) {
            match b {
                b'"' => {
                    let text = std::str::from_utf8(&self.bytes[start..end]).expect("utf-8 run");
                    self.pos = end + 1;
                    return Ok(intern(text));
                }
                b'\\' => break,
                b if *b < 0x20 => break,
                _ => end += 1,
            }
        }
        self.string().map(Cow::Owned)
    }

    fn string(&mut self) -> Result<String, ParseError> {
        self.expect(b'"')?;
        let mut out = String::new();
        loop {
            let start = self.pos;
            // Fast path: copy a run of plain bytes in one go.
            while let Some(b) = self.peek() {
                if b == b'"' || b == b'\\' || b < 0x20 {
                    break;
                }
                self.pos += 1;
            }
            // The input is a &str, and we only stop on ASCII bytes, so the run is valid UTF-8.
            out.push_str(std::str::from_utf8(&self.bytes[start..self.pos]).expect("utf-8 run"));
            match self.peek() {
                None => return Err(self.error("unterminated string")),
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.pos += 1;
                    let esc = self.peek().ok_or_else(|| self.error("truncated escape"))?;
                    self.pos += 1;
                    match esc {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{08}'),
                        b'f' => out.push('\u{0C}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let mut code = self.hex4()?;
                            if (0xD800..0xDC00).contains(&code) {
                                // High surrogate: a low surrogate must follow.
                                if self.bytes[self.pos..].starts_with(b"\\u") {
                                    self.pos += 2;
                                    let low = self.hex4()?;
                                    if !(0xDC00..0xE000).contains(&low) {
                                        return Err(self.error("invalid surrogate pair"));
                                    }
                                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00);
                                } else {
                                    return Err(self.error("lone high surrogate"));
                                }
                            }
                            match char::from_u32(code) {
                                Some(c) => out.push(c),
                                None => return Err(self.error("invalid unicode escape")),
                            }
                        }
                        _ => return Err(self.error("invalid escape")),
                    }
                }
                Some(_) => return Err(self.error("control character in string")),
            }
        }
    }

    fn number(&mut self) -> Result<Json, ParseError> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        let mut is_float = false;
        match self.peek() {
            Some(b'0') => self.pos += 1,
            Some(b'1'..=b'9') => {
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.pos += 1;
                }
            }
            _ => return Err(self.error("invalid number")),
        }
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.error("invalid number"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.error("invalid number"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos]).expect("ascii number");
        if !is_float && let Ok(i) = text.parse::<i64>() {
            return Ok(Json::Int(i));
        }
        text.parse::<f64>()
            .map(Json::Float)
            .map_err(|_| self.error("invalid number"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_a_typical_event_line() {
        let line = r#"{"id":"01K","ts":"2026-08-11T09:20:14.221Z","ticket":"a1b2c3","op":"set","actor":"user/x","set":{"status":"in_progress","priority":1,"labels+":["a","b"]}}"#;
        let value = parse(line).unwrap();
        assert_eq!(value.to_compact(), line);
        assert_eq!(
            value.get("set").unwrap().get("priority").unwrap().as_i64(),
            Some(1)
        );
    }

    #[test]
    fn escapes_and_unicode_survive() {
        let text = "quote\" back\\ nl\n tab\t ctrl\u{01} café 🎉 \u{2028}";
        let encoded = Json::Str(text.to_string()).to_compact();
        assert!(encoded.contains("\\u0001"));
        assert!(encoded.contains("🎉"));
        assert_eq!(parse(&encoded).unwrap(), Json::Str(text.to_string()));
        // Surrogate pairs decode to one scalar.
        assert_eq!(
            parse(r#""\ud83c\udf89""#).unwrap(),
            Json::Str("🎉".to_string())
        );
    }

    #[test]
    fn pretty_matches_python_indent_two() {
        let value = Json::Obj(vec![
            ("a".into(), Json::Arr(vec![])),
            ("b".into(), Json::obj()),
            ("c".into(), Json::Arr(vec![Json::Int(1), Json::Null])),
            ("d".into(), Json::Float(2.0)),
            ("e".into(), Json::Float(0.125)),
        ]);
        assert_eq!(
            value.to_pretty(),
            "{\n  \"a\": [],\n  \"b\": {},\n  \"c\": [\n    1,\n    null\n  ],\n  \"d\": 2.0,\n  \"e\": 0.125\n}"
        );
    }

    #[test]
    fn rejects_garbage() {
        for bad in [
            "",
            "{",
            "[1,]",
            "{\"a\" 1}",
            "tru",
            "\"\\x\"",
            "01",
            "1 2",
            "\"\n\"",
        ] {
            assert!(parse(bad).is_err(), "accepted {bad:?}");
        }
    }
}
