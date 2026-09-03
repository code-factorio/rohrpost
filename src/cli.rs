//! The `rp` command surface: argv parsing, dispatch and rendering.
//!
//! Every mutation goes through [`crate::api`]; this module only adapts argv and
//! renders output. `--json` is honoured on every command and returns
//! machine-readable output (the agent interface); the default is readable text
//! that respects `NO_COLOR`, `CLICOLOR=0` and non-tty streams.
//!
//! Exit codes: `0` success, `1` a domain failure (no such ticket, bad status,
//! …), `2` a usage error.

use std::collections::HashMap;
use std::io::{IsTerminal, Read as _, Write as _};
use std::path::PathBuf;

use crate::api::{self, Assignment, Filter, NewTicket, Scalar};
use crate::compact;
use crate::doctor;
use crate::error::{Error, Result};
use crate::fold::{DEFAULT_PRIORITY, Shape, Ticket, Tickets, derive_status, ticket_to_json};
use crate::ids::render_id;
use crate::json::{self, Json};
use crate::paths;
use crate::stats;
use crate::util::resolve_actor;

// ---------------------------------------------------------------------------
// Command specs: the single description that drives parsing and --help.
// ---------------------------------------------------------------------------
struct Opt {
    long: &'static str,
    short: Option<&'static str>,
    /// `None` for a boolean flag, `Some(METAVAR)` for an option taking a value.
    metavar: Option<&'static str>,
    repeatable: bool,
    help: &'static str,
}

const fn flag(long: &'static str, help: &'static str) -> Opt {
    Opt {
        long,
        short: None,
        metavar: None,
        repeatable: false,
        help,
    }
}
const fn value(long: &'static str, metavar: &'static str, help: &'static str) -> Opt {
    Opt {
        long,
        short: None,
        metavar: Some(metavar),
        repeatable: false,
        help,
    }
}
const fn many(long: &'static str, metavar: &'static str, help: &'static str) -> Opt {
    Opt {
        long,
        short: None,
        metavar: Some(metavar),
        repeatable: true,
        help,
    }
}

const JSON: Opt = flag("json", "emit machine-readable JSON");
const ACTOR: Opt = value(
    "actor",
    "ACTOR",
    "override the event actor (default: user/<git email> or runner from env)",
);
const BODY_FILE: Opt = value(
    "body-file",
    "PATH",
    "read the text from a file ('-' reads stdin); UTF-8, no locale guessing",
);
const ID_ARG: (&str, &str) = ("id", "ticket id (bare or PREFIX-id)");

struct Spec {
    name: &'static str,
    help: &'static str,
    /// Positional arguments: (name, help). Required unless the name ends with `?`.
    positionals: &'static [(&'static str, &'static str)],
    /// The last positional soaks up every remaining token.
    variadic: bool,
    options: &'static [Opt],
}

