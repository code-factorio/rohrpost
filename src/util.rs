//! Actor resolution and small process helpers.
//!
//! The actor namespace (`user/*`, `runner/*`) is load-bearing (spec §5.2): it
//! distinguishes a human decision from a runner write. It is resolved from the
//! environment and git config so nothing ever hardcodes a name.

use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::OnceLock;

/// Run `git` with `args` (optionally in `cwd`) and return trimmed stdout, or
/// `None` if git is missing or exits non-zero. Never raises.
pub fn git_output(cwd: Option<&Path>, args: &[&str]) -> Option<String> {
    let mut cmd = Command::new("git");
    cmd.args(args).stdin(Stdio::null()).stderr(Stdio::null());
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    let output = cmd.output().ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn git_email() -> Option<&'static str> {
    static EMAIL: OnceLock<Option<String>> = OnceLock::new();
    EMAIL
        .get_or_init(|| git_output(None, &["config", "user.email"]).filter(|e| !e.is_empty()))
        .as_deref()
}

/// Resolve the actor for an event: explicit override > env > git > OS user.
///
/// 1. `explicit` (`--actor`) is used verbatim.
/// 2. `ROHRPOST_ACTOR` is used verbatim.
/// 3. `ROHRPOST_RUNNER` → `runner/<name>`, plus `@<ROHRPOST_BATCH>` when set.
/// 4. `user/<git config user.email>`, then `user/<login>`, then `user/unknown`.
pub fn resolve_actor(explicit: Option<&str>) -> String {
    if let Some(actor) = explicit.filter(|a| !a.is_empty()) {
        return actor.to_string();
    }
    let env = |key: &str| std::env::var(key).ok().filter(|v| !v.is_empty());
    if let Some(actor) = env("ROHRPOST_ACTOR") {
        return actor;
    }
    if let Some(runner) = env("ROHRPOST_RUNNER") {
        return match env("ROHRPOST_BATCH") {
            Some(batch) => format!("runner/{runner}@{batch}"),
            None => format!("runner/{runner}"),
        };
    }
    if let Some(email) = git_email() {
        return format!("user/{email}");
    }
    if let Some(login) = env("USER").or_else(|| env("USERNAME")) {
        return format!("user/{login}");
    }
    "user/unknown".to_string()
}
