//! The fold: turning the append-only event log into tickets (spec §6).
//!
//! 1. Read all events (archive then live log).
//! 2. **Deduplicate by event id** — a `merge=union` of the log can produce
//!    duplicate lines and every event id is unique, so dedupe is exact.
//! 3. **Sort by `(ts, id)`** — the ULID tiebreak makes the order total.
//! 4. Replay in order, applying each op's payload **field by field** and
//!    recording `fieldts[field] = ts`.
//! 5. **Last write wins, per field** — not per record. Two runners updating
//!    `status` and `priority` concurrently both win.
//!
//! Set fields (`labels`, `blocked_by`) fold `<field>+`/`<field>-` payload keys
//! as set union/difference so concurrent labelling composes. Readiness and epic
//! status are derived at query time and never stored.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use crate::error::Result;
use crate::events::{Event, SYNC_TICKET};
use crate::ids::bare_slice;
use crate::json::{self, Json, Key};
use crate::store;

/// Stored status values (§5.4). `ready` is deliberately absent: it is derived.
pub const STATUSES: &[&str] = &[
    "open",
    "in_progress",
    "review",
    "waiting",
    "done",
    "dropped",
];
/// Terminal statuses: the work is finished one way or the other.
pub const TERMINAL: &[&str] = &["done", "dropped"];
/// Ticket types (§5.3).
pub const TYPES: &[&str] = &["task", "bug", "spike", "epic"];
/// Whole-value fields updated by per-field last-write-wins.
pub const SCALAR_FIELDS: &[&str] = &[
    "title", "type", "status", "priority", "assignee", "parent", "body",
];
/// Array fields updated by `<field>+` / `<field>-` set ops.
pub const SET_FIELDS: &[&str] = &["labels", "blocked_by"];

pub const DEFAULT_TYPE: &str = "task";
pub const DEFAULT_STATUS: &str = "open";
pub const DEFAULT_PRIORITY: i64 = 2;

/// A local note appended to a ticket (spec §9).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Comment {
    pub ts: String,
    pub actor: String,
    pub text: String,
}

/// The folded shape of a ticket (spec §5.3). Ids are **bare**; the display
/// prefix is applied only at the output layer.
#[derive(Debug, Clone, PartialEq)]
pub struct Ticket {
    pub id: String,
    pub title: String,
    pub kind: String,
    pub status: String,
    pub priority: i64,
    pub parent: Option<String>,
    pub blocked_by: Vec<String>,
    pub labels: Vec<String>,
    pub assignee: Option<String>,
    pub body: Option<String>,
    pub last_close_reason: Option<String>,
    pub comments: Vec<Comment>,
    pub created: String,
    pub updated: String,
    pub fieldts: BTreeMap<&'static str, String>,
}

/// Folded tickets keyed by bare id.
pub type Tickets = BTreeMap<String, Ticket>;

/// Mutable per-ticket accumulator used while replaying the log. It borrows
/// timestamps from the events and only allocates when the ticket is frozen.
#[derive(Default)]
struct Builder<'a> {
    title: Option<String>,
    kind: Option<String>,
    status: Option<String>,
    priority: Option<i64>,
    parent: Option<String>,
    assignee: Option<String>,
    body: Option<String>,
    labels: BTreeSet<String>,
    blocked_by: BTreeSet<String>,
    last_close_reason: Option<String>,
    comments: Vec<Comment>,
    created: Option<&'a str>,
    updated: Option<&'a str>,
    fieldts: BTreeMap<&'static str, &'a str>,
}

impl Builder<'_> {
    fn freeze(self, id: String) -> Ticket {
        Ticket {
            id,
            title: self.title.unwrap_or_default(),
            kind: self.kind.unwrap_or_else(|| DEFAULT_TYPE.to_string()),
            status: self.status.unwrap_or_else(|| DEFAULT_STATUS.to_string()),
            priority: self.priority.unwrap_or(DEFAULT_PRIORITY),
            parent: self.parent,
            blocked_by: self.blocked_by.into_iter().collect(),
            labels: self.labels.into_iter().collect(),
            assignee: self.assignee,
            body: self.body,
            last_close_reason: self.last_close_reason,
            comments: self.comments,
            created: self.created.unwrap_or_default().to_string(),
            updated: self.updated.unwrap_or_default().to_string(),
            fieldts: self
                .fieldts
                .into_iter()
                .map(|(k, v)| (k, v.to_string()))
                .collect(),
        }
    }

    fn set_field(&mut self, field: &str) -> &mut BTreeSet<String> {
        if field == "labels" {
            &mut self.labels
        } else {
            &mut self.blocked_by
        }
    }
}

