//! End-to-end tests: drive the built `rp` binary in throwaway git repositories.
//!
//! These pin the agent-facing contract (exit codes, `--json` shapes, the text
//! renderings agents and humans read) rather than internals; the algorithmic
//! pieces have unit tests next to their modules.

use std::io::Write as _;
use std::path::PathBuf;
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

use rohrpost::json::{self, Json};

static COUNTER: AtomicUsize = AtomicUsize::new(0);

/// A temp directory with `git init` + `rp init --prefix TST`; removed on drop.
struct Repo {
    dir: PathBuf,
}

impl Repo {
    fn bare() -> Repo {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let name = format!(
            "rp-test-{}-{}-{nanos}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        );
        let dir = std::env::temp_dir().join(name);
        std::fs::create_dir_all(&dir).unwrap();
        // Not canonicalised on purpose: on Windows that yields a `\\?\` path,
        // which child processes cannot use as a working directory.
        Repo { dir }
    }

    fn with_git() -> Repo {
        let repo = Repo::bare();
        repo.git(&["-c", "init.defaultBranch=main", "init", "-q"]);
        repo.git(&["config", "user.email", "t@e.st"]);
        repo.git(&["config", "user.name", "t"]);
        repo
    }

    fn new() -> Repo {
        let repo = Repo::with_git();
        repo.ok(&["init", "--prefix", "TST"]);
        repo
    }

    fn git(&self, args: &[&str]) -> String {
        let out = Command::new("git")
            .args(args)
            .current_dir(&self.dir)
            .output()
            .expect("git on PATH");
        assert!(
            out.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    }

    fn command(&self, args: &[&str]) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_rp"));
        cmd.args(args)
            .current_dir(&self.dir)
            .env("NO_COLOR", "1")
            .env_remove("ROHRPOST_ACTOR")
            .env_remove("ROHRPOST_RUNNER")
            .env_remove("ROHRPOST_BATCH");
        cmd
    }

    fn run(&self, args: &[&str]) -> Output {
        self.command(args).output().expect("spawn rp")
    }

    fn run_stdin(&self, args: &[&str], stdin: &[u8]) -> Output {
        let mut child = self
            .command(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child.stdin.take().unwrap().write_all(stdin).unwrap();
        child.wait_with_output().unwrap()
    }

    /// Run and assert exit 0; returns stdout.
    fn ok(&self, args: &[&str]) -> String {
        let out = self.run(args);
        assert_eq!(
            out.status.code(),
            Some(0),
            "rp {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap()
    }

    /// Run, assert the exit code, return stderr.
    fn fails(&self, args: &[&str], code: i32) -> String {
        let out = self.run(args);
        assert_eq!(
            out.status.code(),
            Some(code),
            "rp {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stderr).unwrap()
    }

    fn json(&self, args: &[&str]) -> Json {
        let mut full: Vec<&str> = args.to_vec();
        full.push("--json");
        json::parse(&self.ok(&full)).expect("valid JSON")
    }

    /// `rp new <title> ...` returning the bare id.
    fn new_ticket(&self, title: &str, extra: &[&str]) -> String {
        let mut args = vec!["new", title];
        args.extend_from_slice(extra);
        bare(&self.json(&args))
    }

    fn rohrpost(&self) -> PathBuf {
        self.dir.join(".rohrpost")
    }

    fn write(&self, rel: &str, content: &str) -> PathBuf {
        let path = self.dir.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, content).unwrap();
        path
    }
}

impl Drop for Repo {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn bare(ticket: &Json) -> String {
    ticket
        .get("id")
        .unwrap()
        .as_str()
        .unwrap()
        .rsplit('-')
        .next()
        .unwrap()
        .to_string()
}

fn ids(list: &Json) -> Vec<String> {
    list.as_array().unwrap().iter().map(bare).collect()
}

fn field<'a>(obj: &'a Json, key: &str) -> &'a Json {
    obj.get(key).unwrap_or_else(|| panic!("missing key {key}"))
}

fn event_line(id: &str, ts: &str, ticket: &str, op: &str, payload: &str) -> String {
    format!(
        r#"{{"id":"{id}","ts":"{ts}","ticket":"{ticket}","op":"{op}","actor":"user/t"{payload}}}"#
    )
}

// ---------------------------------------------------------------------------
// Top level.
// ---------------------------------------------------------------------------
#[test]
fn version_help_and_unknown_command() {
    let repo = Repo::bare();
    assert!(repo.ok(&["--version"]).starts_with("rohrpost "));
    assert!(repo.ok(&[]).contains("usage:"));
    assert!(repo.ok(&["--help"]).contains("<command>"));
    assert!(repo.ok(&["new", "-h"]).contains("--body-file"));
    assert!(repo.fails(&["frobnicate"], 2).contains("unknown command"));
    assert!(
        repo.fails(&["new", "t", "--nope"], 2)
            .contains("unrecognized option")
    );
}

#[test]
fn command_outside_a_repo_exits_one() {
    let repo = Repo::bare();
    assert!(
        repo.fails(&["list"], 1)
            .contains("not a rohrpost repository")
    );
}

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------
#[test]
fn init_scaffolds_layout_and_is_idempotent() {
    let repo = Repo::with_git();
    let first = repo.json(&["init", "--prefix", "fac"]);
    assert_eq!(field(&first, "prefix").as_str(), Some("FAC"));
    assert_eq!(field(&first, "created_config"), &Json::Bool(true));
    assert_eq!(field(&first, "updated_gitattributes"), &Json::Bool(true));
    for rel in [
        ".rohrpost/config.toml",
        ".rohrpost/log.jsonl",
        ".rohrpost/archive",
        ".rohrpost/templates",
        ".gitattributes",
    ] {
        assert!(repo.dir.join(rel).exists(), "{rel} missing");
    }
    let attrs = std::fs::read_to_string(repo.dir.join(".gitattributes")).unwrap();
    assert!(attrs.contains(".rohrpost/log.jsonl          merge=union text eol=lf"));
    assert!(attrs.contains(".rohrpost/archive/*.jsonl    merge=union text eol=lf"));
    assert!(!attrs.contains('\r'));
    // git resolves the attributes as intended.
    let check = repo.git(&[
        "check-attr",
        "merge",
        "text",
        "eol",
        "--",
        ".rohrpost/log.jsonl",
    ]);
    assert!(
        check.contains("merge: union") && check.contains("text: set") && check.contains("eol: lf"),
        "{check}"
    );

    let again = repo.json(&["init", "--prefix", "OTHER"]);
    assert_eq!(
        field(&again, "prefix").as_str(),
        Some("FAC"),
        "existing config is not clobbered"
    );
    assert_eq!(field(&again, "created_config"), &Json::Bool(false));
    assert_eq!(field(&again, "updated_gitattributes"), &Json::Bool(false));
}

#[test]
fn init_outside_git_uses_the_cwd_and_a_proposed_prefix() {
    let repo = Repo::bare();
    assert!(
        repo.fails(&["init", "--prefix", "1"], 1)
            .contains("prefix must be"),
        "invalid prefix on a fresh dir"
    );
    let text = repo.ok(&["init"]);
    assert!(text.contains("Initialised rohrpost at"));
    assert!(repo.rohrpost().join("config.toml").is_file());
    let config = std::fs::read_to_string(repo.rohrpost().join("config.toml")).unwrap();
    assert!(config.contains("prefix = \"RPTES\""), "{config}");
    assert!(
        repo.ok(&["init", "--prefix", "1"]).contains("prefix=RPTES"),
        "an existing config wins over --prefix"
    );
}

// ---------------------------------------------------------------------------
// The lifecycle.
// ---------------------------------------------------------------------------
#[test]
fn full_lifecycle_via_json() {
    let repo = Repo::new();
    let created = repo.json(&[
        "new",
        "Fix token refresh race",
        "--type",
        "bug",
        "-p",
        "1",
        "--label",
        "auth",
        "--body",
        "prose",
    ]);
    assert!(field(&created, "id").as_str().unwrap().starts_with("TST-"));
    assert_eq!(field(&created, "type").as_str(), Some("bug"));
    assert_eq!(field(&created, "status").as_str(), Some("open"));
    assert_eq!(field(&created, "priority").as_i64(), Some(1));
    assert_eq!(field(&created, "labels"), &json::str_list(["auth"]));
    assert_eq!(field(&created, "body").as_str(), Some("prose"));
    assert!(field(&created, "_fieldts").get("status").is_some());
    let tid = bare(&created);

    let claimed = repo.json(&["claim", &tid]);
    assert_eq!(field(&claimed, "status").as_str(), Some("in_progress"));
    assert!(
        field(&claimed, "assignee")
            .as_str()
            .unwrap()
            .starts_with("user/")
    );

    let noted = repo.json(&["comment", &tid, "retried with backoff"]);
    assert_eq!(field(&noted, "comments").as_array().unwrap().len(), 1);

    let closed = repo.json(&["close", &tid, "--reason", "shipped"]);
    assert_eq!(field(&closed, "status").as_str(), Some("done"));
    assert_eq!(
        field(&closed, "last_close_reason").as_str(),
        Some("shipped")
    );

    // Rendered ids are accepted on input too.
    let shown = repo.json(&["show", &format!("TST-{tid}")]);
    assert_eq!(field(&shown, "last_close_reason").as_str(), Some("shipped"));

    // The log for the ticket has every event, oldest first.
    let log = repo.json(&["log", &tid]);
    let ops: Vec<&str> = log
        .as_array()
        .unwrap()
        .iter()
        .map(|e| field(e, "op").as_str().unwrap())
        .collect();
    assert_eq!(ops, ["create", "set", "comment", "set"]);
    assert_eq!(
        field(&log.as_array().unwrap()[3], "reason").as_str(),
        Some("shipped")
    );
}

#[test]
fn mutations_are_idempotent() {
    let repo = Repo::new();
    let tid = repo.new_ticket("t", &[]);
    assert!(
        repo.ok(&["set", &tid, "status=in_progress"])
            .contains("Updated")
    );
    assert!(
        repo.ok(&["set", &tid, "status=in_progress"])
            .contains("No change to")
    );
    assert!(repo.ok(&["close", &tid]).contains("Closed"));
    assert!(repo.ok(&["close", &tid]).contains("Already closed"));
    assert!(repo.ok(&["drop", &tid]).contains("Dropped"));
    assert!(repo.ok(&["drop", &tid]).contains("Already dropped"));
    assert!(repo.ok(&["claim", &tid]).contains("Claimed"));
    assert!(repo.ok(&["claim", &tid]).contains("Already claimed"));
    // Four writes plus the create: the no-ops appended nothing.
    assert_eq!(repo.json(&["log", &tid]).as_array().unwrap().len(), 5);
}

#[test]
fn set_handles_scalars_set_ops_and_validation() {
    let repo = Repo::new();
    let epic = repo.new_ticket("Epic", &["--type", "epic"]);
    let dep = repo.new_ticket("dep", &[]);
    let tid = repo.new_ticket("t", &[]);
    let updated = repo.json(&[
        "set",
        &tid,
        "labels+=a,b",
        "priority=0",
        "title=Renamed",
        &format!("parent=TST-{epic}"),
        &format!("blocked_by+={dep}"),
    ]);
    assert_eq!(field(&updated, "labels"), &json::str_list(["a", "b"]));
    assert_eq!(field(&updated, "priority").as_i64(), Some(0));
    assert_eq!(field(&updated, "title").as_str(), Some("Renamed"));
    assert_eq!(
        field(&updated, "parent").as_str(),
        Some(format!("TST-{epic}").as_str())
    );
    assert_eq!(
        field(&updated, "blocked_by"),
        &json::str_list([format!("TST-{dep}")])
    );

    let updated = repo.json(&["set", &tid, "labels-=a,zzz", &format!("blocked_by-={dep}")]);
    assert_eq!(field(&updated, "labels"), &json::str_list(["b"]));
    assert_eq!(field(&updated, "blocked_by"), &Json::Arr(vec![]));

    assert!(
        repo.fails(&["set", &tid, "status=bogus"], 1)
            .contains("status must be one of")
    );
    assert!(
        repo.fails(&["set", &tid, "priority=9"], 1)
            .contains("priority must be 0..4")
    );
    assert!(
        repo.fails(&["set", &tid, "type=feature"], 1)
            .contains("type must be one of")
    );
    assert!(
        repo.fails(&["set", &tid, "colour=red"], 1)
            .contains("unknown field")
    );
    assert!(
        repo.fails(&["set", &tid, "title+=x"], 1)
            .contains("not a set field")
    );
    assert!(
        repo.fails(&["set", &tid], 2)
            .contains("requires field=value")
    );
    assert!(
        repo.fails(&["show", "zzzzzz"], 1)
            .contains("no such ticket")
    );
    assert!(
        repo.fails(&["new", "t", "--type", "story"], 1)
            .contains("type must be one of")
    );
    assert!(
        repo.fails(&["new", "   "], 1)
            .contains("title must be non-empty")
    );
}

#[test]
fn ready_is_derived_from_status_type_and_blockers() {
    let repo = Repo::new();
    let epic = repo.new_ticket("Epic", &["--type", "epic"]);
    let blocker = repo.new_ticket("blocker", &["-p", "1"]);
    let blocked = repo.new_ticket(
        "blocked",
        &["--blocked-by", &blocker, "-p", "0", "--parent", &epic],
    );
    let waiting = repo.new_ticket("waiting", &[]);
    repo.ok(&["set", &waiting, "status=waiting"]);
    let dropped_dep = repo.new_ticket("dropped dep", &[]);
    let stuck = repo.new_ticket("stuck", &["--blocked-by", &dropped_dep]);
    repo.ok(&["drop", &dropped_dep]);

    assert_eq!(
        ids(&repo.json(&["ready"])),
        vec![blocker.clone()],
        "epics, waiting and blocked tickets are excluded"
    );
    repo.ok(&["close", &blocker]);
    assert_eq!(
        ids(&repo.json(&["ready"])),
        vec![blocked.clone()],
        "closing the blocker unblocks; a dropped blocker does not"
    );
    assert!(!ids(&repo.json(&["ready"])).contains(&stuck));
    assert_eq!(
        ids(&repo.json(&["ready", "--limit", "0"])),
        Vec::<String>::new()
    );

    // Epic status is derived from its children.
    let tree = repo.json(&["tree", &epic]);
    assert_eq!(field(field(&tree, "root"), "status").as_str(), Some("open"));
    assert_eq!(ids(field(&tree, "children")), vec![blocked.clone()]);
    repo.ok(&["close", &blocked]);
    assert_eq!(
        ids(&repo.json(&["list", "--status", "done", "--type", "epic"])),
        vec![epic.clone()]
    );
    assert!(repo.ok(&["tree", &epic]).contains("[done]"));
}

#[test]
fn list_filters_compose() {
    let repo = Repo::new();
    let epic = repo.new_ticket("Epic", &["--type", "epic"]);
    let a = repo.new_ticket(
        "Token Refresh race",
        &["--label", "auth", "--parent", &epic, "-p", "3"],
    );
    let b = repo.new_ticket("Other", &["--label", "auth", "-p", "1"]);
    let _c = repo.new_ticket("Third", &["--type", "spike"]);
    assert_eq!(
        ids(&repo.json(&["list", "--label", "auth"])),
        vec![b.clone(), a.clone()],
        "priority order"
    );
    assert_eq!(
        ids(&repo.json(&["list", "--match", "token refresh"])),
        vec![a.clone()]
    );
    assert_eq!(
        ids(&repo.json(&["list", "--parent", &format!("TST-{epic}")])),
        vec![a.clone()]
    );
    assert_eq!(ids(&repo.json(&["list", "--type", "spike"])).len(), 1);
    assert_eq!(
        ids(&repo.json(&["list", "--status", "ready", "--label", "auth"])).len(),
        2,
        "derived statuses are queryable"
    );
    assert!(
        repo.ok(&["list", "--label", "nonexistent"])
            .contains("No tickets match.")
    );
}

#[test]
fn list_and_ready_omit_prose_show_carries_it() {
    let repo = Repo::new();
    let body = "x".repeat(5000);
    let tid = repo.new_ticket("with body", &["--body", &body]);
    repo.ok(&["comment", &tid, "a note"]);
    for cmd in [&["ready"][..], &["list"]] {
        let rows = repo.json(cmd);
        let row = &rows.as_array().unwrap()[0];
        assert!(row.get("body").is_none(), "{cmd:?} carries a body");
        assert!(row.get("comments").is_none());
        assert!(row.get("_fieldts").is_none());
    }
    let shown = repo.json(&["show", &tid]);
    assert_eq!(field(&shown, "body").as_str(), Some(body.as_str()));
    assert_eq!(field(&shown, "comments").as_array().unwrap().len(), 1);
}

#[test]
fn templates_supply_defaults_and_flags_win() {
    let repo = Repo::new();
    repo.write(".rohrpost/templates/bug.toml", "[defaults]\ntype = \"bug\"\npriority = 1\nlabels = [\"auth\"]\nbody = \"\"\"\ntemplate body\n\"\"\"\n");
    let t = repo.json(&["new", "A bug", "--template", "bug"]);
    assert_eq!(field(&t, "type").as_str(), Some("bug"));
    assert_eq!(field(&t, "priority").as_i64(), Some(1));
    assert_eq!(field(&t, "labels"), &json::str_list(["auth"]));
    assert_eq!(field(&t, "body").as_str(), Some("template body\n"));

    let t = repo.json(&[
        "new",
        "A bug",
        "--template",
        "bug",
        "-p",
        "3",
        "--label",
        "ui",
        "--body",
        "explicit",
    ]);
    assert_eq!(field(&t, "priority").as_i64(), Some(3));
    assert_eq!(field(&t, "labels"), &json::str_list(["ui"]));
    assert_eq!(field(&t, "body").as_str(), Some("explicit"));

    repo.write(".rohrpost/templates/bad.toml", "priority = \"high\"\n");
    assert!(
        repo.fails(&["new", "t", "--template", "bad"], 1)
            .contains("priority must be an integer")
    );
    repo.write(".rohrpost/templates/odd.toml", "colour = 1\n");
    assert!(
        repo.fails(&["new", "t", "--template", "odd"], 1)
            .contains("unknown template field")
    );
    assert!(
        repo.fails(&["new", "t", "--template", "missing"], 1)
            .contains("no such template")
    );
    assert!(
        repo.fails(&["new", "t", "--template", "../config"], 1)
            .contains("simple filename")
    );
}

// ---------------------------------------------------------------------------
// --body-file: multi-line input without heredocs.
// ---------------------------------------------------------------------------
#[test]
fn body_file_reads_paths_and_stdin_as_strict_utf8() {
    let repo = Repo::new();
    let path = repo.write("body.md", "## Context\n\nline two\n");
    let t = repo.json(&["new", "t", "--body-file", path.to_str().unwrap()]);
    assert_eq!(field(&t, "body").as_str(), Some("## Context\n\nline two\n"));

    let out = repo.run_stdin(
        &["new", "t", "--body-file", "-", "--json"],
        "café ☕\n".as_bytes(),
    );
    assert_eq!(out.status.code(), Some(0));
    let t = json::parse(std::str::from_utf8(&out.stdout).unwrap()).unwrap();
    assert_eq!(field(&t, "body").as_str(), Some("café ☕\n"));

    let empty = repo.write("empty.md", "");
    let t = repo.json(&["new", "t", "--body-file", empty.to_str().unwrap()]);
    assert!(field(&t, "body").is_null(), "an empty file yields no body");

    let latin = repo.dir.join("latin1.md");
    std::fs::write(&latin, b"caf\xe9").unwrap();
    let err = repo.fails(&["new", "t", "--body-file", latin.to_str().unwrap()], 2);
    assert!(err.contains("UTF-8") && err.contains("latin1.md"), "{err}");

    let err = repo.fails(&["new", "t", "--body-file", "no/such/file.md"], 2);
    assert!(err.contains("no/such/file.md"));
    let err = repo.fails(&["new", "t", "--body", "x", "--body-file", "-"], 2);
    assert!(err.contains("--body") && err.contains("--body-file"));
}

#[test]
fn body_file_on_set_and_comment() {
    let repo = Repo::new();
    let tid = repo.new_ticket("t", &["--body", "old"]);
    let path = repo.write("body.md", "## Decision\n\nuse a flag\n");
    repo.ok(&[
        "set",
        &tid,
        "status=in_progress",
        "--body-file",
        path.to_str().unwrap(),
    ]);
    let shown = repo.json(&["show", &tid]);
    assert_eq!(field(&shown, "status").as_str(), Some("in_progress"));
    assert_eq!(
        field(&shown, "body").as_str(),
        Some("## Decision\n\nuse a flag\n")
    );
    assert!(
        repo.fails(
            &[
                "set",
                &tid,
                "body=inline",
                "--body-file",
                path.to_str().unwrap()
            ],
            2
        )
        .contains("body=")
    );

    let empty = repo.write("empty.md", "");
    repo.ok(&["set", &tid, "--body-file", empty.to_str().unwrap()]);
    assert!(
        field(&repo.json(&["show", &tid]), "body").is_null(),
        "an empty file clears the body"
    );

    let note = repo.write("note.md", "retried, still 429s\nwith detail");
    repo.ok(&["comment", &tid, "--body-file", note.to_str().unwrap()]);
    let out = repo.run_stdin(&["comment", &tid, "--body-file", "-"], b"piped note");
    assert_eq!(out.status.code(), Some(0));
    let notes = repo.json(&["comments", &tid]);
    let texts: Vec<&str> = notes
        .as_array()
        .unwrap()
        .iter()
        .map(|n| field(n, "text").as_str().unwrap())
        .collect();
    assert_eq!(texts, ["retried, still 429s\nwith detail", "piped note"]);
    assert!(
        repo.fails(
            &[
                "comment",
                &tid,
                "text",
                "--body-file",
                note.to_str().unwrap()
            ],
            2
        )
        .contains("--body-file")
    );
    assert!(repo.fails(&["comment", &tid], 2).contains("--body-file"));
}

// ---------------------------------------------------------------------------
// Text output.
// ---------------------------------------------------------------------------
#[test]
fn text_renderings_for_humans() {
    let repo = Repo::new();
    assert!(repo.ok(&["ready"]).contains("The tube is empty."));
    let epic = repo.new_ticket("Epic", &["--type", "epic"]);
    let dep = repo.new_ticket("Dependency", &[]);
    let child = repo.new_ticket(
        "Child",
        &[
            "--parent",
            &epic,
            "--label",
            "auth",
            "--blocked-by",
            &dep,
            "--body",
            "some prose body",
            "--assignee",
            "user/x",
        ],
    );
    repo.ok(&["comment", &child, "a note"]);
    repo.ok(&["close", &dep, "--reason", "shipped"]);

    let ready = repo.ok(&["ready"]);
    assert!(
        ready.contains(&format!("TST-{child}  [ready]  task  p2  Child")),
        "{ready}"
    );
    assert!(!ready.contains('\x1b'), "no ANSI when NO_COLOR is set");

    let shown = repo.ok(&["show", &child, "--include", "body,deps,notes,fieldts"]);
    for needle in [
        "status:   open",
        "assignee:   user/x",
        &format!("parent:   TST-{epic}"),
        "labels:   auth",
        "blocked_by:",
        &format!("- TST-{dep} (done)"),
        "\nsome prose body\n",
        "notes:",
        "a note",
        "_fieldts:",
        "    status: ",
    ] {
        assert!(shown.contains(needle), "missing {needle:?} in:\n{shown}");
    }
    let shown_dep = repo.ok(&["show", &dep]);
    assert!(shown_dep.contains("close:   shipped"));
    assert!(
        !shown_dep.contains("notes:"),
        "the default show omits notes"
    );

    let tree = repo.ok(&["tree", &epic]);
    assert!(tree.starts_with(&format!(
        "TST-{epic}  [open]  epic  p2  Epic\n  TST-{child}"
    )));
    assert!(repo.ok(&["comments", &child]).contains("] user/"));
    assert!(repo.ok(&["comments", &dep]).contains("No notes."));
    let log = repo.ok(&["log", &dep]);
    assert!(
        log.contains(" create TST-") && log.contains(" set TST-") && log.ends_with("shipped\n"),
        "{log}"
    );
    assert!(
        repo.ok(&["new", "Created line"])
            .starts_with("Created TST-")
    );
    assert!(
        repo.ok(&["comment", &child, "x"])
            .starts_with("Noted on TST-")
    );
}

#[test]
fn ansi_colour_is_off_when_stdout_is_not_a_terminal() {
    let repo = Repo::new();
    repo.new_ticket("t", &[]);
    let out = repo
        .command(&["ready"])
        .env_remove("NO_COLOR")
        .output()
        .unwrap();
    assert!(!String::from_utf8_lossy(&out.stdout).contains('\x1b'));
}

#[test]
fn unicode_round_trips() {
    let repo = Repo::new();
    let tid = repo.new_ticket("unicode: café 🎉 \"quoted\" back\\slash", &[]);
    assert_eq!(
        field(&repo.json(&["show", &tid]), "title").as_str(),
        Some("unicode: café 🎉 \"quoted\" back\\slash")
    );
    assert!(repo.ok(&["show", &tid]).contains("🎉"));
}

// ---------------------------------------------------------------------------
// Actors.
// ---------------------------------------------------------------------------
#[test]
fn actor_resolution_precedence() {
    let repo = Repo::new();
    let tid = repo.new_ticket("t", &[]);
    let actor_of = |cmd: &mut Command| -> String {
        let out = cmd.output().unwrap();
        assert!(out.status.success());
        let log = json::parse(std::str::from_utf8(&out.stdout).unwrap()).unwrap();
        field(log.as_array().unwrap().last().unwrap(), "actor")
            .as_str()
            .unwrap()
            .to_string()
    };
    let last_actor = |repo: &Repo| actor_of(&mut repo.command(&["log", &tid, "--json"]));

    repo.ok(&["comment", &tid, "human"]);
    assert_eq!(last_actor(&repo), "user/t@e.st");
    assert!(
        repo.command(&["comment", &tid, "runner", "--json"])
            .env("ROHRPOST_RUNNER", "claude")
            .env("ROHRPOST_BATCH", "b-3")
            .status()
            .unwrap()
            .success()
    );
    assert_eq!(last_actor(&repo), "runner/claude@b-3");
    assert!(
        repo.command(&["comment", &tid, "env", "--json"])
            .env("ROHRPOST_ACTOR", "remote/x")
            .env("ROHRPOST_RUNNER", "claude")
            .status()
            .unwrap()
            .success()
    );
    assert_eq!(last_actor(&repo), "remote/x");
    repo.ok(&["comment", &tid, "explicit", "--actor", "user/explicit"]);
    assert_eq!(last_actor(&repo), "user/explicit");
}

// ---------------------------------------------------------------------------
// doctor
// ---------------------------------------------------------------------------
#[test]
fn doctor_passes_on_a_fresh_repo_and_reports_problems() {
    let repo = Repo::new();
    let a = repo.new_ticket("a", &[]);
    let b = repo.new_ticket("b", &["--blocked-by", &a]);
    assert!(repo.ok(&["doctor"]).contains("all clear"));
    let findings = repo.json(&["doctor"]);
    assert!(
        findings
            .as_array()
            .unwrap()
            .iter()
            .all(|f| field(f, "ok") == &Json::Bool(true))
    );

    // A dangling reference, a cycle, and a malformed line.
    repo.ok(&["set", &a, &format!("blocked_by+={b}")]);
    let out = repo.run(&["doctor", "--json"]);
    assert_eq!(out.status.code(), Some(1));
    let findings = json::parse(std::str::from_utf8(&out.stdout).unwrap()).unwrap();
    let failing: Vec<&str> = findings
        .as_array()
        .unwrap()
        .iter()
        .filter(|f| field(f, "ok") == &Json::Bool(false))
        .map(|f| field(f, "check").as_str().unwrap())
        .collect();
    assert_eq!(failing, ["no_cycles"]);

    repo.ok(&["set", &a, &format!("blocked_by-={b}"), "blocked_by+=zzzzzz"]);
    let log = repo.rohrpost().join("log.jsonl");
    let mut text = std::fs::read_to_string(&log).unwrap();
    text.push_str("{not json\n");
    std::fs::write(&log, text).unwrap();
    let out = repo.run(&["doctor"]);
    assert_eq!(out.status.code(), Some(1));
    let report = String::from_utf8_lossy(&out.stdout);
    assert!(
        report.contains("[XX ] log_parses: 1 malformed line(s)"),
        "{report}"
    );
    assert!(report.contains("skipped (log unparseable)"));
    assert!(report.contains("need attention."));
    assert!(
        repo.fails(&["list"], 1).contains("malformed event log"),
        "reads fail loudly on a corrupt log"
    );
}

#[test]
fn doctor_flags_missing_gitattributes_rules() {
    let repo = Repo::new();
    std::fs::write(repo.dir.join(".gitattributes"), "*.txt text\n").unwrap();
    let out = repo.run(&["doctor", "--json"]);
    assert_eq!(out.status.code(), Some(1));
    let findings = json::parse(std::str::from_utf8(&out.stdout).unwrap()).unwrap();
    let ga = findings
        .as_array()
        .unwrap()
        .iter()
        .find(|f| field(f, "check").as_str() == Some("gitattributes"))
        .unwrap();
    assert_eq!(field(ga, "ok"), &Json::Bool(false));
    assert!(
        field(ga, "detail")
            .as_str()
            .unwrap()
            .contains("missing rule")
    );
}

// ---------------------------------------------------------------------------
// Legacy logs written by the removed sync layer.
// ---------------------------------------------------------------------------
#[test]
fn legacy_sync_events_are_kept_but_ignored() {
    let repo = Repo::new();
    let lines = [
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAA1",
            "2026-01-01T00:00:01.000Z",
            "a1b2c3",
            "create",
            r#","set":{"title":"legacy","type":"task","status":"open","priority":2}"#,
        ),
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAA2",
            "2026-01-01T00:00:02.000Z",
            "a1b2c3",
            "link",
            r#","remote":"github","ref":"42""#,
        ),
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAA3",
            "2026-01-01T00:00:03.000Z",
            "__sync__",
            "synced",
            r#","remote":"github","at":"2026-01-01T00:00:03.000Z""#,
        ),
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAA4",
            "2026-01-01T00:00:04.000Z",
            "a1b2c3",
            "unlink",
            r#","remote":"github""#,
        ),
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAA1",
            "2026-01-01T00:00:01.000Z",
            "a1b2c3",
            "create",
            r#","set":{"title":"legacy","type":"task","status":"open","priority":2}"#,
        ),
    ];
    std::fs::write(repo.rohrpost().join("log.jsonl"), lines.join("\n") + "\n").unwrap();

    let tickets = repo.json(&["list"]);
    assert_eq!(
        ids(&tickets),
        vec!["a1b2c3".to_string()],
        "the watermark is not a ticket; the duplicate create is folded once"
    );
    assert_eq!(
        field(&tickets.as_array().unwrap()[0], "updated").as_str(),
        Some("2026-01-01T00:00:04.000Z")
    );
    assert!(tickets.as_array().unwrap()[0].get("remotes").is_none());

    let log = repo.json(&["log"]);
    assert_eq!(
        log.as_array().unwrap().len(),
        5,
        "rp log shows the raw history, duplicates included"
    );
    let link = log
        .as_array()
        .unwrap()
        .iter()
        .find(|e| field(e, "op").as_str() == Some("link"))
        .unwrap();
    assert_eq!(
        field(link, "remote").as_str(),
        Some("github"),
        "unknown keys round-trip"
    );
    assert_eq!(
        repo.json(&["log", "a1b2c3"]).as_array().unwrap().len(),
        4,
        "the watermark never attributes to a ticket"
    );

    let out = repo.run(&["doctor", "--json"]);
    assert_eq!(
        out.status.code(),
        Some(1),
        "the duplicate line is a real finding"
    );
    let findings = json::parse(std::str::from_utf8(&out.stdout).unwrap()).unwrap();
    let names: Vec<&str> = findings
        .as_array()
        .unwrap()
        .iter()
        .filter(|f| field(f, "ok") == &Json::Bool(false))
        .map(|f| field(f, "check").as_str().unwrap())
        .collect();
    assert_eq!(names, ["no_duplicate_ids"]);
    assert!(
        findings
            .as_array()
            .unwrap()
            .iter()
            .any(|f| field(f, "check").as_str() == Some("legacy_sync_events"))
    );
}

