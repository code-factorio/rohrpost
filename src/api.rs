//! High-level ticket operations: the library behind the `rp` commands.
//!
//! This is the *one write path* (spec §3 principle 3). Every mutation builds a
//! well-formed [`Event`], appends it through [`store`], and returns the
//! re-folded ticket. Mutations are idempotent (spec §9.2): each folds first,
//! drops assignments that would change nothing, and appends nothing when
//! nothing effective remains, so the log stays clean under retries.
//!
//! Functions take the `.rohrpost/` directory and return bare-id domain objects;
//! the display prefix is applied only by the CLI.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::config::{self, Config};
use crate::error::{Error, Result};
use crate::events::Event;
use crate::fold::{
    self, DEFAULT_PRIORITY, DEFAULT_TYPE, SET_FIELDS, STATUSES, TYPES, Ticket, Tickets,
    sort_tickets,
};
use crate::ids::{new_ticket_id, new_ulid, normalize_id};
use crate::json::{self, Json, Key};
use crate::paths;
use crate::store;
use crate::time::now_ts;
use crate::toml::{self, Toml};

/// Outcome of a mutation: the ticket and whether an event was appended.
/// `wrote == false` means the op was an idempotent no-op.
#[derive(Debug, Clone)]
pub struct WriteResult {
    pub ticket: Ticket,
    pub wrote: bool,
}

// ---------------------------------------------------------------------------
// Field assignments (`rp set`).
// ---------------------------------------------------------------------------
/// A scalar assignment value: `priority` is an integer, everything else text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Scalar {
    Str(String),
    Int(i64),
}

/// One `field=value` / `field+=v,v` / `field-=v,v` directive.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Assignment {
    Set { field: String, value: Scalar },
    Add { field: String, values: Vec<String> },
    Remove { field: String, values: Vec<String> },
}

impl Assignment {
    pub fn field(&self) -> &str {
        match self {
            Assignment::Set { field, .. }
            | Assignment::Add { field, .. }
            | Assignment::Remove { field, .. } => field,
        }
    }

    pub fn set_str(field: &str, value: &str) -> Assignment {
        Assignment::Set {
            field: field.to_string(),
            value: Scalar::Str(value.to_string()),
        }
    }
}

const SCALAR_FIELD_NAMES: &[&str] = &[
    "title", "type", "status", "priority", "assignee", "parent", "body",
];

fn normalise_structural(value: &str) -> Result<String> {
    normalize_id(value).map_err(|e| Error::Ticket(e.message().to_string()))
}

/// Parse one `field=value` token. `labels+=a,b` adds, `labels-=a` removes,
/// `priority=1` coerces to an integer.
pub fn parse_assignment(token: &str) -> Result<Assignment> {
    let Some((key, raw)) = token.split_once('=') else {
        return Err(Error::Ticket(format!(
            "expected field=value, got '{token}'"
        )));
    };
    let key = key.trim();
    if key.is_empty() {
        return Err(Error::Ticket(format!(
            "empty field in assignment '{token}'"
        )));
    }
    if let Some(field) = key.strip_suffix('+').or_else(|| key.strip_suffix('-')) {
        if !SET_FIELDS.contains(&field) {
            return Err(Error::Ticket(format!(
                "'{field}' is not a set field (cannot use +/-)"
            )));
        }
        let mut values: Vec<String> = raw
            .split(',')
            .map(str::trim)
            .filter(|v| !v.is_empty())
            .map(String::from)
            .collect();
        if values.is_empty() {
            return Err(Error::Ticket(format!(
                "empty value list in assignment '{token}'"
            )));
        }
        if field == "blocked_by" {
            values = values
                .iter()
                .map(|v| normalise_structural(v))
                .collect::<Result<_>>()?;
        }
        let field = field.to_string();
        return Ok(if key.ends_with('+') {
            Assignment::Add { field, values }
        } else {
            Assignment::Remove { field, values }
        });
    }
    if !SCALAR_FIELD_NAMES.contains(&key) {
        return Err(Error::Ticket(format!("unknown field '{key}'")));
    }
    let value = match key {
        "priority" => Scalar::Int(
            raw.trim()
                .parse()
                .map_err(|_| Error::Ticket(format!("priority must be an integer, got '{raw}'")))?,
        ),
        "parent" => Scalar::Str(normalise_structural(raw)?),
        _ => Scalar::Str(raw.to_string()),
    };
    Ok(Assignment::Set {
        field: key.to_string(),
        value,
    })
}