/// Normalise an id to bare form (borrowed); an unparseable value is returned
/// unchanged so `rp doctor` can flag it as a dangling reference.
pub fn bare_id(value: &str) -> &str {
    bare_slice(value).unwrap_or(value)
}

/// The `&'static str` for a known field name, so `fieldts` keys never allocate.
fn static_field(name: &str) -> Option<&'static str> {
    SCALAR_FIELDS
        .iter()
        .chain(SET_FIELDS)
        .find(|f| **f == name)
        .copied()
}

fn json_as_string(value: &Json) -> String {
    match value {
        Json::Str(s) => s.clone(),
        other => other.to_compact(),
    }
}

/// Coerce a set-op payload to strings; `blocked_by` values are ticket ids.
fn set_values(field: &str, value: &Json) -> Vec<String> {
    let items: Vec<String> = match value {
        Json::Arr(items) => items.iter().map(json_as_string).collect(),
        other => vec![json_as_string(other)],
    };
    if field == "blocked_by" {
        items.iter().map(|s| bare_id(s).to_string()).collect()
    } else {
        items
    }
}

fn apply_scalar<'a>(b: &mut Builder<'a>, key: &str, value: &Json, ts: &'a str) {
    if key == "priority" {
        // Malformed priorities are skipped so the fold stays total.
        let parsed = match value {
            Json::Int(i) => Some(*i),
            Json::Float(f) if f.is_finite() => Some(f.trunc() as i64),
            Json::Str(s) => s.trim().parse::<i64>().ok(),
            _ => None,
        };
        if let Some(p) = parsed {
            b.priority = Some(p);
            if let Some(field) = static_field(key) {
                b.fieldts.insert(field, ts);
            }
        }
        return;
    }
    let text = match value {
        Json::Str(s) => Some(s.clone()),
        Json::Null => None,
        _ => return, // wrong type: ignored, the fold stays total
    };
    let text = match key {
        "parent" => text.map(|s| bare_id(&s).to_string()),
        // An empty body means no body.
        "body" => text.filter(|s| !s.is_empty()),
        _ => text,
    };
    match key {
        "title" => b.title = text,
        "type" => b.kind = text,
        "status" => b.status = text,
        "assignee" => b.assignee = text,
        "parent" => b.parent = text,
        "body" => b.body = text,
        _ => return,
    }
    if let Some(field) = static_field(key) {
        b.fieldts.insert(field, ts);
    }
}

/// Apply a `create`/`set` payload field by field (per-field LWW by order).
fn apply_set<'a>(b: &mut Builder<'a>, payload: &[(Key, Json)], ts: &'a str, reason: Option<&str>) {
    for (key, value) in payload {
        if let Some(name) = key.strip_suffix('+') {
            if let Some(field) = static_field(name).filter(|f| SET_FIELDS.contains(f)) {
                b.set_field(field).extend(set_values(field, value));
                b.fieldts.insert(field, ts);
            }
        } else if let Some(name) = key.strip_suffix('-') {
            if let Some(field) = static_field(name).filter(|f| SET_FIELDS.contains(f)) {
                for item in set_values(field, value) {
                    b.set_field(field).remove(&item);
                }
                b.fieldts.insert(field, ts);
            }
        } else if SCALAR_FIELDS.contains(&key.as_ref()) {
            apply_scalar(b, key, value, ts);
            if key == "status" && value.as_str().is_some_and(|s| TERMINAL.contains(&s)) {
                b.last_close_reason = reason.map(str::to_string);
            }
        }
        // Unknown keys are ignored: a future field must not crash older code.
    }
}

fn apply_event<'a>(b: &mut Builder<'a>, ev: &'a Event) {
    match ev.op.as_str() {
        "create" | "set" => {
            if let Some(payload) = &ev.set {
                apply_set(b, payload, &ev.ts, ev.reason.as_deref());
            }
        }
        "comment" => {
            if let Some(text) = &ev.text {
                b.comments.push(Comment {
                    ts: ev.ts.clone(),
                    actor: ev.actor.clone(),
                    text: text.clone(),
                });
            }
        }
        // Legacy sync ops carry no ticket field state any more.
        _ => {}
    }
}

