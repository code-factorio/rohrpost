---
name: chrome-agent-testing
description: Use when the user wants automated app testing in a browser, browser automation or E2E test scripts, UI smoke tests, or app behavior verified in Chrome. Drives Chrome through the chrome-agent CLI (CDP).
---

# Browser test automation with chrome-agent

`chrome-agent` is a CLI that drives a real Chrome through the Chrome DevTools Protocol. Two channels. A one-shot command acts: `chrome-agent <instance> Domain.method '{"json"}'` sends one command, prints the JSON result, disconnects. `attach` observes: it holds a connection and streams the events you subscribe to as JSON lines. Install with `uv tool install chrome-agent`; system Chrome is required.

The running browser is the command reference. `chrome-agent help <instance> Network.responseReceived` prints the exact signature of any method or event, read live from the installed Chrome. Look signatures up there instead of guessing.

Command patterns for the steps below are in [references/cdp-cookbook.md](references/cdp-cookbook.md). Open it whenever you reach for a concrete command.

## 1. Set up

Check `chrome-agent --version` (install with `uv tool install chrome-agent` if missing). Confirm the app under test is serving its dev URL; discover the URL from the repo or ask. Then `chrome-agent launch --headless`, or headed when the user wants to watch. Launch prints `{"name", "port", "pid"}`; the instance name (auto-derived from the working directory) addresses every later call.

Done when `chrome-agent status` lists the instance with at least one tab.

## 2. Plan assertions before commands

List the flows to test and, for each step of a flow, write the assertion that proves the step worked: a DOM read, a page-state read, or a network event that must appear. Take expected values from the app's spec, seed data, or fixtures, never from what the page currently shows. If the user did not name the flows, propose the list and confirm before writing anything.

Done when every flow step has an observable assertion, and none reads a screenshot or trusts a command's return value.

## 3. Write the script

The deliverable is a deterministic script kept in the repo (ask where; propose `scripts/e2e_<name>.sh` or `.py` if nothing exists). The exit code is the pass/fail signal, so CI can run it unchanged. Four rules every script follows:

1. Sense, act, sense again. Never trust an action's return value. After each action, read back the state it should have changed and assert on that read. A click that "succeeded" can still have done nothing.
2. Wait on events, never on sleep. Wait for `document.readyState === "complete"`, for a target element to exist, or for a page or network event. Fixed sleeps either waste time or wake early and read half-loaded pages. Background one `attach` stream for the whole run and block per event with [scripts/cdp-wait.py](scripts/cdp-wait.py); when a deliverable script uses it, copy the waiter next to the script so the script runs without the skill present.
3. Watch for failure in every observation. Subscribe to `+Runtime.exceptionThrown +Network.loadingFailed` on every `attach`. A happy-path-only stream stays silent through a crash, and silence reads as success.
4. Fail loudly. On a failed assertion, print expected and observed, save a screenshot for the report, and exit non-zero immediately.

Done when the script encodes every planned assertion and each one reads observed state.

## 4. Run and iterate

Run the script against the running instance. On failure, decide whether the script asserted wrong or the app misbehaved, fix that one thing, and rerun. In the report, say which it was, and attach the failure screenshot plus the observed value.

Done when the script passes end to end, or every failure is reported as app behavior with evidence.

## 5. Tear down

`chrome-agent stop <instance>`, then `chrome-agent status`. The status read is the verification; the stop command's own output is not.

Done when `chrome-agent status` shows no instance you launched.