// ---------------------------------------------------------------------------
// Templates (`rp new --template`).
// ---------------------------------------------------------------------------
/// Ticket defaults read from `templates/<name>.toml`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Template {
    pub kind: Option<String>,
    pub priority: Option<i64>,
    pub labels: Option<Vec<String>>,
    pub blocked_by: Option<Vec<String>>,
    pub parent: Option<String>,
    pub assignee: Option<String>,
    pub body: Option<String>,
}

const TEMPLATE_FIELDS: &[&str] = &[
    "title",
    "type",
    "status",
    "priority",
    "assignee",
    "parent",
    "body",
    "labels",
    "blocked_by",
];

fn template_path(rohrpost_dir: &Path, name: &str) -> Result<PathBuf> {
    let requested = name.trim();
    if requested.is_empty() {
        return Err(Error::Ticket("template name must be non-empty".into()));
    }
    let filename = if requested.ends_with(".toml") {
        requested.to_string()
    } else {
        format!("{requested}.toml")
    };
    let simple = !filename.contains(['/', '\\'])
        && filename != ".toml"
        && filename != "..toml"
        && !filename.starts_with('.');
    if !simple {
        return Err(Error::Ticket(
            "template name must be a simple filename".into(),
        ));
    }
    let path = paths::templates_dir(rohrpost_dir).join(&filename);
    if !path.is_file() {
        return Err(Error::Ticket(format!("no such template: {name}")));
    }
    Ok(path)
}

fn template_strings(field: &str, value: &Toml) -> Result<Vec<String>> {
    let items: Vec<&Toml> = match value {
        Toml::Array(items) => items.iter().collect(),
        other => vec![other],
    };
    items
        .into_iter()
        .map(|item| match item.as_str().map(str::trim) {
            Some(s) if !s.is_empty() => Ok(s.to_string()),
            _ => Err(Error::Ticket(format!(
                "template {field} must contain non-empty strings"
            ))),
        })
        .collect()
}

/// Load and validate ticket defaults from `templates/<name>.toml`. Values may
/// sit at the top level or under `[defaults]`, `[fields]` or `[ticket]`.
pub fn load_template(rohrpost_dir: &Path, name: &str) -> Result<Template> {
    let path = template_path(rohrpost_dir, name)?;
    let text = std::fs::read_to_string(&path)
        .map_err(|e| Error::Ticket(format!("cannot read template '{name}': {e}")))?;
    let root =
        toml::parse(&text).map_err(|e| Error::Ticket(format!("invalid template '{name}': {e}")))?;

    let sections = ["defaults", "fields", "ticket"];
    let mut values: Vec<(String, Toml)> = root
        .iter()
        .filter(|(k, _)| !sections.contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    for section in sections {
        if let Some(table) = root.get(section) {
            let Toml::Table(table) = table else {
                return Err(Error::Ticket(format!(
                    "template section [{section}] must be a table"
                )));
            };
            values.extend(table.iter().map(|(k, v)| (k.clone(), v.clone())));
        }
    }
    let unknown: BTreeSet<&str> = values
        .iter()
        .map(|(k, _)| k.as_str())
        .filter(|k| !TEMPLATE_FIELDS.contains(k))
        .collect();
    if !unknown.is_empty() {
        let list: Vec<&str> = unknown.into_iter().collect();
        return Err(Error::Ticket(format!(
            "unknown template field(s): {}",
            list.join(", ")
        )));
    }

    let mut template = Template::default();
    for (key, value) in &values {
        match key.as_str() {
            "priority" => {
                template.priority =
                    Some(value.as_int().ok_or_else(|| {
                        Error::Ticket("template priority must be an integer".into())
                    })?);
            }
            "labels" => template.labels = Some(template_strings("labels", value)?),
            "blocked_by" => {
                template.blocked_by = Some(
                    template_strings("blocked_by", value)?
                        .iter()
                        .map(|v| normalise_structural(v))
                        .collect::<Result<_>>()?,
                );
            }
            "parent" => {
                let raw = value
                    .as_str()
                    .ok_or_else(|| Error::Ticket("template parent must be a ticket id".into()))?;
                template.parent = Some(normalise_structural(raw)?);
            }
            "type" | "assignee" | "body" | "title" | "status" => {
                let raw = value
                    .as_str()
                    .ok_or_else(|| Error::Ticket(format!("template {key} must be a string")))?;
                match key.as_str() {
                    "type" => template.kind = Some(raw.to_string()),
                    "assignee" => template.assignee = Some(raw.to_string()),
                    "body" => template.body = Some(raw.to_string()),
                    _ => {} // title/status are accepted for forward compatibility but unused
                }
            }
            _ => unreachable!("validated above"),
        }
    }
    Ok(template)
}

// ---------------------------------------------------------------------------
// Shared helpers.
// ---------------------------------------------------------------------------
pub fn load_tickets(rohrpost_dir: &Path) -> Result<Tickets> {
    fold::load_tickets(rohrpost_dir)
}

fn resolve<'a>(by_id: &'a Tickets, ticket_ref: &str) -> Result<&'a Ticket> {
    let tid = normalise_structural(ticket_ref)?;
    by_id
        .get(&tid)
        .ok_or_else(|| Error::NotFound(format!("no such ticket: {ticket_ref}")))
}

