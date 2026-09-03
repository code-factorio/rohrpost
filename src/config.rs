//! Project configuration: `.rohrpost/config.toml`.
//!
//! Committed, hand-editable. The display prefix is **display-only** (spec §5.1):
//! it never enters the log, so a config edit re-renders every ticket id with
//! no migration. Unknown tables (e.g. a `[remotes.*]` left over from the removed
//! sync layer) are ignored.

use std::path::Path;

use crate::error::{Error, Result};
use crate::paths;
use crate::toml::{self, Toml};

/// The fallback prefix when no config exists (the `RP-` of spec §2).
pub const DEFAULT_PREFIX: &str = "RP";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    pub prefix: String,
    /// The branch `rp compact` insists on (defaults to `main`).
    pub default_branch: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            prefix: DEFAULT_PREFIX.to_string(),
            default_branch: None,
        }
    }
}

/// Normalise and validate a prefix: two to five uppercase ASCII letters.
pub fn validate_prefix(prefix: &str) -> Result<String> {
    let candidate = prefix.trim().to_ascii_uppercase();
    let ok =
        (2..=5).contains(&candidate.len()) && candidate.bytes().all(|b| b.is_ascii_uppercase());
    if ok {
        Ok(candidate)
    } else {
        Err(Error::Config(format!(
            "prefix must be 2-5 uppercase letters (e.g. 'FAC'), got '{prefix}'"
        )))
    }
}

/// Load `config.toml`; a missing file yields the defaults, a malformed one an error.
pub fn load_config(rohrpost_dir: &Path) -> Result<Config> {
    let path = paths::config_path(rohrpost_dir);
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Config::default()),
        Err(e) => {
            return Err(Error::Config(format!(
                "cannot read {}: {e}",
                path.display()
            )));
        }
    };
    let root = toml::parse(&text)
        .map_err(|e| Error::Config(format!("invalid {}: {e}", paths::CONFIG_FILENAME)))?;
    let mut config = Config::default();
    let Some(project) = root.get("project") else {
        return Ok(config);
    };
    let Toml::Table(project) = project else {
        return Err(Error::Config("[project] must be a table".into()));
    };
    if let Some(prefix) = project.get("prefix") {
        let raw = prefix
            .as_str()
            .ok_or_else(|| Error::Config("[project].prefix must be a string".into()))?;
        config.prefix = validate_prefix(raw)?;
    }
    if let Some(branch) = project.get("default_branch") {
        let raw = branch
            .as_str()
            .ok_or_else(|| Error::Config("[project].default_branch must be a string".into()))?;
        config.default_branch = Some(raw.to_string());
    }
    Ok(config)
}

/// Load config, falling back to defaults if unreadable (read paths must work).
pub fn load_config_or_default(rohrpost_dir: &Path) -> Config {
    load_config(rohrpost_dir).unwrap_or_default()
}

/// The minimal committed `config.toml` that `rp init` writes.
pub fn render_config_toml(prefix: &str) -> String {
    format!(
        "# Rohrpost project configuration. Committed; safe to hand-edit.\n\
         # The prefix is DISPLAY-ONLY: it never enters the event log, so\n\
         # renaming it here re-renders every ticket id with no migration.\n\
         \n\
         [project]\n\
         prefix = \"{prefix}\"\n\
         # default_branch = \"main\"   # the branch `rp compact` requires\n"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prefix_validation() {
        assert_eq!(validate_prefix(" fac ").unwrap(), "FAC");
        for bad in ["A", "TOOLONG", "F4C", "", "ÄB"] {
            assert!(validate_prefix(bad).is_err(), "accepted {bad:?}");
        }
    }

    #[test]
    fn rendered_config_reloads() {
        let root = toml::parse(&render_config_toml("FAC")).unwrap();
        assert_eq!(
            root["project"].as_table().unwrap()["prefix"].as_str(),
            Some("FAC")
        );
    }
}
