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
// TODO(agent-browser-dialog-api): if a future agent-browser adds native dialog handling
// (e.g. `--dialog-accept`), prefer it and drop this override — otherwise this would
// silently shadow it and a test asserting a confirm *appears* would pass vacuously.
window.confirm = () => true;
window.alert = () => {};
window.prompt = (_message, fallback) => (fallback == null ? "" : fallback);