fn build_event(ticket: &str, op: &str, actor: &str) -> Result<Event> {
    Ok(Event {
        id: new_ulid(None)?,
        ts: now_ts(),
        ticket: ticket.to_string(),
        op: op.to_string(),
        actor: actor.to_string(),
        set: None,
        text: None,
        reason: None,
        extra: Vec::new(),
    })
}

fn append_and_reload(rohrpost_dir: &Path, event: Event) -> Result<Ticket> {
    store::append_event(rohrpost_dir, &event)?;
    load_tickets(rohrpost_dir)?
        .remove(&event.ticket)
        .ok_or_else(|| {
            Error::Store(format!(
                "ticket {} did not appear after its event was appended",
                event.ticket
            ))
        })
}

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct InitResult {
    pub rohrpost_dir: PathBuf,
    pub prefix: String,
    pub created_config: bool,
    pub updated_gitattributes: bool,
}

/// A 2-5 letter uppercase prefix from a directory name; `RP` if it yields too few letters.
pub fn propose_prefix(directory: &Path) -> String {
    let name = directory
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let candidate: String = name
        .chars()
        .filter(char::is_ascii_alphabetic)
        .map(|c| c.to_ascii_uppercase())
        .take(5)
        .collect();
    if candidate.len() < 2 {
        "RP".to_string()
    } else {
        candidate
    }
}

/// Scaffold `.rohrpost/` and the committed `.gitattributes` rules. Idempotent:
/// re-running fills in anything missing without clobbering `config.toml`. The
/// scaffold lands at the git root when inside a repo, else at `target_dir`.
pub fn init_repo(target_dir: &Path, prefix: Option<&str>) -> Result<InitResult> {
    let repo_root =
        paths::find_git_root(Some(target_dir))?.unwrap_or_else(|| target_dir.to_path_buf());
    let rohrpost_dir = repo_root.join(paths::ROHRPOST_DIR_NAME);
    paths::ensure_layout(&rohrpost_dir)?;

    let cfg_path = paths::config_path(&rohrpost_dir);
    let created_config = !cfg_path.is_file();
    if created_config {
        let chosen = match prefix {
            Some(p) => config::validate_prefix(p)?,
            None => propose_prefix(&repo_root),
        };
        std::fs::write(&cfg_path, config::render_config_toml(&chosen))
            .map_err(|e| Error::Store(format!("cannot write {}: {e}", cfg_path.display())))?;
    }
    let config = config::load_config(&rohrpost_dir)?;
    let updated_gitattributes = paths::write_gitattributes(&repo_root)?;
    Ok(InitResult {
        rohrpost_dir,
        prefix: config.prefix,
        created_config,
        updated_gitattributes,
    })
}