const SPECS: &[Spec] = &[
    Spec {
        name: "init",
        help: "scaffold .rohrpost/ in this repository",
        positionals: &[],
        variadic: false,
        options: &[
            value(
                "prefix",
                "PREFIX",
                "project id prefix (2-5 uppercase letters)",
            ),
            JSON,
        ],
    },
    Spec {
        name: "new",
        help: "create a ticket",
        positionals: &[("title", "ticket title")],
        variadic: false,
        options: &[
            value(
                "template",
                "NAME",
                "load defaults from templates/<name>.toml",
            ),
            value("type", "TYPE", "task | bug | spike | epic (default: task)"),
            Opt {
                long: "priority",
                short: Some("p"),
                metavar: Some("N"),
                repeatable: false,
                help: "0 highest .. 4 lowest",
            },
            many("label", "LABEL", "label (repeatable)"),
            many("blocked-by", "ID", "ticket id (repeatable)"),
            value("parent", "ID", "parent epic id"),
            value("assignee", "ACTOR", "assignee actor string"),
            value("body", "TEXT", "ticket body / description"),
            BODY_FILE,
            ACTOR,
            JSON,
        ],
    },
    Spec {
        name: "ready",
        help: "unblocked, actionable work",
        positionals: &[],
        variadic: false,
        options: &[value("limit", "N", "cap the number of results"), JSON],
    },
    Spec {
        name: "show",
        help: "show a ticket",
        positionals: &[ID_ARG],
        variadic: false,
        options: &[
            value(
                "include",
                "SECTIONS",
                "comma list of extra sections: body,deps,notes,fieldts (default: body)",
            ),
            JSON,
        ],
    },
    Spec {
        name: "tree",
        help: "an epic and its children",
        positionals: &[ID_ARG],
        variadic: false,
        options: &[JSON],
    },
    Spec {
        name: "list",
        help: "query tickets",
        positionals: &[],
        variadic: false,
        options: &[
            value("status", "STATUS", "filter by (possibly derived) status"),
            value("label", "LABEL", "filter by label"),
            value("parent", "ID", "filter by parent id"),
            value("type", "TYPE", "filter by type"),
            value("match", "TEXT", "case-insensitive substring of the title"),
            JSON,
        ],
    },
    Spec {
        name: "claim",
        help: "mark a ticket in_progress and stamp the actor",
        positionals: &[ID_ARG],
        variadic: false,
        options: &[ACTOR, JSON],
    },
    Spec {
        name: "set",
        help: "update one or more fields (field=value ...)",
        positionals: &[ID_ARG, ("field=value?", "e.g. status=done labels+=auth")],
        variadic: true,
        options: &[BODY_FILE, ACTOR, JSON],
    },
    Spec {
        name: "close",
        help: "set status to done",
        positionals: &[ID_ARG],
        variadic: false,
        options: &[
            value("reason", "TEXT", "close reason (recorded on the event)"),
            ACTOR,
            JSON,
        ],
    },
    Spec {
        name: "drop",
        help: "set status to dropped",
        positionals: &[ID_ARG],
        variadic: false,
        options: &[
            value("reason", "TEXT", "drop reason (recorded on the event)"),
            ACTOR,
            JSON,
        ],
    },
    Spec {
        name: "comment",
        help: "append a local note",
        positionals: &[ID_ARG, ("text?", "note text (or pass --body-file)")],
        variadic: false,
        options: &[BODY_FILE, ACTOR, JSON],
    },
    Spec {
        name: "comments",
        help: "show all notes on a ticket",
        positionals: &[ID_ARG],
        variadic: false,
        options: &[JSON],
    },
    Spec {
        name: "log",
        help: "raw event history",
        positionals: &[("id?", "optional ticket id to filter to")],
        variadic: false,
        options: &[JSON],
    },
    Spec {
        name: "doctor",
        help: "integrity and config checks",
        positionals: &[],
        variadic: false,
        options: &[JSON],
    },
    Spec {
        name: "compact",
        help: "archive old events and truncate the log (main only)",
        positionals: &[],
        variadic: false,
        options: &[
            flag("force", "bypass the clean-main-branch guard"),
            value(
                "archive-after",
                "DAYS",
                "days a terminal ticket must sit before archiving (default: 90)",
            ),
            JSON,
        ],
    },
    Spec {
        name: "stats",
        help: "repository statistics: body/line sizes, fold timing",
        positionals: &[],
        variadic: false,
        options: &[JSON],
    },
];

fn spec(name: &str) -> Option<&'static Spec> {
    SPECS.iter().find(|s| s.name == name)
}

// ---------------------------------------------------------------------------
// argv parsing.
// ---------------------------------------------------------------------------
struct Parsed {
    spec: &'static Spec,
    positionals: Vec<String>,
    values: HashMap<&'static str, Vec<String>>,
    flags: Vec<&'static str>,
}

impl Parsed {
    fn flag(&self, name: &str) -> bool {
        self.flags.contains(&name)
    }
    fn value(&self, name: &str) -> Option<&str> {
        self.values
            .get(name)
            .and_then(|v| v.last())
            .map(String::as_str)
    }
    fn values(&self, name: &str) -> Vec<String> {
        self.values.get(name).cloned().unwrap_or_default()
    }
    fn positional(&self, index: usize) -> Option<&str> {
        self.positionals.get(index).map(String::as_str)
    }
    fn int_value(&self, name: &str) -> Result<Option<i64>> {
        self.value(name)
            .map(|raw| {
                raw.trim()
                    .parse::<i64>()
                    .map_err(|_| usage(&format!("argument --{name}: invalid int value: '{raw}'")))
            })
            .transpose()
    }
}

fn usage(message: &str) -> Error {
    Error::Usage(message.to_string())
}