/// Deduplicate by id, then sort by `(ts, id)` (a total, deterministic order).
pub fn dedup_sort(events: &[Event]) -> Vec<&Event> {
    let mut seen: HashSet<&str> = HashSet::with_capacity(events.len());
    let mut unique: Vec<&Event> = events
        .iter()
        .filter(|e| seen.insert(e.id.as_str()))
        .collect();
    unique.sort_by(|a, b| (a.ts.as_str(), a.id.as_str()).cmp(&(b.ts.as_str(), b.id.as_str())));
    unique
}

/// Fold events into tickets. Input need not be sorted or de-duplicated.
pub fn fold(events: &[Event]) -> Tickets {
    let mut builders: HashMap<&str, Builder> = HashMap::new();
    let mut order: Vec<&str> = Vec::new();
    for ev in dedup_sort(events) {
        if ev.op == "synced" || ev.ticket == SYNC_TICKET {
            continue;
        }
        let tid = bare_id(&ev.ticket);
        let b = builders.entry(tid).or_insert_with(|| {
            order.push(tid);
            Builder::default()
        });
        if b.created.is_none() {
            b.created = Some(ev.ts.as_str());
        }
        b.updated = Some(ev.ts.as_str());
        apply_event(b, ev);
    }
    order
        .into_iter()
        .map(|tid| {
            let b = builders.remove(tid).expect("builder for every id");
            (tid.to_string(), b.freeze(tid.to_string()))
        })
        .collect()
}

/// Read + fold the whole log.
pub fn load_tickets(rohrpost_dir: &Path) -> Result<Tickets> {
    Ok(fold(&store::read_events(rohrpost_dir)?))
}

// ---------------------------------------------------------------------------
// Derived views (§5.4 readiness, §5.5 epics). Never stored.
// ---------------------------------------------------------------------------
/// Stored status, except for epics with children: `done` when every child is
/// `done`, otherwise `open`.
pub fn derive_status(ticket: &Ticket, by_id: &Tickets) -> String {
    if ticket.kind != "epic" {
        return ticket.status.clone();
    }
    let mut children = by_id
        .values()
        .filter(|c| c.parent.as_deref() == Some(&ticket.id))
        .peekable();
    if children.peek().is_none() {
        return ticket.status.clone();
    }
    if children.all(|c| c.status == "done") {
        "done".into()
    } else {
        "open".into()
    }
}

/// Actionable now: `open`, not an epic, every `blocked_by` is `done`.
pub fn is_ready(ticket: &Ticket, by_id: &Tickets) -> bool {
    ticket.kind != "epic"
        && ticket.status == "open"
        && ticket
            .blocked_by
            .iter()
            .all(|dep| by_id.get(dep).is_some_and(|b| b.status == "done"))
}

/// A cyclic path of bare ids along `blocked_by` edges, or `None` if acyclic.
pub fn find_cycle(by_id: &Tickets) -> Option<Vec<String>> {
    #[derive(Clone, Copy, PartialEq)]
    enum Color {
        White,
        Gray,
        Black,
    }
    fn dfs<'a>(
        node: &'a str,
        by_id: &'a Tickets,
        color: &mut HashMap<&'a str, Color>,
        stack: &mut Vec<&'a str>,
    ) -> Option<Vec<String>> {
        color.insert(node, Color::Gray);
        stack.push(node);
        for dep in &by_id[node].blocked_by {
            match color.get(dep.as_str()) {
                None => continue, // dangling reference: doctor reports it separately
                Some(Color::Gray) => {
                    let start = stack.iter().position(|n| *n == dep.as_str()).unwrap_or(0);
                    let mut cycle: Vec<String> =
                        stack[start..].iter().map(|s| s.to_string()).collect();
                    cycle.push(dep.clone());
                    return Some(cycle);
                }
                Some(Color::White) => {
                    if let Some(found) = dfs(dep, by_id, color, stack) {
                        return Some(found);
                    }
                }
                Some(Color::Black) => {}
            }
        }
        stack.pop();
        color.insert(node, Color::Black);
        None
    }
    let mut color: HashMap<&str, Color> =
        by_id.keys().map(|k| (k.as_str(), Color::White)).collect();
    let mut stack = Vec::new();
    for tid in by_id.keys() {
        if color[tid.as_str()] == Color::White
            && let Some(cycle) = dfs(tid, by_id, &mut color, &mut stack)
        {
            return Some(cycle);
        }
    }
    None
}