// ---------------------------------------------------------------------------
// create
// ---------------------------------------------------------------------------
/// Inputs for [`create_ticket`]. Created tickets always start `open` (§5.4).
#[derive(Debug, Clone)]
pub struct NewTicket {
    pub title: String,
    pub kind: String,
    pub priority: i64,
    pub parent: Option<String>,
    pub labels: Vec<String>,
    pub blocked_by: Vec<String>,
    pub assignee: Option<String>,
    pub body: Option<String>,
}

impl NewTicket {
    pub fn new(title: &str) -> NewTicket {
        NewTicket {
            title: title.to_string(),
            kind: DEFAULT_TYPE.to_string(),
            priority: DEFAULT_PRIORITY,
            parent: None,
            labels: Vec::new(),
            blocked_by: Vec::new(),
            assignee: None,
            body: None,
        }
    }
}

fn validate_type(kind: &str) -> Result<()> {
    if TYPES.contains(&kind) {
        Ok(())
    } else {
        Err(Error::Ticket(format!(
            "type must be one of {}, got '{kind}'",
            sorted_list(TYPES)
        )))
    }
}

fn validate_priority(priority: i64) -> Result<()> {
    if (0..=4).contains(&priority) {
        Ok(())
    } else {
        Err(Error::Ticket(format!(
            "priority must be 0..4, got {priority}"
        )))
    }
}

