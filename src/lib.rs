//! rohrpost: a git-native ticket system for agentic coding workflows.
//!
//! The event log (`.rohrpost/log.jsonl`) is truth; tickets are a fold over it.
//! One write path: every mutation goes through [`api`], which appends an event
//! through [`store`]. The binary `rp` ([`cli`]) is a thin adapter over the
//! library. Everything is standard-library only, on purpose: `rp` is invoked
//! from bare containers on Linux, macOS and Windows and must carry no supply
//! chain. See `docs/spec/ROHRPOST-SPEC.md` for the design.
//!
//! Dependency direction is strictly downward:
//! `cli → api → {store, fold, config, paths}`; `fold → store → events → ids`.

pub mod api;
pub mod cli;
pub mod compact;
pub mod config;
pub mod doctor;
pub mod error;
pub mod events;
pub mod fold;
pub mod ids;
pub mod json;
pub mod paths;
pub mod stats;
pub mod store;
pub mod time;
pub mod toml;
pub mod util;

/// The version reported by `rp --version`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
