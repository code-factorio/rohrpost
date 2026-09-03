//! A TOML reader covering what `config.toml` and ticket templates need.
//!
//! Supported: comments, `[tables]` and dotted keys, bare/quoted keys, basic and
//! literal strings (single- and multi-line), integers (decimal, `0x`, `0o`,
//! `0b`, underscores), floats, booleans, arrays and inline tables. Not
//! supported, and reported as an error: arrays of tables (`[[x]]`) and
//! date/time values. Rohrpost never writes TOML beyond the config template, so
//! a reader for this subset keeps the tool dependency-free without surprising
//! anyone who hand-edits a template.

use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq)]
pub enum Toml {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
    Array(Vec<Toml>),
    Table(BTreeMap<String, Toml>),
}

impl Toml {
    pub fn as_table(&self) -> Option<&BTreeMap<String, Toml>> {
        match self {
            Toml::Table(t) => Some(t),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Toml::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_int(&self) -> Option<i64> {
        match self {
            Toml::Int(i) => Some(*i),
            _ => None,
        }
    }

    /// Human name of the value's type, for error messages.
    pub fn type_name(&self) -> &'static str {
        match self {
            Toml::Str(_) => "string",
            Toml::Int(_) => "integer",
            Toml::Float(_) => "float",
            Toml::Bool(_) => "boolean",
            Toml::Array(_) => "array",
            Toml::Table(_) => "table",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TomlError {
    pub line: usize,
    pub message: String,
}

impl std::fmt::Display for TomlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "line {}: {}", self.line, self.message)
    }
}

/// Parse a whole TOML document into its root table.
pub fn parse(text: &str) -> Result<BTreeMap<String, Toml>, TomlError> {
    let mut parser = Parser {
        chars: text.chars().collect(),
        pos: 0,
        line: 1,
    };
    let mut root = BTreeMap::new();
    let mut current: Vec<String> = Vec::new();
    loop {
        parser.skip_ws_and_comments_and_newlines();
        match parser.peek() {
            None => return Ok(root),
            Some('[') => {
                if parser.peek_at(1) == Some('[') {
                    return Err(parser.error("arrays of tables ([[...]]) are not supported"));
                }
                parser.pos += 1;
                parser.skip_ws();
                current = parser.key_path()?;
                parser.skip_ws();
                parser.expect(']')?;
                // Declaring a table creates it (possibly empty).
                let _ = table_at(&mut root, &current, &parser)?;
                parser.end_of_line()?;
            }
            Some(_) => {
                let path = parser.key_path()?;
                parser.skip_ws();
                parser.expect('=')?;
                parser.skip_ws();
                let value = parser.value()?;
                let (last, prefix) = path.split_last().expect("key path is non-empty");
                let mut full = current.clone();
                full.extend(prefix.iter().cloned());
                let table = table_at(&mut root, &full, &parser)?;
                if table.contains_key(last) {
                    return Err(parser.error(&format!("duplicate key {last:?}")));
                }
                table.insert(last.clone(), value);
                parser.end_of_line()?;
            }
        }
    }
}

/// Walk (creating as needed) to the table at `path`.
fn table_at<'a>(
    root: &'a mut BTreeMap<String, Toml>,
    path: &[String],
    parser: &Parser,
) -> Result<&'a mut BTreeMap<String, Toml>, TomlError> {
    let mut table = root;
    for segment in path {
        let entry = table
            .entry(segment.clone())
            .or_insert_with(|| Toml::Table(BTreeMap::new()));
        table = match entry {
            Toml::Table(t) => t,
            other => {
                return Err(parser.error(&format!(
                    "key {segment:?} is a {}, not a table",
                    other.type_name()
                )));
            }
        };
    }
    Ok(table)
}

struct Parser {
    chars: Vec<char>,
    pos: usize,
    line: usize,
}