// ---------------------------------------------------------------------------
// compact
// ---------------------------------------------------------------------------
fn seed_old_and_recent(repo: &Repo) -> (String, String) {
    let old = "a1d001".to_string();
    let recent = "b2c001".to_string();
    let lines = [
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAB1",
            "2025-02-01T00:00:00.000Z",
            &old,
            "create",
            r#","set":{"title":"old","type":"task","status":"open","priority":2}"#,
        ),
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAB2",
            "2025-02-02T00:00:00.000Z",
            &old,
            "comment",
            r#","text":"note""#,
        ),
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAB3",
            "2025-05-03T00:00:00.000Z",
            &old,
            "set",
            r#","set":{"status":"done"},"reason":"long ago""#,
        ),
        event_line(
            "01AAAAAAAAAAAAAAAAAAAAAAB4",
            "2025-05-04T00:00:00.000Z",
            &recent,
            "create",
            r#","set":{"title":"recent","type":"task","status":"open","priority":2}"#,
        ),
    ];
    std::fs::write(repo.rohrpost().join("log.jsonl"), lines.join("\n") + "\n").unwrap();
    repo.ok(&["close", &recent]); // terminal, but too recent to archive
    (old, recent)
}

#[test]
fn compact_archives_long_terminal_tickets_by_quarter() {
    let repo = Repo::new();
    let (old, recent) = seed_old_and_recent(&repo);

    // Untracked files make the tree dirty: the guard refuses without --force.
    let err = repo.fails(&["compact"], 1);
    assert!(
        err.contains("refusing to compact: working tree is dirty"),
        "{err}"
    );
    let out = repo.run(&["compact", "--json"]);
    assert_eq!(out.status.code(), Some(1));
    assert!(
        json::parse(std::str::from_utf8(&out.stdout).unwrap())
            .unwrap()
            .get("error")
            .is_some()
    );

    let result = repo.json(&["compact", "--force"]);
    assert_eq!(field(&result, "archived").as_i64(), Some(3));
    assert_eq!(field(&result, "remaining").as_i64(), Some(2));
    assert_eq!(
        field(&result, "archive_files"),
        &json::str_list(["log-2025-Q1.jsonl", "log-2025-Q2.jsonl"])
    );
    let q1 = std::fs::read_to_string(repo.rohrpost().join("archive/log-2025-Q1.jsonl")).unwrap();
    assert_eq!(q1.lines().count(), 2);
    let live = std::fs::read_to_string(repo.rohrpost().join("log.jsonl")).unwrap();
    assert_eq!(live.lines().count(), 2);
    assert!(!live.contains('\r'));

    // Archive + log still fold to the same tickets; doctor is happy; a rerun archives nothing.
    let mut all = ids(&repo.json(&["list"]));
    all.sort();
    assert_eq!(all, vec![old.clone(), recent.clone()]);
    assert_eq!(
        field(&repo.json(&["show", &old]), "last_close_reason").as_str(),
        Some("long ago")
    );
    assert!(repo.ok(&["doctor"]).contains("all clear"));
    let again = repo.json(&["compact", "--force"]);
    assert_eq!(field(&again, "archived").as_i64(), Some(0));
    assert_eq!(field(&again, "remaining").as_i64(), Some(2));

    // --archive-after 0 catches the recently closed ticket too.
    let third = repo.json(&["compact", "--force", "--archive-after", "0"]);
    assert_eq!(field(&third, "archived").as_i64(), Some(2));
    assert_eq!(
        repo.ok(&["compact", "--force"]),
        "Compacted: archived 0 event(s), kept 0.\n"
    );
}