/// Sort key for listings: highest priority first, then oldest, then id.
pub fn sort_tickets(tickets: &mut [&Ticket]) {
    tickets.sort_by(|a, b| {
        (a.priority, a.created.as_str(), a.id.as_str()).cmp(&(
            b.priority,
            b.created.as_str(),
            b.id.as_str(),
        ))
    });
}

// ---------------------------------------------------------------------------
// The `--json` shape (§5.3).
// ---------------------------------------------------------------------------
pub fn comment_to_json(comment: &Comment) -> Json {
    Json::Obj(vec![
        ("ts".into(), json::s(&comment.ts)),
        ("actor".into(), json::s(&comment.actor)),
        ("text".into(), json::s(&comment.text)),
    ])
}

/// Options for [`ticket_to_json`]: the full shape carries everything; the
/// short list/ready shape drops the body, comments and `_fieldts` so the
/// work-queue view never carries ticket prose into an agent's context.
#[derive(Debug, Clone, Copy)]
pub struct Shape {
    pub fieldts: bool,
    pub comments: bool,
    pub body: bool,
}

impl Shape {
    pub const FULL: Shape = Shape {
        fieldts: true,
        comments: true,
        body: true,
    };
    pub const SHORT: Shape = Shape {
        fieldts: false,
        comments: false,
        body: false,
    };
}