fn sorted_list(items: &[&str]) -> String {
    let mut sorted: Vec<&str> = items.to_vec();
    sorted.sort_unstable();
    format!(
        "[{}]",
        sorted
            .iter()
            .map(|s| format!("'{s}'"))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

fn new_id(existing: &Tickets) -> Result<String> {
    (0..8)
        .map(|_| new_ticket_id())
        .find(|candidate| !existing.contains_key(candidate))
        .ok_or_else(|| {
            Error::Store("could not allocate a non-colliding ticket id after 8 tries".into())
        })
}

/// Create a ticket (append a `create` event) and return the folded result.
pub fn create_ticket(rohrpost_dir: &Path, spec: &NewTicket, actor: &str) -> Result<WriteResult> {
    let title = spec.title.trim();
    if title.is_empty() {
        return Err(Error::Ticket("title must be non-empty".into()));
    }
    validate_type(&spec.kind)?;
    validate_priority(spec.priority)?;

    let mut payload: Vec<(Key, Json)> = vec![
        ("title".into(), json::s(title)),
        ("type".into(), json::s(&spec.kind)),
        ("status".into(), json::s("open")),
        ("priority".into(), Json::Int(spec.priority)),
    ];
    if let Some(parent) = &spec.parent {
        payload.push(("parent".into(), json::s(normalise_structural(parent)?)));
    }
    if !spec.labels.is_empty() {
        let labels: BTreeSet<&str> = spec.labels.iter().map(String::as_str).collect();
        payload.push(("labels+".into(), json::str_list(labels)));
    }
    if !spec.blocked_by.is_empty() {
        let deps: BTreeSet<String> = spec
            .blocked_by
            .iter()
            .map(|b| normalise_structural(b))
            .collect::<Result<_>>()?;
        payload.push(("blocked_by+".into(), json::str_list(deps)));
    }
    if let Some(assignee) = spec.assignee.as_deref().filter(|a| !a.is_empty()) {
        payload.push(("assignee".into(), json::s(assignee)));
    }
    if let Some(body) = spec.body.as_deref().filter(|b| !b.trim().is_empty()) {
        payload.push(("body".into(), json::s(body)));
    }

    let existing = load_tickets(rohrpost_dir)?;
    let tid = new_id(&existing)?;
    let mut event = build_event(&tid, "create", actor)?;
    event.set = Some(payload);
    Ok(WriteResult {
        ticket: append_and_reload(rohrpost_dir, event)?,
        wrote: true,
    })
}

// ---------------------------------------------------------------------------
// set / claim / close / drop / comment
// ---------------------------------------------------------------------------
/// Does the ticket already hold this scalar value?
fn scalar_matches(ticket: &Ticket, field: &str, value: &Scalar) -> bool {
    match (field, value) {
        ("priority", Scalar::Int(i)) => *i == ticket.priority,
        ("title", Scalar::Str(s)) => *s == ticket.title,
        ("type", Scalar::Str(s)) => *s == ticket.kind,
        ("status", Scalar::Str(s)) => *s == ticket.status,
        ("assignee", Scalar::Str(s)) => ticket.assignee.as_deref() == Some(s),
        ("parent", Scalar::Str(s)) => ticket.parent.as_deref() == Some(s),
        ("body", Scalar::Str(s)) => ticket.body.as_deref() == Some(s),
        _ => false,
    }
}

/// The effective form of one assignment, or `None` if it would change nothing.
fn effective_one(ticket: &Ticket, a: &Assignment) -> Option<Assignment> {
    match a {
        Assignment::Set { field, value } => {
            (!scalar_matches(ticket, field, value)).then(|| a.clone())
        }
        Assignment::Add { field, values } | Assignment::Remove { field, values } => {
            let current: &[String] = if field == "labels" {
                &ticket.labels
            } else {
                &ticket.blocked_by
            };
            let adding = matches!(a, Assignment::Add { .. });
            let mut changed: Vec<String> = Vec::new();
            for v in values {
                if current.contains(v) != adding && !changed.contains(v) {
                    changed.push(v.clone());
                }
            }
            if changed.is_empty() {
                None
            } else if adding {
                Some(Assignment::Add {
                    field: field.clone(),
                    values: changed,
                })
            } else {
                Some(Assignment::Remove {
                    field: field.clone(),
                    values: changed,
                })
            }
        }
    }
}

fn validate_assignment(a: &Assignment) -> Result<()> {
    if let Assignment::Set { field, value } = a {
        match (field.as_str(), value) {
            ("status", Scalar::Str(s)) if !STATUSES.contains(&s.as_str()) => {
                return Err(Error::Ticket(format!(
                    "status must be one of {}, got '{s}'",
                    sorted_list(STATUSES)
                )));
            }
            ("type", Scalar::Str(s)) => validate_type(s)?,
            ("priority", Scalar::Int(p)) => validate_priority(*p)?,
            _ => {}
        }
    }
    Ok(())
}

fn assignments_to_payload(assignments: &[Assignment]) -> Vec<(Key, Json)> {
    assignments
        .iter()
        .map(|a| match a {
            Assignment::Set {
                field,
                value: Scalar::Str(s),
            } => (field.clone().into(), json::s(s)),
            Assignment::Set {
                field,
                value: Scalar::Int(i),
            } => (field.clone().into(), Json::Int(*i)),
            Assignment::Add { field, values } => (
                format!("{field}+").into(),
                json::str_list(values.iter().cloned()),
            ),
            Assignment::Remove { field, values } => (
                format!("{field}-").into(),
                json::str_list(values.iter().cloned()),
            ),
        })
        .collect()
}

/// Apply assignments as one `set` event. Idempotent: assignments already
/// satisfied are dropped, and nothing is appended when none remain.
pub fn set_fields(
    rohrpost_dir: &Path,
    ticket_ref: &str,
    assignments: &[Assignment],
    actor: &str,
) -> Result<WriteResult> {
    let by_id = load_tickets(rohrpost_dir)?;
    let ticket = resolve(&by_id, ticket_ref)?;
    let effective: Vec<Assignment> = assignments
        .iter()
        .filter_map(|a| effective_one(ticket, a))
        .collect();
    if effective.is_empty() {
        return Ok(WriteResult {
            ticket: ticket.clone(),
            wrote: false,
        });
    }
    for a in &effective {
        validate_assignment(a)?;
    }
    let mut event = build_event(&ticket.id, "set", actor)?;
    event.set = Some(assignments_to_payload(&effective));
    Ok(WriteResult {
        ticket: append_and_reload(rohrpost_dir, event)?,
        wrote: true,
    })
}

/// Mark a ticket `in_progress` and stamp the actor as assignee (idempotent).
pub fn claim(rohrpost_dir: &Path, ticket_ref: &str, actor: &str) -> Result<WriteResult> {
    let assignments = [
        Assignment::set_str("status", "in_progress"),
        Assignment::set_str("assignee", actor),
    ];
    set_fields(rohrpost_dir, ticket_ref, &assignments, actor)
}

fn terminate(
    rohrpost_dir: &Path,
    ticket_ref: &str,
    status: &str,
    reason: Option<&str>,
    actor: &str,
) -> Result<WriteResult> {
    let by_id = load_tickets(rohrpost_dir)?;
    let ticket = resolve(&by_id, ticket_ref)?;
    if ticket.status == status {
        return Ok(WriteResult {
            ticket: ticket.clone(),
            wrote: false,
        });
    }
    let mut event = build_event(&ticket.id, "set", actor)?;
    event.set = Some(vec![("status".into(), json::s(status))]);
    event.reason = reason
        .map(str::trim)
        .filter(|r| !r.is_empty())
        .map(String::from);
    Ok(WriteResult {
        ticket: append_and_reload(rohrpost_dir, event)?,
        wrote: true,
    })
}

/// Set status to `done` with an optional reason (idempotent).
pub fn close(
    rohrpost_dir: &Path,
    ticket_ref: &str,
    reason: Option<&str>,
    actor: &str,
) -> Result<WriteResult> {
    terminate(rohrpost_dir, ticket_ref, "done", reason, actor)
}

/// Set status to `dropped` with an optional reason (idempotent).
pub fn drop(
    rohrpost_dir: &Path,
    ticket_ref: &str,
    reason: Option<&str>,
    actor: &str,
) -> Result<WriteResult> {
    terminate(rohrpost_dir, ticket_ref, "dropped", reason, actor)
}

/// Append a local note (`comment` event).
pub fn add_comment(
    rohrpost_dir: &Path,
    ticket_ref: &str,
    text: &str,
    actor: &str,
) -> Result<WriteResult> {
    let text = text.trim();
    if text.is_empty() {
        return Err(Error::Ticket("comment text must be non-empty".into()));
    }
    let by_id = load_tickets(rohrpost_dir)?;
    let ticket = resolve(&by_id, ticket_ref)?;
    let mut event = build_event(&ticket.id, "comment", actor)?;
    event.text = Some(text.to_string());
    Ok(WriteResult {
        ticket: append_and_reload(rohrpost_dir, event)?,
        wrote: true,
    })
}

// ---------------------------------------------------------------------------
// Reads.
// ---------------------------------------------------------------------------
pub fn show_ticket(rohrpost_dir: &Path, ticket_ref: &str) -> Result<Ticket> {
    let by_id = load_tickets(rohrpost_dir)?;
    resolve(&by_id, ticket_ref).cloned()
}

/// Filters for [`list_tickets`]; all optional, and they compose.
#[derive(Debug, Clone, Default)]
pub struct Filter {
    pub status: Option<String>,
    pub label: Option<String>,
    pub parent: Option<String>,
    pub kind: Option<String>,
    pub matches: Option<String>,
    pub ready: bool,
}

/// Query folded tickets, sorted by priority then age.
pub fn list_tickets<'a>(by_id: &'a Tickets, filter: &Filter) -> Result<Vec<&'a Ticket>> {
    let parent = filter
        .parent
        .as_deref()
        .map(normalise_structural)
        .transpose()?;
    let needle = filter.matches.as_deref().map(str::to_lowercase);
    let mut out: Vec<&Ticket> = by_id
        .values()
        .filter(|t| {
            filter
                .status
                .as_deref()
                .is_none_or(|s| status_matches(t, by_id, s))
        })
        .filter(|t| {
            filter
                .label
                .as_deref()
                .is_none_or(|l| t.labels.iter().any(|x| x == l))
        })
        .filter(|t| {
            parent
                .as_deref()
                .is_none_or(|p| t.parent.as_deref() == Some(p))
        })
        .filter(|t| filter.kind.as_deref().is_none_or(|k| t.kind == k))
        .filter(|t| {
            needle
                .as_deref()
                .is_none_or(|n| t.title.to_lowercase().contains(n))
        })
        .filter(|t| !filter.ready || fold::is_ready(t, by_id))
        .collect();
    sort_tickets(&mut out);
    Ok(out)
}