impl Parser {
    fn error(&self, message: &str) -> TomlError {
        TomlError {
            line: self.line,
            message: message.to_string(),
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn peek_at(&self, offset: usize) -> Option<char> {
        self.chars.get(self.pos + offset).copied()
    }

    fn bump(&mut self) -> Option<char> {
        let c = self.peek()?;
        self.pos += 1;
        if c == '\n' {
            self.line += 1;
        }
        Some(c)
    }

    fn expect(&mut self, c: char) -> Result<(), TomlError> {
        if self.peek() == Some(c) {
            self.bump();
            Ok(())
        } else {
            Err(self.error(&format!("expected '{c}'")))
        }
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(' ' | '\t')) {
            self.pos += 1;
        }
    }

    fn skip_comment(&mut self) {
        if self.peek() == Some('#') {
            while !matches!(self.peek(), None | Some('\n')) {
                self.pos += 1;
            }
        }
    }

    fn skip_ws_and_comments_and_newlines(&mut self) {
        loop {
            self.skip_ws();
            self.skip_comment();
            match self.peek() {
                Some('\n') => {
                    self.bump();
                }
                Some('\r') if self.peek_at(1) == Some('\n') => {
                    self.pos += 1;
                    self.bump();
                }
                _ => return,
            }
        }
    }

    /// After a key/value or table header: optional whitespace, comment, newline or EOF.
    fn end_of_line(&mut self) -> Result<(), TomlError> {
        self.skip_ws();
        self.skip_comment();
        match self.peek() {
            None => Ok(()),
            Some('\n') => {
                self.bump();
                Ok(())
            }
            Some('\r') if self.peek_at(1) == Some('\n') => {
                self.pos += 1;
                self.bump();
                Ok(())
            }
            Some(_) => Err(self.error("expected newline after value")),
        }
    }

    fn key_path(&mut self) -> Result<Vec<String>, TomlError> {
        let mut path = vec![self.simple_key()?];
        loop {
            self.skip_ws();
            if self.peek() == Some('.') {
                self.bump();
                self.skip_ws();
                path.push(self.simple_key()?);
            } else {
                return Ok(path);
            }
        }
    }

    fn simple_key(&mut self) -> Result<String, TomlError> {
        match self.peek() {
            Some('"') => self.basic_string(),
            Some('\'') => self.literal_string(),
            Some(c) if is_bare_key_char(c) => {
                let start = self.pos;
                while matches!(self.peek(), Some(c) if is_bare_key_char(c)) {
                    self.pos += 1;
                }
                Ok(self.chars[start..self.pos].iter().collect())
            }
            _ => Err(self.error("expected a key")),
        }
    }

    fn value(&mut self) -> Result<Toml, TomlError> {
        match self.peek() {
            None => Err(self.error("expected a value")),
            Some('"') => {
                if self.peek_at(1) == Some('"') && self.peek_at(2) == Some('"') {
                    self.multiline_basic_string().map(Toml::Str)
                } else {
                    self.basic_string().map(Toml::Str)
                }
            }
            Some('\'') => {
                if self.peek_at(1) == Some('\'') && self.peek_at(2) == Some('\'') {
                    self.multiline_literal_string().map(Toml::Str)
                } else {
                    self.literal_string().map(Toml::Str)
                }
            }
            Some('[') => self.array(),
            Some('{') => self.inline_table(),
            Some('t') | Some('f') => self.boolean(),
            Some(c) if c == '+' || c == '-' || c.is_ascii_digit() || c == 'i' || c == 'n' => {
                self.number()
            }
            Some(_) => Err(self.error("unsupported value")),
        }
    }

    fn boolean(&mut self) -> Result<Toml, TomlError> {
        for (word, value) in [("true", true), ("false", false)] {
            let candidate: String = self.chars[self.pos..].iter().take(word.len()).collect();
            if candidate == word {
                self.pos += word.len();
                return Ok(Toml::Bool(value));
            }
        }
        Err(self.error("invalid boolean"))
    }

    fn number(&mut self) -> Result<Toml, TomlError> {
        let start = self.pos;
        while matches!(self.peek(), Some(c) if c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '_' | '.'))
        {
            self.pos += 1;
        }
        let raw: String = self.chars[start..self.pos].iter().collect();
        if raw.contains(':') || (raw.matches('-').count() >= 2 && !raw.starts_with('-')) {
            return Err(self.error("date/time values are not supported"));
        }
        let cleaned = raw.replace('_', "");
        let (negative, body) = match cleaned.strip_prefix('-') {
            Some(rest) => (true, rest.to_string()),
            None => (
                false,
                cleaned.strip_prefix('+').unwrap_or(&cleaned).to_string(),
            ),
        };
        let radix_parse = |digits: &str, radix: u32| i64::from_str_radix(digits, radix).ok();
        let int = if let Some(hex) = body.strip_prefix("0x") {
            radix_parse(hex, 16)
        } else if let Some(oct) = body.strip_prefix("0o") {
            radix_parse(oct, 8)
        } else if let Some(bin) = body.strip_prefix("0b") {
            radix_parse(bin, 2)
        } else if body.chars().all(|c| c.is_ascii_digit()) && !body.is_empty() {
            body.parse::<i64>().ok()
        } else {
            None
        };
        if let Some(i) = int {
            return Ok(Toml::Int(if negative { -i } else { i }));
        }
        match body.as_str() {
            "inf" => {
                return Ok(Toml::Float(if negative {
                    f64::NEG_INFINITY
                } else {
                    f64::INFINITY
                }));
            }
            "nan" => return Ok(Toml::Float(f64::NAN)),
            _ => {}
        }
        if body
            .chars()
            .all(|c| c.is_ascii_digit() || matches!(c, '.' | 'e' | 'E' | '+' | '-'))
            && let Ok(f) = body.parse::<f64>()
        {
            return Ok(Toml::Float(if negative { -f } else { f }));
        }
        Err(self.error(&format!("invalid number {raw:?}")))
    }