/// What the top-level parse decided to do.
enum Action {
    Help(Option<&'static Spec>),
    Version,
    Run(Parsed),
}

fn parse_args(args: &[String]) -> Result<Action> {
    let Some(first) = args.first() else {
        return Ok(Action::Help(None));
    };
    match first.as_str() {
        "-h" | "--help" => return Ok(Action::Help(None)),
        "--version" | "-V" => return Ok(Action::Version),
        _ => {}
    }
    let Some(spec) = spec(first) else {
        return Err(usage(&format!(
            "unknown command '{first}' (see `rp --help`)"
        )));
    };
    let mut parsed = Parsed {
        spec,
        positionals: Vec::new(),
        values: HashMap::new(),
        flags: Vec::new(),
    };
    let mut tokens = args[1..].iter();
    let mut only_positionals = false;
    while let Some(token) = tokens.next() {
        if only_positionals || !looks_like_option(token) {
            parsed.positionals.push(token.clone());
            continue;
        }
        if token == "--" {
            only_positionals = true;
            continue;
        }
        if token == "-h" || token == "--help" {
            return Ok(Action::Help(Some(spec)));
        }
        // Split `--opt=value` / `-pVALUE`; find the option.
        let (name, inline): (String, Option<String>) = if let Some(long) = token.strip_prefix("--")
        {
            match long.split_once('=') {
                Some((n, v)) => (n.to_string(), Some(v.to_string())),
                None => (long.to_string(), None),
            }
        } else {
            // Split on character boundaries: `-é` must be a usage error, not a panic.
            let mut chars = token[1..].chars();
            let short = chars.next().map(String::from).unwrap_or_default();
            let rest = chars.as_str();
            let opt = spec
                .options
                .iter()
                .find(|o| o.short == Some(short.as_str()))
                .ok_or_else(|| usage(&format!("{}: unrecognized option '{token}'", spec.name)))?;
            (
                opt.long.to_string(),
                (!rest.is_empty()).then(|| rest.trim_start_matches('=').to_string()),
            )
        };
        let opt = spec
            .options
            .iter()
            .find(|o| o.long == name)
            .ok_or_else(|| usage(&format!("{}: unrecognized option '--{name}'", spec.name)))?;
        match opt.metavar {
            None => {
                if inline.is_some() {
                    return Err(usage(&format!("--{name} does not take a value")));
                }
                parsed.flags.push(opt.long);
            }
            Some(_) => {
                let value = match inline {
                    Some(v) => v,
                    None => tokens.next().cloned().ok_or_else(|| {
                        usage(&format!("argument --{name}: expected one argument"))
                    })?,
                };
                let slot = parsed.values.entry(opt.long).or_default();
                if !opt.repeatable {
                    slot.clear();
                }
                slot.push(value);
            }
        }
    }
    let required = spec
        .positionals
        .iter()
        .filter(|(n, _)| !n.ends_with('?'))
        .count();
    if parsed.positionals.len() < required {
        let missing: Vec<&str> = spec.positionals[parsed.positionals.len()..required]
            .iter()
            .map(|(n, _)| *n)
            .collect();
        return Err(usage(&format!(
            "{}: the following arguments are required: {}",
            spec.name,
            missing.join(", ")
        )));
    }
    if !spec.variadic && parsed.positionals.len() > spec.positionals.len() {
        return Err(usage(&format!(
            "{}: unrecognized arguments: {}",
            spec.name,
            parsed.positionals[spec.positionals.len()..].join(" ")
        )));
    }
    Ok(Action::Run(parsed))
}

/// `-x`/`--x` are options; a lone `-` (stdin) and negative numbers are values.
fn looks_like_option(token: &str) -> bool {
    token.len() > 1
        && token.starts_with('-')
        && !token[1..].starts_with(|c: char| c.is_ascii_digit())
}

fn render_help(spec: Option<&Spec>) -> String {
    let mut out = String::new();
    match spec {
        None => {
            out.push_str("usage: rp [-h] [--version] <command> ...\n\n");
            out.push_str("Rohrpost — a git-native ticket system for agentic coding workflows.\n\n");
            out.push_str("commands:\n");
            for s in SPECS {
                out.push_str(&format!("  {:<10} {}\n", s.name, s.help));
            }
            out.push_str("\noptions:\n  -h, --help  show this help message and exit\n  --version   show program's version number and exit\n");
            out.push_str("\nEvery command takes --json. Exit codes: 0 ok, 1 domain failure, 2 usage error.\n");
        }
        Some(s) => {
            let mut usage_line = format!("usage: rp {}", s.name);
            for o in s.options {
                match o.metavar {
                    Some(m) => usage_line.push_str(&format!(" [--{} {m}]", o.long)),
                    None => usage_line.push_str(&format!(" [--{}]", o.long)),
                }
            }
            for (name, _) in s.positionals {
                let bare = name.trim_end_matches('?');
                usage_line.push_str(if name.ends_with('?') { " [" } else { " " });
                usage_line.push_str(bare);
                if name.ends_with('?') {
                    usage_line.push(']');
                }
                if s.variadic && Some(name) == s.positionals.last().map(|(n, _)| n) {
                    usage_line.push_str(" ...");
                }
            }
            out.push_str(&usage_line);
            out.push_str(&format!("\n\n{}\n", s.help));
            if !s.positionals.is_empty() {
                out.push_str("\npositional arguments:\n");
                for (name, help) in s.positionals {
                    out.push_str(&format!("  {:<22} {help}\n", name.trim_end_matches('?')));
                }
            }
            out.push_str("\noptions:\n  -h, --help             show this help message and exit\n");
            for o in s.options {
                let mut left = String::new();
                if let Some(short) = o.short {
                    left.push_str(&format!("-{short}, "));
                }
                left.push_str(&format!("--{}", o.long));
                if let Some(m) = o.metavar {
                    left.push_str(&format!(" {m}"));
                }
                out.push_str(&format!("  {left:<22} {}\n", o.help));
            }
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Output.
// ---------------------------------------------------------------------------
fn emit(text: &str) {
    // A closed pipe (`rp ready | head`) must not turn into a panic.
    let stdout = std::io::stdout();
    let mut handle = stdout.lock();
    let _ = handle.write_all(text.as_bytes());
    let _ = handle.flush();
}

fn emit_line(text: &str) {
    let mut line = text.to_string();
    line.push('\n');
    emit(&line);
}

fn emit_json(value: &Json) {
    let mut text = value.to_pretty();
    text.push('\n');
    emit(&text);
}

fn emit_err(text: &str) {
    let _ = writeln!(std::io::stderr(), "{text}");
}

fn use_color() -> bool {
    if std::env::var_os("NO_COLOR").is_some_and(|v| !v.is_empty()) {
        return false;
    }
    if std::env::var("CLICOLOR").ok().as_deref() == Some("0") {
        return false;
    }
    std::io::stdout().is_terminal()
}

/// Resolved output settings for one invocation.
struct Out {
    json: bool,
    color: bool,
    prefix: String,
}

impl Out {
    fn rend(&self, bare_id: &str) -> String {
        render_id(&self.prefix, bare_id)
    }

    fn status(&self, status: &str) -> String {
        if !self.color {
            return status.to_string();
        }
        let code = match status {
            "done" | "ready" => "32",
            "dropped" => "90",
            "in_progress" => "36",
            "review" => "35",
            "waiting" => "33",
            _ => "0",
        };
        format!("\x1b[{code}m{status}\x1b[0m")
    }

    fn full(&self, ticket: &Ticket) -> Json {
        ticket_to_json(ticket, Some(&self.prefix), Shape::FULL)
    }

    fn short(&self, ticket: &Ticket) -> Json {
        ticket_to_json(ticket, Some(&self.prefix), Shape::SHORT)
    }

    fn summary(&self, ticket: &Ticket, by_id: &Tickets, status_override: Option<&str>) -> String {
        let status = status_override
            .map(str::to_string)
            .unwrap_or_else(|| derive_status(ticket, by_id));
        format!(
            "{}  [{}]  {}  p{}  {}",
            self.rend(&ticket.id),
            self.status(&status),
            ticket.kind,
            ticket.priority,
            ticket.title
        )
    }
}

/// The repo, its config and output settings for a command that needs a repo.
struct Ctx {
    repo: PathBuf,
    out: Out,
}

fn ctx(parsed: &Parsed) -> Result<Ctx> {
    let repo = paths::require_rohrpost_dir()?;
    let config = api::load_repo_config(&repo);
    Ok(Ctx {
        repo,
        out: Out {
            json: parsed.flag("json"),
            color: use_color(),
            prefix: config.prefix,
        },
    })
}

fn actor_of(parsed: &Parsed) -> String {
    resolve_actor(parsed.value("actor"))
}

/// Read `--body-file`: a path, or `-` for stdin. Strict UTF-8 on every platform;
/// a missing file or undecodable bytes is a usage error so pipelines fail loudly.
fn read_body_file(spec: &str) -> Result<String> {
    let raw = if spec == "-" {
        let mut buf = Vec::new();
        std::io::stdin()
            .lock()
            .read_to_end(&mut buf)
            .map_err(|e| usage(&format!("--body-file: cannot read stdin: {e}")))?;
        buf
    } else {
        std::fs::read(spec)
            .map_err(|e| usage(&format!("--body-file: cannot read '{spec}': {e}")))?
    };
    String::from_utf8(raw).map_err(|e| {
        usage(&format!(
            "--body-file: '{spec}' is not valid UTF-8 ({})",
            e.utf8_error()
        ))
    })
}

fn write_result_line(ctx: &Ctx, result: &api::WriteResult, yes: &str, no: &str, suffix: &str) {
    if ctx.out.json {
        emit_json(&ctx.out.full(&result.ticket));
    } else {
        let verb = if result.wrote { yes } else { no };
        emit_line(&format!(
            "{verb} {}{suffix}",
            ctx.out.rend(&result.ticket.id)
        ));
    }
}

// ---------------------------------------------------------------------------
// Command handlers. Each returns an exit code.
// ---------------------------------------------------------------------------
fn cmd_init(parsed: &Parsed) -> Result<i32> {
    let cwd = std::env::current_dir()
        .map_err(|e| Error::Store(format!("cannot determine the current directory: {e}")))?;
    let result = api::init_repo(&cwd, parsed.value("prefix"))?;
    if parsed.flag("json") {
        emit_json(&Json::Obj(vec![
            (
                "rohrpost_dir".into(),
                json::s(result.rohrpost_dir.display().to_string()),
            ),
            ("prefix".into(), json::s(&result.prefix)),
            ("created_config".into(), Json::Bool(result.created_config)),
            (
                "updated_gitattributes".into(),
                Json::Bool(result.updated_gitattributes),
            ),
        ]));
        return Ok(0);
    }
    emit_line(&format!(
        "Initialised rohrpost at {} (prefix={})",
        result.rohrpost_dir.display(),
        result.prefix
    ));
    if result.created_config {
        emit_line(&format!("  wrote {}", paths::CONFIG_FILENAME));
    }
    if result.updated_gitattributes {
        emit_line("  updated .gitattributes (merge and line-ending rules)");
    }
    Ok(0)
}

fn cmd_new(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let template = match parsed.value("template") {
        Some(name) => api::load_template(&ctx.repo, name)?,
        None => api::Template::default(),
    };
    if parsed.value("body").is_some() && parsed.value("body-file").is_some() {
        return Err(usage("--body and --body-file are mutually exclusive"));
    }
    let body = match parsed.value("body-file") {
        Some(path) => Some(read_body_file(path)?),
        None => parsed.value("body").map(String::from).or(template.body),
    };
    let priority = match parsed.int_value("priority")? {
        Some(p) => p,
        None => template.priority.unwrap_or(DEFAULT_PRIORITY),
    };
    let spec = NewTicket {
        title: parsed.positional(0).unwrap_or_default().to_string(),
        kind: parsed
            .value("type")
            .map(String::from)
            .or(template.kind)
            .unwrap_or_else(|| "task".into()),
        priority,
        parent: parsed.value("parent").map(String::from).or(template.parent),
        labels: if parsed.values.contains_key("label") {
            parsed.values("label")
        } else {
            template.labels.unwrap_or_default()
        },
        blocked_by: if parsed.values.contains_key("blocked-by") {
            parsed.values("blocked-by")
        } else {
            template.blocked_by.unwrap_or_default()
        },
        assignee: parsed
            .value("assignee")
            .map(String::from)
            .or(template.assignee),
        body,
    };
    let result = api::create_ticket(&ctx.repo, &spec, &actor_of(parsed))?;
    if ctx.out.json {
        emit_json(&ctx.out.full(&result.ticket));
    } else {
        emit_line(&format!(
            "Created {}  {}",
            ctx.out.rend(&result.ticket.id),
            result.ticket.title
        ));
    }
    Ok(0)
}

fn cmd_ready(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let by_id = api::load_tickets(&ctx.repo)?;
    let limit = parsed.int_value("limit")?.map(|n| n.max(0) as usize);
    let tickets = api::ready_tickets(&by_id, limit)?;
    if ctx.out.json {
        emit_json(&Json::Arr(
            tickets.iter().map(|t| ctx.out.short(t)).collect(),
        ));
    } else if tickets.is_empty() {
        emit_line("No actionable work. The tube is empty.");
    } else {
        for t in tickets {
            emit_line(&ctx.out.summary(t, &by_id, Some("ready")));
        }
    }
    Ok(0)
}

fn cmd_show(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let by_id = api::load_tickets(&ctx.repo)?;
    let ticket = api::show_ticket(&ctx.repo, parsed.positional(0).unwrap_or_default())?;
    if ctx.out.json {
        emit_json(&ctx.out.full(&ticket));
        return Ok(0);
    }
    emit(&render_detail(
        &ctx.out,
        &ticket,
        &by_id,
        parsed.value("include").unwrap_or("body"),
    ));
    Ok(0)
}

fn render_detail(out: &Out, ticket: &Ticket, by_id: &Tickets, include: &str) -> String {
    let sections: Vec<&str> = include
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect();
    let mut text = format!("{}  {}\n", out.rend(&ticket.id), ticket.title);
    text.push_str(&format!(
        "  status:   {}\n",
        out.status(&derive_status(ticket, by_id))
    ));
    text.push_str(&format!("  type:     {}\n", ticket.kind));
    text.push_str(&format!("  priority: {}\n", ticket.priority));
    if let Some(assignee) = &ticket.assignee {
        text.push_str(&format!("  assignee:   {assignee}\n"));
    }
    if let Some(parent) = &ticket.parent {
        text.push_str(&format!("  parent:   {}\n", out.rend(parent)));
    }
    if !ticket.labels.is_empty() {
        text.push_str(&format!("  labels:   {}\n", ticket.labels.join(", ")));
    }
    if let Some(reason) = &ticket.last_close_reason {
        text.push_str(&format!("  close:   {reason}\n"));
    }
    text.push_str(&format!("  created:  {}\n", ticket.created));
    text.push_str(&format!("  updated:  {}\n", ticket.updated));
    if sections.contains(&"deps") && !ticket.blocked_by.is_empty() {
        text.push_str("  blocked_by:\n");
        for dep in &ticket.blocked_by {
            let mark = by_id
                .get(dep)
                .map(|b| b.status.as_str())
                .unwrap_or("missing");
            text.push_str(&format!("    - {} ({mark})\n", out.rend(dep)));
        }
    }
    if sections.contains(&"body")
        && let Some(body) = &ticket.body
    {
        text.push('\n');
        text.push_str(body);
        text.push('\n');
    }
    if sections.contains(&"notes") && !ticket.comments.is_empty() {
        text.push_str("  notes:\n");
        let skip = ticket.comments.len().saturating_sub(10);
        for note in ticket.comments.iter().skip(skip) {
            text.push_str(&format!(
                "    [{}] {}: {}\n",
                note.ts, note.actor, note.text
            ));
        }
    }
    if sections.contains(&"fieldts") {
        text.push_str("  _fieldts:\n");
        for (key, ts) in &ticket.fieldts {
            text.push_str(&format!("    {key}: {ts}\n"));
        }
    }
    text
}

fn cmd_tree(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let by_id = api::load_tickets(&ctx.repo)?;
    let tree = api::tree(&by_id, parsed.positional(0).unwrap_or_default())?;
    if ctx.out.json {
        emit_json(&Json::Obj(vec![
            ("root".into(), ctx.out.full(tree.root)),
            (
                "children".into(),
                Json::Arr(tree.children.iter().map(|c| ctx.out.short(c)).collect()),
            ),
        ]));
        return Ok(0);
    }
    emit_line(&ctx.out.summary(tree.root, &by_id, None));
    for child in tree.children {
        emit_line(&format!("  {}", ctx.out.summary(child, &by_id, None)));
    }
    Ok(0)
}

fn cmd_list(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let by_id = api::load_tickets(&ctx.repo)?;
    let filter = Filter {
        status: parsed.value("status").map(String::from),
        label: parsed.value("label").map(String::from),
        parent: parsed.value("parent").map(String::from),
        kind: parsed.value("type").map(String::from),
        matches: parsed.value("match").map(String::from),
        ready: false,
    };
    let tickets = api::list_tickets(&by_id, &filter)?;
    if ctx.out.json {
        emit_json(&Json::Arr(
            tickets.iter().map(|t| ctx.out.short(t)).collect(),
        ));
    } else if tickets.is_empty() {
        emit_line("No tickets match.");
    } else {
        for t in tickets {
            emit_line(&ctx.out.summary(t, &by_id, None));
        }
    }
    Ok(0)
}

fn cmd_claim(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let result = api::claim(
        &ctx.repo,
        parsed.positional(0).unwrap_or_default(),
        &actor_of(parsed),
    )?;
    write_result_line(
        &ctx,
        &result,
        "Claimed",
        "Already claimed",
        " -> in_progress",
    );
    Ok(0)
}

fn cmd_set(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let mut assignments: Vec<Assignment> = parsed.positionals[1..]
        .iter()
        .map(|tok| api::parse_assignment(tok))
        .collect::<Result<_>>()?;
    if let Some(path) = parsed.value("body-file") {
        if assignments
            .iter()
            .any(|a| matches!(a, Assignment::Set { field, .. } if field == "body"))
        {
            return Err(usage("body= and --body-file are mutually exclusive"));
        }
        assignments.push(Assignment::Set {
            field: "body".into(),
            value: Scalar::Str(read_body_file(path)?),
        });
    } else if assignments.is_empty() {
        return Err(usage("set requires field=value assignments or --body-file"));
    }
    let result = api::set_fields(
        &ctx.repo,
        parsed.positional(0).unwrap_or_default(),
        &assignments,
        &actor_of(parsed),
    )?;
    write_result_line(&ctx, &result, "Updated", "No change to", "");
    Ok(0)
}

fn cmd_close(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let result = api::close(
        &ctx.repo,
        parsed.positional(0).unwrap_or_default(),
        parsed.value("reason"),
        &actor_of(parsed),
    )?;
    write_result_line(&ctx, &result, "Closed", "Already closed", " -> done");
    Ok(0)
}

fn cmd_drop(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let result = api::drop(
        &ctx.repo,
        parsed.positional(0).unwrap_or_default(),
        parsed.value("reason"),
        &actor_of(parsed),
    )?;
    write_result_line(&ctx, &result, "Dropped", "Already dropped", " -> dropped");
    Ok(0)
}

fn cmd_comment(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let text = match (parsed.positional(1), parsed.value("body-file")) {
        (Some(_), Some(_)) => {
            return Err(usage("comment text and --body-file are mutually exclusive"));
        }
        (None, Some(path)) => read_body_file(path)?,
        (Some(text), None) => text.to_string(),
        (None, None) => return Err(usage("comment requires note text or --body-file")),
    };
    let result = api::add_comment(
        &ctx.repo,
        parsed.positional(0).unwrap_or_default(),
        &text,
        &actor_of(parsed),
    )?;
    if ctx.out.json {
        emit_json(&ctx.out.full(&result.ticket));
    } else {
        emit_line(&format!("Noted on {}", ctx.out.rend(&result.ticket.id)));
    }
    Ok(0)
}

fn cmd_comments(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let ticket = api::show_ticket(&ctx.repo, parsed.positional(0).unwrap_or_default())?;
    if ctx.out.json {
        emit_json(&Json::Arr(
            ticket
                .comments
                .iter()
                .map(crate::fold::comment_to_json)
                .collect(),
        ));
    } else if ticket.comments.is_empty() {
        emit_line("No notes.");
    } else {
        for n in &ticket.comments {
            emit_line(&format!("[{}] {}: {}", n.ts, n.actor, n.text));
        }
    }
    Ok(0)
}

fn cmd_log(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let events = api::event_log(&ctx.repo, parsed.positional(0))?;
    if ctx.out.json {
        emit_json(&Json::Arr(events.iter().map(|e| e.to_json()).collect()));
    } else if events.is_empty() {
        emit_line("No events.");
    } else {
        for e in &events {
            let detail = e
                .reason
                .as_deref()
                .or(e.text.as_deref())
                .or(e.extra_str("remote"))
                .unwrap_or("");
            let suffix = if detail.is_empty() {
                String::new()
            } else {
                format!("  {detail}")
            };
            emit_line(&format!(
                "[{}] {} {} {} {}{suffix}",
                e.ts,
                e.id,
                e.actor,
                e.op,
                ctx.out.rend(&e.ticket)
            ));
        }
    }
    Ok(0)
}

fn cmd_doctor(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let findings = doctor::run(&ctx.repo)?;
    let all_ok = findings.iter().all(|f| f.ok);
    if ctx.out.json {
        emit_json(&Json::Arr(
            findings.iter().map(doctor::Finding::to_json).collect(),
        ));
    } else {
        emit(&doctor::render_report(&findings));
    }
    Ok(if all_ok { 0 } else { 1 })
}

fn cmd_compact(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let days = parsed
        .int_value("archive-after")?
        .unwrap_or(compact::DEFAULT_ARCHIVE_AFTER_DAYS);
    match compact::run(&ctx.repo, days, parsed.flag("force")) {
        Ok(result) => {
            if ctx.out.json {
                emit_json(&result.to_json());
            } else {
                emit_line(&format!(
                    "Compacted: archived {} event(s), kept {}.",
                    result.archived, result.remaining
                ));
                if !result.archive_files.is_empty() {
                    emit_line(&format!(
                        "  archive files: {}",
                        result.archive_files.join(", ")
                    ));
                }
            }
            Ok(0)
        }
        // A refusal is reported (as JSON when asked) rather than raised.
        Err(Error::Ticket(reason)) => {
            if ctx.out.json {
                emit_json(&Json::Obj(vec![("error".into(), json::s(&reason))]));
            } else {
                emit_err(&format!("rp compact: {reason}"));
            }
            Ok(1)
        }
        Err(other) => Err(other),
    }
}

fn cmd_stats(parsed: &Parsed) -> Result<i32> {
    let ctx = ctx(parsed)?;
    let data = stats::compute_stats(&ctx.repo, 5)?;
    if ctx.out.json {
        emit_json(&data);
    } else {
        emit(&stats::render(&data));
    }
    Ok(0)
}

fn dispatch(parsed: &Parsed) -> Result<i32> {
    match parsed.spec.name {
        "init" => cmd_init(parsed),
        "new" => cmd_new(parsed),
        "ready" => cmd_ready(parsed),
        "show" => cmd_show(parsed),
        "tree" => cmd_tree(parsed),
        "list" => cmd_list(parsed),
        "claim" => cmd_claim(parsed),
        "set" => cmd_set(parsed),
        "close" => cmd_close(parsed),
        "drop" => cmd_drop(parsed),
        "comment" => cmd_comment(parsed),
        "comments" => cmd_comments(parsed),
        "log" => cmd_log(parsed),
        "doctor" => cmd_doctor(parsed),
        "compact" => cmd_compact(parsed),
        "stats" => cmd_stats(parsed),
        other => Err(usage(&format!("unknown command '{other}'"))),
    }
}

/// Run the `rp` CLI on `args` (argv without the program name). Returns the exit code.
pub fn main(args: &[String]) -> i32 {
    let action = match parse_args(args) {
        Ok(action) => action,
        Err(err) => {
            emit_err(&format!("rp: {err}"));
            return err.exit_code();
        }
    };
    match action {
        Action::Help(spec) => {
            emit(&render_help(spec));
            0
        }
        Action::Version => {
            emit_line(&format!("rohrpost {}", crate::VERSION));
            0
        }
        Action::Run(parsed) => match dispatch(&parsed) {
            Ok(code) => code,
            Err(err) => {
                emit_err(&format!("rp: {err}"));
                err.exit_code()
            }
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Parsed> {
        let owned: Vec<String> = args.iter().map(|s| s.to_string()).collect();
        match parse_args(&owned)? {
            Action::Run(p) => Ok(p),
            _ => panic!("expected a command"),
        }
    }

    #[test]
    fn parses_flags_values_repeats_and_positionals() {
        let p = parse(&[
            "new",
            "A title",
            "--type",
            "bug",
            "-p",
            "1",
            "--label",
            "a",
            "--label=b",
            "--json",
        ])
        .unwrap();
        assert_eq!(p.positional(0), Some("A title"));
        assert_eq!(p.value("type"), Some("bug"));
        assert_eq!(p.int_value("priority").unwrap(), Some(1));
        assert_eq!(p.values("label"), vec!["a", "b"]);
        assert!(p.flag("json"));

        let p = parse(&[
            "set",
            "a1b2c3",
            "status=done",
            "labels+=x",
            "--body-file",
            "-",
        ])
        .unwrap();
        assert_eq!(&p.positionals[1..], ["status=done", "labels+=x"]);
        assert_eq!(p.value("body-file"), Some("-"));

        let p = parse(&["comment", "a1b2c3", "--", "-starts with dash"]).unwrap();
        assert_eq!(p.positional(1), Some("-starts with dash"));
    }

    #[test]
    fn usage_errors_are_exit_two() {
        for args in [
            &["bogus"][..],
            &["new", "t", "-é"],
            &["new", "t", "-"],
            &["show"],
            &["show", "a", "b"],
            &["new", "t", "--nope"],
            &["new", "t", "--type"],
            &["ready", "--limit", "x"],
        ] {
            let owned: Vec<String> = args.iter().map(|s| s.to_string()).collect();
            let err = match parse_args(&owned) {
                Err(e) => e,
                Ok(Action::Run(p)) => p.int_value("limit").unwrap_err(),
                Ok(_) => panic!("{args:?} parsed"),
            };
            assert_eq!(err.exit_code(), 2, "{args:?}");
        }
    }

    #[test]
    fn help_and_version_short_circuit() {
        assert!(matches!(parse_args(&[]).unwrap(), Action::Help(None)));
        assert!(matches!(
            parse_args(&["--version".to_string()]).unwrap(),
            Action::Version
        ));
        assert!(matches!(
            parse_args(&["new".to_string(), "-h".to_string()]).unwrap(),
            Action::Help(Some(_))
        ));
        assert!(render_help(None).contains("<command>"));
        assert!(render_help(spec("set")).contains("field=value ..."));
    }
}