#[test]
fn compact_guard_accepts_a_clean_default_branch_and_refuses_others() {
    let repo = Repo::new();
    seed_old_and_recent(&repo);
    repo.git(&["add", "-A"]);
    repo.git(&["-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"]);
    assert!(
        repo.ok(&["compact"])
            .starts_with("Compacted: archived 3 event(s), kept 2.")
    );

    repo.git(&["add", "-A"]);
    repo.git(&[
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "compacted",
    ]);
    repo.git(&["checkout", "-q", "-b", "feature"]);
    let err = repo.fails(&["compact"], 1);
    assert!(err.contains("HEAD is on 'feature', not 'main'"), "{err}");

    // The configured default branch is honoured.
    let cfg = repo.rohrpost().join("config.toml");
    let mut text = std::fs::read_to_string(&cfg).unwrap();
    text.push_str("default_branch = \"feature\"\n");
    std::fs::write(&cfg, text).unwrap();
    repo.git(&["add", "-A"]);
    repo.git(&["-c", "commit.gpgsign=false", "commit", "-q", "-m", "cfg"]);
    assert!(repo.ok(&["compact"]).starts_with("Compacted"));
}

// ---------------------------------------------------------------------------
// stats
// ---------------------------------------------------------------------------
#[test]
fn stats_reports_distributions_and_fold_timing() {
    let repo = Repo::new();
    repo.new_ticket("a", &["--body", &"x".repeat(5000)]);
    repo.new_ticket("b", &["--body", "short"]);
    let data = repo.json(&["stats"]);
    assert_eq!(field(&data, "tickets").as_i64(), Some(2));
    assert_eq!(field(&data, "events").as_i64(), Some(2));
    assert_eq!(field(&data, "pipe_buf").as_i64(), Some(4096));
    let body = field(&data, "body_bytes");
    assert_eq!(field(body, "count").as_i64(), Some(2));
    assert_eq!(field(body, "max").as_i64(), Some(5000));
    let line = field(&data, "event_line_bytes");
    assert_eq!(field(line, "over_pipe_buf").as_i64(), Some(1));
    assert!(matches!(field(line, "lock_share_pct"), Json::Float(f) if (*f - 50.0).abs() < 1e-9));
    assert!(matches!(field(&data, "fold_ms"), Json::Float(f) if *f >= 0.0));
    assert!(repo.ok(&["stats"]).contains("cold fold:"));
}