    fn array(&mut self) -> Result<Toml, TomlError> {
        self.expect('[')?;
        let mut items = Vec::new();
        loop {
            self.skip_ws_and_comments_and_newlines();
            match self.peek() {
                None => return Err(self.error("unterminated array")),
                Some(']') => {
                    self.bump();
                    return Ok(Toml::Array(items));
                }
                Some(_) => {
                    items.push(self.value()?);
                    self.skip_ws_and_comments_and_newlines();
                    match self.peek() {
                        Some(',') => {
                            self.bump();
                        }
                        Some(']') => {}
                        _ => return Err(self.error("expected ',' or ']' in array")),
                    }
                }
            }
        }
    }

    fn inline_table(&mut self) -> Result<Toml, TomlError> {
        self.expect('{')?;
        let mut table = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some('}') {
            self.bump();
            return Ok(Toml::Table(table));
        }
        loop {
            self.skip_ws();
            let path = self.key_path()?;
            self.skip_ws();
            self.expect('=')?;
            self.skip_ws();
            let value = self.value()?;
            let (last, prefix) = path.split_last().expect("non-empty");
            let target = table_at(&mut table, prefix, self)?;
            if target.contains_key(last) {
                return Err(self.error(&format!("duplicate key {last:?}")));
            }
            target.insert(last.clone(), value);
            self.skip_ws();
            match self.bump() {
                Some(',') => {}
                Some('}') => return Ok(Toml::Table(table)),
                _ => return Err(self.error("expected ',' or '}' in inline table")),
            }
        }
    }

    fn unicode_escape(&mut self, len: usize) -> Result<char, TomlError> {
        let digits: String = self.chars[self.pos..].iter().take(len).collect();
        if digits.len() != len {
            return Err(self.error("truncated unicode escape"));
        }
        self.pos += len;
        u32::from_str_radix(&digits, 16)
            .ok()
            .and_then(char::from_u32)
            .ok_or_else(|| self.error("invalid unicode escape"))
    }

    fn escape(&mut self) -> Result<char, TomlError> {
        match self.bump() {
            Some('b') => Ok('\u{08}'),
            Some('t') => Ok('\t'),
            Some('n') => Ok('\n'),
            Some('f') => Ok('\u{0C}'),
            Some('r') => Ok('\r'),
            Some('"') => Ok('"'),
            Some('\\') => Ok('\\'),
            Some('u') => self.unicode_escape(4),
            Some('U') => self.unicode_escape(8),
            _ => Err(self.error("invalid escape sequence")),
        }
    }

    fn basic_string(&mut self) -> Result<String, TomlError> {
        self.expect('"')?;
        let mut out = String::new();
        loop {
            match self.bump() {
                None | Some('\n') => return Err(self.error("unterminated string")),
                Some('"') => return Ok(out),
                Some('\\') => out.push(self.escape()?),
                Some(c) => out.push(c),
            }
        }
    }

    fn literal_string(&mut self) -> Result<String, TomlError> {
        self.expect('\'')?;
        let mut out = String::new();
        loop {
            match self.bump() {
                None | Some('\n') => return Err(self.error("unterminated literal string")),
                Some('\'') => return Ok(out),
                Some(c) => out.push(c),
            }
        }
    }

