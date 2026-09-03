//! The event envelope: one line of `log.jsonl`, the source of truth.
//!
//! The five required fields (`id`, `ts`, `ticket`, `op`, `actor`) are the
//! load-bearing envelope every event carries (spec §5.2). Op-dependent payloads
//! (`set` for field updates, `text` for comments, `reason` for close reasons)
//! are optional and omitted from the line when absent. Keys this version does
//! not know are kept verbatim in `extra`, so `rp log --json` and compaction
//! round-trip a log written by an older or newer version byte-for-byte.
//!
//! Ops: `create`, `set`, `comment` are the live set. `link`, `unlink` and
//! `synced` were written by the removed sync layer; they still decode (old
//! logs must keep folding) and the fold skips their payload.

use crate::json::{self, Json, Key};

/// Ops the decoder accepts. The first three are live; the rest are legacy.
pub const KNOWN_OPS: &[&str] = &["create", "set", "comment", "link", "unlink", "synced"];

/// The reserved ticket the legacy sync layer used for its watermark events.
pub const SYNC_TICKET: &str = "__sync__";

#[derive(Debug, Clone, PartialEq)]
pub struct Event {
    pub id: String,
    pub ts: String,
    pub ticket: String,
    pub op: String,
    pub actor: String,
    /// `create`/`set` payload: field → value, in written order.
    pub set: Option<Vec<(Key, Json)>>,
    /// `comment` payload.
    pub text: Option<String>,
    /// Close/drop reason riding on a terminal `set` event.
    pub reason: Option<String>,
    /// Unknown keys, preserved verbatim (e.g. the legacy `remote`/`ref`/`at`).
    pub extra: Vec<(Key, Json)>,
}

impl Event {
    /// Serialise to one compact JSON line (no trailing newline).
    pub fn encode(&self) -> String {
        self.to_json().to_compact()
    }

    /// The event as a JSON object, keys in canonical order.
    pub fn to_json(&self) -> Json {
        let mut pairs: Vec<(Key, Json)> = vec![
            ("id".into(), json::s(&self.id)),
            ("ts".into(), json::s(&self.ts)),
            ("ticket".into(), json::s(&self.ticket)),
            ("op".into(), json::s(&self.op)),
            ("actor".into(), json::s(&self.actor)),
        ];
        if let Some(set) = &self.set {
            pairs.push(("set".into(), Json::Obj(set.clone())));
        }
        if let Some(text) = &self.text {
            pairs.push(("text".into(), json::s(text)));
        }
        pairs.extend(self.extra.iter().cloned());
        if let Some(reason) = &self.reason {
            pairs.push(("reason".into(), json::s(reason)));
        }
        Json::Obj(pairs)
    }

    /// A string value from the `extra` bag (legacy `remote`, for `rp log`).
    pub fn extra_str(&self, key: &str) -> Option<&str> {
        self.extra
            .iter()
            .find(|(k, _)| k.as_ref() == key)
            .and_then(|(_, v)| v.as_str())
    }
}

/// Decode one JSONL line. Errors name the problem so `rp doctor` can report it.
pub fn decode_line(line: &str) -> Result<Event, String> {
    let value = json::parse(line).map_err(|e| format!("invalid JSON ({e})"))?;
    let Json::Obj(pairs) = value else {
        return Err("event line is not a JSON object".to_string());
    };
    let mut event = Event {
        id: String::new(),
        ts: String::new(),
        ticket: String::new(),
        op: String::new(),
        actor: String::new(),
        set: None,
        text: None,
        reason: None,
        extra: Vec::new(),
    };
    let mut seen = [false; 5];
    let required = |key: &str, value: Json| -> Result<String, String> {
        match value {
            Json::Str(s) => Ok(s),
            other => Err(format!(
                "field '{key}' must be a string, got {}",
                kind(&other)
            )),
        }
    };
    let optional = |key: &str, value: Json| -> Result<Option<String>, String> {
        match value {
            Json::Null => Ok(None),
            Json::Str(s) => Ok(Some(s)),
            other => Err(format!(
                "field '{key}' must be a string or null, got {}",
                kind(&other)
            )),
        }
    };
    for (key, value) in pairs {
        match key.as_ref() {
            "id" => {
                event.id = required("id", value)?;
                seen[0] = true;
            }
            "ts" => {
                event.ts = required("ts", value)?;
                seen[1] = true;
            }
            "ticket" => {
                event.ticket = required("ticket", value)?;
                seen[2] = true;
            }
            "op" => {
                event.op = required("op", value)?;
                seen[3] = true;
            }
            "actor" => {
                event.actor = required("actor", value)?;
                seen[4] = true;
            }
            "set" => {
                event.set = match value {
                    Json::Null => None,
                    Json::Obj(pairs) => Some(pairs),
                    other => {
                        return Err(format!(
                            "field 'set' must be an object, got {}",
                            kind(&other)
                        ));
                    }
                }
            }
            "text" => event.text = optional("text", value)?,
            "reason" => event.reason = optional("reason", value)?,
            _ => event.extra.push((key, value)),
        }
    }
    if let Some(missing) = ["id", "ts", "ticket", "op", "actor"]
        .iter()
        .zip(seen)
        .find(|(_, s)| !*s)
    {
        return Err(format!("missing required field '{}'", missing.0));
    }
    if !KNOWN_OPS.contains(&event.op.as_str()) {
        return Err(format!("unknown op '{}'", event.op));
    }
    Ok(event)
}

fn kind(value: &Json) -> &'static str {
    match value {
        Json::Null => "null",
        Json::Bool(_) => "boolean",
        Json::Int(_) | Json::Float(_) => "number",
        Json::Str(_) => "string",
        Json::Arr(_) => "array",
        Json::Obj(_) => "object",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_encode_round_trips_live_and_legacy_lines() {
        let live = r#"{"id":"01K","ts":"2026-08-11T09:20:14.221Z","ticket":"a1b2c3","op":"set","actor":"user/x","set":{"status":"done"},"reason":"shipped"}"#;
        let legacy = r#"{"id":"01L","ts":"2026-08-11T09:20:14.221Z","ticket":"a1b2c3","op":"link","actor":"user/x","remote":"github","ref":"42"}"#;
        for line in [live, legacy] {
            let event = decode_line(line).unwrap();
            assert_eq!(event.encode(), line);
        }
        assert_eq!(
            decode_line(legacy).unwrap().extra_str("remote"),
            Some("github")
        );
    }

    #[test]
    fn rejects_broken_envelopes() {
        let base = r#"{"id":"01K","ts":"t","ticket":"a1b2c3","op":"set","actor":"u"}"#;
        assert!(decode_line(base).is_ok());
        assert!(
            decode_line(&base.replace(r#""actor":"u""#, r#""actor":1"#))
                .unwrap_err()
                .contains("actor")
        );
        assert!(
            decode_line(&base.replace(r#""op":"set""#, r#""op":"explode""#))
                .unwrap_err()
                .contains("unknown op")
        );
        assert!(
            decode_line(&base.replace(r#""ts":"t","#, ""))
                .unwrap_err()
                .contains("missing")
        );
        assert!(decode_line("[1]").is_err());
        assert!(decode_line("{").unwrap_err().contains("invalid JSON"));
    }
}