/// Convert a ticket to JSON, rendering ids with `prefix` when given.
pub fn ticket_to_json(ticket: &Ticket, prefix: Option<&str>, shape: Shape) -> Json {
    let rnd = |tid: &str| match prefix {
        Some(p) => format!("{p}-{tid}"),
        None => tid.to_string(),
    };
    let mut pairs: Vec<(Key, Json)> = vec![
        ("id".into(), json::s(rnd(&ticket.id))),
        ("title".into(), json::s(&ticket.title)),
        ("type".into(), json::s(&ticket.kind)),
        ("status".into(), json::s(&ticket.status)),
        ("priority".into(), Json::Int(ticket.priority)),
        (
            "parent".into(),
            ticket
                .parent
                .as_deref()
                .map(|p| json::s(rnd(p)))
                .unwrap_or(Json::Null),
        ),
        (
            "blocked_by".into(),
            json::str_list(ticket.blocked_by.iter().map(|b| rnd(b))),
        ),
        (
            "labels".into(),
            json::str_list(ticket.labels.iter().cloned()),
        ),
        ("assignee".into(), json::opt(ticket.assignee.as_deref())),
    ];
    if shape.body {
        pairs.push(("body".into(), json::opt(ticket.body.as_deref())));
    }
    pairs.push((
        "last_close_reason".into(),
        json::opt(ticket.last_close_reason.as_deref()),
    ));
    pairs.push(("created".into(), json::s(&ticket.created)));
    pairs.push(("updated".into(), json::s(&ticket.updated)));
    if shape.comments {
        pairs.push((
            "comments".into(),
            Json::Arr(ticket.comments.iter().map(comment_to_json).collect()),
        ));
    }
    if shape.fieldts {
        pairs.push((
            "_fieldts".into(),
            Json::Obj(
                ticket
                    .fieldts
                    .iter()
                    .map(|(k, v)| (Key::Borrowed(k), json::s(v)))
                    .collect(),
            ),
        ));
    }
    Json::Obj(pairs)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::decode_line;

    fn ev(id: &str, ts: &str, ticket: &str, op: &str, payload: &str) -> Event {
        let line = format!(
            r#"{{"id":"{id}","ts":"2026-01-01T00:00:{ts}Z","ticket":"{ticket}","op":"{op}","actor":"user/t"{payload}}}"#
        );
        decode_line(&line).unwrap()
    }

    #[test]
    fn dedupes_sorts_and_applies_per_field_lww() {
        let events = vec![
            ev(
                "B",
                "02.000",
                "a1b2c3",
                "set",
                r#","set":{"status":"in_progress"}"#,
            ),
            ev(
                "A",
                "01.000",
                "a1b2c3",
                "create",
                r#","set":{"title":"t","type":"task","status":"open","priority":2}"#,
            ),
            ev("C", "03.000", "a1b2c3", "set", r#","set":{"priority":0}"#),
            ev(
                "B",
                "02.000",
                "a1b2c3",
                "set",
                r#","set":{"status":"in_progress"}"#,
            ), // union-merge duplicate
        ];
        let tickets = fold(&events);
        let t = &tickets["a1b2c3"];
        assert_eq!((t.status.as_str(), t.priority), ("in_progress", 0));
        assert_eq!(t.created, "2026-01-01T00:00:01.000Z");
        assert_eq!(t.updated, "2026-01-01T00:00:03.000Z");
        assert_eq!(t.fieldts["status"], "2026-01-01T00:00:02.000Z");
        assert_eq!(t.fieldts["priority"], "2026-01-01T00:00:03.000Z");
    }

    #[test]
    fn set_ops_compose_and_close_reason_rides_on_the_event() {
        let events = vec![
            ev(
                "A",
                "01.000",
                "a1b2c3",
                "create",
                r#","set":{"title":"t","labels+":["b","a"],"blocked_by+":["TST-zzzzzz"]}"#,
            ),
            ev(
                "B",
                "02.000",
                "a1b2c3",
                "set",
                r#","set":{"labels+":["c"],"labels-":["a"]}"#,
            ),
            ev(
                "C",
                "03.000",
                "a1b2c3",
                "set",
                r#","set":{"status":"done"},"reason":"shipped""#,
            ),
            ev("D", "04.000", "a1b2c3", "comment", r#","text":"note""#),
            ev(
                "E",
                "05.000",
                "a1b2c3",
                "link",
                r#","remote":"github","ref":"1""#,
            ),
            ev(
                "F",
                "06.000",
                "__sync__",
                "synced",
                r#","remote":"github","at":"x""#,
            ),
            ev(
                "G",
                "07.000",
                "a1b2c3",
                "set",
                r#","set":{"body":"","future_field":1,"priority":"x"}"#,
            ),
        ];
        let tickets = fold(&events);
        assert_eq!(tickets.len(), 1, "synced events create no ticket");
        let t = &tickets["a1b2c3"];
        assert_eq!(t.labels, vec!["b", "c"]);
        assert_eq!(t.blocked_by, vec!["zzzzzz"]);
        assert_eq!(t.last_close_reason.as_deref(), Some("shipped"));
        assert_eq!(t.comments.len(), 1);
        assert_eq!(t.updated, "2026-01-01T00:00:07.000Z");
        assert_eq!(t.body, None);
        assert_eq!(t.priority, DEFAULT_PRIORITY);
    }

    #[test]
    fn readiness_epic_status_and_cycles_are_derived() {
        let events = vec![
            ev(
                "A",
                "01.000",
                "epic01",
                "create",
                r#","set":{"title":"e","type":"epic"}"#,
            ),
            ev(
                "B",
                "02.000",
                "child1",
                "create",
                r#","set":{"title":"c1","parent":"epic01","status":"done"}"#,
            ),
            ev(
                "C",
                "03.000",
                "child2",
                "create",
                r#","set":{"title":"c2","parent":"epic01","blocked_by+":["child1"]}"#,
            ),
            ev(
                "D",
                "04.000",
                "child3",
                "create",
                r#","set":{"title":"c3","blocked_by+":["child2"]}"#,
            ),
        ];
        let tickets = fold(&events);
        assert_eq!(derive_status(&tickets["epic01"], &tickets), "open");
        assert!(!is_ready(&tickets["epic01"], &tickets));
        assert!(is_ready(&tickets["child2"], &tickets), "blocker is done");
        assert!(!is_ready(&tickets["child3"], &tickets), "blocker is open");
        assert_eq!(find_cycle(&tickets), None);

        let mut cyclic = events.clone();
        cyclic.push(ev(
            "E",
            "05.000",
            "child2",
            "set",
            r#","set":{"blocked_by+":["child3"]}"#,
        ));
        let cycle = find_cycle(&fold(&cyclic)).expect("cycle");
        assert_eq!(cycle.first(), cycle.last());
        assert!(cycle.contains(&"child3".to_string()));
    }
}