// ---------------------------------------------------------------------------
// Concurrency: many processes appending at once never corrupt the log.
// ---------------------------------------------------------------------------
#[test]
fn concurrent_writers_serialise_cleanly() {
    let repo = Repo::new();
    let tid = repo.new_ticket("shared", &["--body", &"y".repeat(3000)]);
    let bin = env!("CARGO_BIN_EXE_rp");
    let dir = repo.dir.clone();
    let workers: Vec<_> = (0..12)
        .map(|i| {
            let (bin, dir, tid) = (bin.to_string(), dir.clone(), tid.clone());
            std::thread::spawn(move || {
                let long = format!("note {i} {}", "z".repeat(6000));
                let args: Vec<String> = if i % 3 == 0 {
                    vec!["new".into(), format!("parallel {i}"), "--body".into(), long]
                } else {
                    vec!["comment".into(), tid.clone(), long]
                };
                Command::new(&bin)
                    .args(&args)
                    .current_dir(&dir)
                    .env_remove("ROHRPOST_ACTOR")
                    .status()
                    .unwrap()
                    .success()
            })
        })
        .collect();
    assert!(workers.into_iter().all(|w| w.join().unwrap()));
    let text = std::fs::read_to_string(repo.rohrpost().join("log.jsonl")).unwrap();
    assert_eq!(text.lines().count(), 13);
    assert!(text.lines().all(|l| json::parse(l).is_ok()));
    assert_eq!(
        field(&repo.json(&["show", &tid]), "comments")
            .as_array()
            .unwrap()
            .len(),
        8
    );
    assert!(repo.ok(&["doctor"]).contains("all clear"));
}