    fn skip_opening_newline(&mut self) {
        // A newline immediately after the opening delimiter is trimmed.
        if self.peek() == Some('\r') && self.peek_at(1) == Some('\n') {
            self.pos += 1;
        }
        if self.peek() == Some('\n') {
            self.bump();
        }
    }

    fn multiline_basic_string(&mut self) -> Result<String, TomlError> {
        self.pos += 3;
        self.skip_opening_newline();
        let mut out = String::new();
        loop {
            match self.bump() {
                None => return Err(self.error("unterminated multi-line string")),
                Some('"') if self.peek() == Some('"') && self.peek_at(1) == Some('"') => {
                    self.pos += 2;
                    // Up to two extra quotes may precede the closing delimiter.
                    while self.peek() == Some('"') && out.len() < usize::MAX {
                        out.push('"');
                        self.pos += 1;
                    }
                    return Ok(out);
                }
                Some('\\') => {
                    if matches!(self.peek(), Some(' ' | '\t' | '\n' | '\r')) {
                        // Line-ending backslash: skip whitespace and newlines.
                        while matches!(self.peek(), Some(' ' | '\t' | '\n' | '\r')) {
                            self.bump();
                        }
                    } else {
                        out.push(self.escape()?);
                    }
                }
                Some('\r') if self.peek() == Some('\n') => {}
                Some(c) => out.push(c),
            }
        }
    }

    fn multiline_literal_string(&mut self) -> Result<String, TomlError> {
        self.pos += 3;
        self.skip_opening_newline();
        let mut out = String::new();
        loop {
            match self.bump() {
                None => return Err(self.error("unterminated multi-line literal string")),
                Some('\'') if self.peek() == Some('\'') && self.peek_at(1) == Some('\'') => {
                    self.pos += 2;
                    while self.peek() == Some('\'') {
                        out.push('\'');
                        self.pos += 1;
                    }
                    return Ok(out);
                }
                Some('\r') if self.peek() == Some('\n') => {}
                Some(c) => out.push(c),
            }
        }
    }
}

fn is_bare_key_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '-'
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_config_and_template_shapes() {
        let doc = "# comment\n[project]\nprefix = \"FAC\" # trailing\ndefault_branch = 'main'\n\n[defaults]\ntype = \"bug\"\npriority = 1\nlabels = [\"auth\",\n  \"ui\",\n]\nbody = \"\"\"\nline one\nline \"two\"\n\"\"\"\nmeta = { nested.flag = true, n = -0x1F }\n";
        let root = parse(doc).unwrap();
        let project = root["project"].as_table().unwrap();
        assert_eq!(project["prefix"].as_str(), Some("FAC"));
        assert_eq!(project["default_branch"].as_str(), Some("main"));
        let defaults = root["defaults"].as_table().unwrap();
        assert_eq!(defaults["priority"].as_int(), Some(1));
        assert_eq!(
            defaults["labels"],
            Toml::Array(vec![Toml::Str("auth".into()), Toml::Str("ui".into())])
        );
        assert_eq!(defaults["body"].as_str(), Some("line one\nline \"two\"\n"));
        let meta = defaults["meta"].as_table().unwrap();
        assert_eq!(meta["nested"].as_table().unwrap()["flag"], Toml::Bool(true));
        assert_eq!(meta["n"].as_int(), Some(-31));
    }

    #[test]
    fn dotted_keys_and_crlf_and_escapes() {
        let doc = "a.b = \"x\\ty\\u00e9\"\r\nc = 1_000\r\n[t]\r\nd = 2.5\r\n";
        let root = parse(doc).unwrap();
        assert_eq!(root["a"].as_table().unwrap()["b"].as_str(), Some("x\tyé"));
        assert_eq!(root["c"].as_int(), Some(1000));
        assert_eq!(root["t"].as_table().unwrap()["d"], Toml::Float(2.5));
    }

    #[test]
    fn rejects_what_it_does_not_support() {
        assert!(parse("[[items]]\na = 1\n").is_err());
        assert!(parse("when = 2026-08-11T09:00:00Z\n").is_err());
        assert!(parse("a = 1\na = 2\n").is_err());
        assert!(parse("a = \"unterminated\n").is_err());
        assert!(parse("a = 1 b = 2\n").is_err());
    }
}
