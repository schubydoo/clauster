// E2E init script: auto-accept native browser dialogs.
//
// The dashboard guards destructive actions with window.confirm (the desktop-bridge
// Stop since #577; plus forget / stopHosted / killHosted). agent-browser (v0.31) has
// no dialog API, so a blocking confirm makes the triggering click fail. No e2e test
// relies on a confirm being CANCELLED, so accept every dialog — destructive-action
// tests then drive the action as they did before the guard existed. Registered before
// navigation via AGENT_BROWSER_INIT_SCRIPTS (see _driver.py), so it is in place before
// any app/Alpine code runs and overrides window.confirm for the lifetime of the page.
//
// NOTE(agent-browser-dialog-api, re-evaluated 2026-07-16 at 0.29.x): agent-browser now
// ships `agent-browser dialog accept|dismiss|status` (alert/beforeunload are even
// auto-accepted natively). Evaluated and REJECTED as a wholesale replacement: the
// native command is an async per-occurrence follow-up, so every confirm-guarded click
// site in the suite would need a paired dialog call — a bigger, flakier refactor than
// this one synchronous override with no test-value gain. Revisit only if a test ever
// needs to assert that a confirm APPEARS (this override shadows that signal).
window.confirm = () => true;
window.alert = () => {};
window.prompt = (_message, fallback) => (fallback == null ? "" : fallback);