/// `--status` matches the derived status; `ready` is the readiness predicate.
fn status_matches(ticket: &Ticket, by_id: &Tickets, wanted: &str) -> bool {
    if wanted == "ready" {
        fold::is_ready(ticket, by_id)
    } else {
        fold::derive_status(ticket, by_id) == wanted
    }
}

/// The actionable work queue (spec §10): `open`, unblocked, non-epic, by priority.
pub fn ready_tickets(by_id: &Tickets, limit: Option<usize>) -> Result<Vec<&Ticket>> {
    let mut tickets = list_tickets(
        by_id,
        &Filter {
            ready: true,
            ..Filter::default()
        },
    )?;
    if let Some(n) = limit {
        tickets.truncate(n);
    }
    Ok(tickets)
}

/// An epic and its direct children (one level of nesting, spec §5.5).
pub struct Tree<'a> {
    pub root: &'a Ticket,
    pub children: Vec<&'a Ticket>,
}

pub fn tree<'a>(by_id: &'a Tickets, ticket_ref: &str) -> Result<Tree<'a>> {
    let root = resolve(by_id, ticket_ref)?;
    let mut children: Vec<&Ticket> = by_id
        .values()
        .filter(|c| c.parent.as_deref() == Some(&root.id))
        .collect();
    sort_tickets(&mut children);
    Ok(Tree { root, children })
}

/// Raw event history sorted by `(ts, id)`, optionally filtered to one ticket
/// (legacy sync watermarks are never attributed to a ticket).
pub fn event_log(rohrpost_dir: &Path, ticket_ref: Option<&str>) -> Result<Vec<Event>> {
    let mut events = store::read_events(rohrpost_dir)?;
    if let Some(reference) = ticket_ref {
        let tid = normalise_structural(reference)?;
        events.retain(|e| e.op != "synced" && fold::bare_id(&e.ticket) == tid);
    }
    events.sort_by(|a, b| (a.ts.as_str(), a.id.as_str()).cmp(&(b.ts.as_str(), b.id.as_str())));
    Ok(events)
}

/// Config for the CLI: defaults if unreadable so read paths always work.
pub fn load_repo_config(rohrpost_dir: &Path) -> Config {
    config::load_config_or_default(rohrpost_dir)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_scalar_and_set_assignments() {
        assert_eq!(
            parse_assignment("priority=1").unwrap(),
            Assignment::Set {
                field: "priority".into(),
                value: Scalar::Int(1)
            }
        );
        assert_eq!(
            parse_assignment("parent=TST-a1b2c3").unwrap(),
            Assignment::set_str("parent", "a1b2c3")
        );
        assert_eq!(
            parse_assignment("labels+=auth, bug,").unwrap(),
            Assignment::Add {
                field: "labels".into(),
                values: vec!["auth".into(), "bug".into()]
            }
        );
        assert_eq!(
            parse_assignment("blocked_by-=TST-a1b2c3").unwrap(),
            Assignment::Remove {
                field: "blocked_by".into(),
                values: vec!["a1b2c3".into()]
            }
        );
        for bad in [
            "nofield",
            "=v",
            "bogus=1",
            "priority=x",
            "title+=x",
            "labels+=",
            "parent=nope",
        ] {
            assert!(parse_assignment(bad).is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn proposes_prefix_from_directory_name() {
        assert_eq!(propose_prefix(Path::new("/tmp/my-project42")), "MYPRO");
        assert_eq!(propose_prefix(Path::new("/tmp/x")), "RP");
        assert_eq!(propose_prefix(Path::new("ab")), "AB");
    }
}
