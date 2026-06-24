# Clauster — Browser E2E Checklist

Manual browser-verification checklist. The pytest suite (`tests/test_*.py`)
covers logic at the route/unit level via Starlette's `TestClient`; **it does not
drive a real browser.** This checklist is the standing list of flows to click
through in an actual browser (Chromium) before a release or after touching the
dashboard JS/CSS.

**Automated suite (first slice landed).** `tests/e2e/` now drives real headless
Chromium (via the `agent-browser` CLI) against a live clauster. It is opt-in — run
it with `scripts/e2e.sh` (excluded from the default/CI run, so the required `tests`
gate is unchanged). So far it covers the **non-bridge** flows, marked **`[auto]`**
below; the rest are still manual. The bridge-spawn slice
(`tests/e2e/test_bridge_e2e.py`) drives a real bridge subprocess (the fake
`claude` fixture) through the dashboard — trust-on-start, start → running →
stop, and the spawn-option pass-through — in *standard* mode. Porting more flows
(notably the gated pty true-resume) is tracked in `scratch/TODO.md` →
"Automated browser E2E".

> **Why this file exists:** several features are **gated behind config flags and
> off by default**, so they don't render unless explicitly enabled. They are easy
> to forget and easy to break invisibly. Each gated feature below names the flag
> to set. Do not assume a gated feature works because its routes are unit-tested
> — render it in a browser at least once per release.

## How to run a throwaway preview

Run a loopback instance on a non-default port (never touch a live deploy):

```sh
# minimal clauster.yml: host 127.0.0.1 (loopback needs no auth), a spare port,
# a temp projects_root, plus whichever gates you're testing (see each item).
uv run clauster run -c /tmp/preview/clauster.yml
```

Open the chosen port in a browser. Hard-refresh (Ctrl/Cmd+Shift+R) after
restarts — the dashboard JS/CSS are cached static assets.

---

## Always-on flows

- [ ] **Project grid** renders one card per dir under `projects_root`; git /
      `CLAUDE.md` / trust badges correct. **`[auto]`** (card-per-project render).
- [ ] **Start / Stop / Resume** a bridge; status transitions
      Starting → Running → Stopped; optimistic pending states + disabled buttons.
      **`[auto]`** for standard-mode start → running → stop (a stopped standard
      bridge then offers **Resume**); the pty Resume *content* check stays manual.
- [ ] **Trust-on-Start** — an untrusted dir has NO standalone "Trust directory" button.
      Clicking **Start bridge** pops a "Trust the files in `<name>`?" prompt with an
      "I trust the files in this directory" checkbox: **Trust & start** stays disabled
      until it's ticked, and the **Start** button is greyed while the prompt is open.
      Confirming trusts the dir (a green shield appears next to the name) **in place**
      (no reload) then spawns; a trusted dir starts with no prompt. **Cancel** backs out.
      **`[auto]`** (checkbox-gated Trust & start → green shield → running).
- [ ] **Spawn controls** — spawn-mode + permission-mode + **resume-mode (Mode)**
      pickers render and pass through. **`[auto]`** for the pickers rendering and the
      chosen spawn + permission values reaching the bridge argv. The Mode picker
      (standard / pty) defaults to `claude.resume_mode`; choosing **pty** with a
      `standard` config default starts a pty bridge (the `↻ true-resume` badge appears),
      and choosing **standard** with a `pty` default starts a subcommand bridge. The
      Mode picker is hidden on Windows (pty is POSIX-only).
- [ ] **Reboot recovery** — with a bridge running, restart the host (or stop
      Clauster, kill the bridge process, restart Clauster). The bridge reappears as a
      **Stopped** card built from `state.json`, not lost: a **pty** bridge offers
      **Resume** (`--continue` restores the conversation), a **standard** bridge offers
      a fresh **Start**. A project with no persisted record shows no phantom card.
- [ ] **Open in Claude** deep link + **QR code** render and scan. **`[auto]`** for
      the running-bridge session-link row: the **Open in Claude** link, the **copy**
      button (toasts "Link copied"), and the **QR** show/hide toggle (image appears
      then clears); the phone-scan check stays manual.
- [ ] **External sessions** (started outside Clauster) appear with their distinct
      indicator. **`[auto]`** (`test_observability_e2e`): a fake `agents --json` session
      in a managed dir that Clauster didn't start is attributed EXTERNAL and shows the
      "External session active" indicator (a project with none shows nothing).
- [ ] **External-session adoption** (#330): a **standard** external session shows a
      **Manage** button on its project-row external note; clicking it (confirm dialog
      warns "Resume starts fresh") promotes it to a managed RUNNING row with Stop/observe,
      and the external note clears. A **pty** external session shows the note but **no**
      Manage button (adoption is standard-only).
- [ ] **Live log tail** streams over WS; ANSI stripped; IDs/tokens redacted.
      **`[auto]`** (`test_actions_e2e`): a running bridge's Logs panel populates with
      the streamed marker lines, ANSI is stripped, and the session id / bearer token on
      the bridge's deep-link line are redacted (`sanitize_line`).
- [ ] **CLAUDE.md editor** — load, edit, save; stale-bridge banner; 409 conflict
      surfaces. **`[auto]`** for open-via-··· (the overflow menu) → load → edit →
      save (`✓ saved`, persisted on reopen) plus the running-bridge banner
      (`test_dashboard_e2e`), and the **··· menu open/close** (Alpine-driven,
      `@click.outside` closes it) + its **Edit CLAUDE.md** item opening the editor
      (`test_controls_e2e`); the 409-conflict check stays manual.
- [ ] **Create project** (empty) and **clone** a git URL — the new card appears
      **in place with no full-page reload** (empty grid → first card, or appended
      to existing). Clone shows the **live progress bar**; repos shipping
      `CLAUDE.md`/`.claude` show the "code runs on start" warning and the inserted
      card stays untrusted; errors render inline (not silent). The inserted card
      is fully interactive (Start/Trust/spawn selects) without a refresh.
      Clicking the warning's **"Show it"** CTA reveals and focuses the new card
      (scrolls into view + lands focus) instead of forcing a full-page reload.
      **`[auto]`** for the create-empty path: the form's name-gated submit + the
      clone-mode Git-URL toggle, the inserted card appearing in place with **no
      full-page reload**, and that card being Start/Trust-interactive without a
      refresh. The clone download, progress bar, and "Show it" CTA stay manual.
- [ ] **Per-project cost badge** lazy-loads `≈$X.XX` after first paint; blank
      project shows no badge. **`[auto]`** (`test_observability_e2e`): a project with a
      seeded usage transcript shows the `≈`-prefixed badge; a blank project shows none.
- [ ] **Login / logout** (when `auth.password_required`): login, then logout
      revokes everywhere (old cookie rejected). **`[auto]`** for login (wrong
      password gated, correct reaches the dashboard); logout-revocation still manual.
- [ ] **Theme toggle** (dark/light) persists across reload — sun/moon Tabler
      icons render (no broken `<use>` refs). **`[auto]`** (persistence across reload).
- [ ] **Action-button icons** (Tabler) render on Start/Stop/Resume/Trust/Edit/
      logs/QR/copy/Open and follow the button text color in both themes.
      **`[auto]`** (`test_a11y_e2e`) for a representative always-present set: the
      per-project play + ··· overflow icons and the theme toggle's active sun/moon icon
      each render with a non-zero box (a broken `<use>` sprite ref renders zero-size).
      The color-follows-text check stays manual.
- [ ] **Accessibility (a11y)** — no serious/critical WCAG 2 A/AA violations on the
      dashboard or login page (interactive controls have accessible names, inputs have
      labels, images have alt). **`[auto]`** (`test_a11y_e2e`): vendored, network-free
      axe-core (`tests/e2e/vendor/axe.min.js`, registered as a page init script) runs
      `axe.run` in-page on both and asserts zero serious/critical violations.
- [ ] **Connection-lost banner** — stop the server (or block `/api/instances`);
      after ~2 failed polls a "Lost connection … retrying" banner appears; it
      **clears** when the server returns. A 401 mid-session bounces to `/login`.
      **`[auto]`** (`test_observability_e2e`): killing the server subprocess mid-session
      makes the banner appear. The **clears-on-return** recovery and the **401→/login**
      bounce stay manual.
- [ ] **Action errors surface** — a failed start/stop/restart/trust shows an
      inline error on the card (not just a toast that vanishes); a failed copy
      toasts rather than failing silently.
      **`[auto]`** (`test_actions_e2e`) for the trust-on-start failure: an obstructed
      trust write surfaces in the persistent inline `errorOf` `.alert-danger` block (and
      it outlives the ~4.5s toast). The failed-copy toast stays manual. Note: a
      fake-`claude` *spawn crash* is surfaced as an error-**status** instance, not via
      `errorOf` (the spawn POST still returns 201); only the non-OK action responses
      (trust/stop/resume) feed the inline block.

## Gated / opt-in flows — MUST set the flag first

These are off by default. Set the flag, restart, hard-refresh, then verify.

- [ ] **Ghost-environment reaper UI** — set `reaper.ui_enabled: true`.
      - Panel "Clean up leftover environments" appears above the grid.
      - Ghost list renders (id / directory / name); summary line counts ghosts,
        live dirs kept, and notes cloud `Default` is never touched.
      - **Archive (reversible)** button styled as a warning action.
      - **Permanently delete** stays **disabled** until `DELETE` is typed exactly.
      - With the flag unset, the panel is absent *and* `GET
        /api/environments/ghosts` 404s.
      - **`[auto]`** for the **gating** only: the panel renders with the flag, and is
        absent + the endpoint 404s with it unset (`test_gated_e2e.py`). The ghost-list
        / archive / typed-DELETE flow needs cloud-env data and stays manual.
- [ ] **Conversation recap on restart** — set `claude.resume_recap: true`.
      - On bridge **Restart**, the new session receives a recap of the prior
        transcript (verify in the attached Claude session that prior context is
        present). Confirm turns entered via **Desktop/mobile** (not just the
        terminal) are included.
      - With the flag unset, restart produces a fresh, empty-context session.
- [ ] **bypassPermissions footgun** — set
      `projects.<name>.allow_bypass_permissions: true`.
      - The `bypassPermissions` option only renders for that project.
      - Selecting it requires typing the project name to confirm before spawn.
      - For a project *without* the ceiling, the option is absent (cannot be
        forced from the client).
      - **`[auto]`** (`test_gated_e2e.py`): the option renders only for the opted-in
        project, is absent otherwise, and the typed-name confirm blocks the spawn (a
        wrong name is rejected inline).
- [ ] **Non-loopback + auth** — bind a non-loopback host with
      `auth.password_required` (or reverse-proxy / `allow_unauthenticated_network`).
      - Login is enforced; the correct `allowed_origins` must be set or the login
        POST 403s on the origin check.
- [ ] **On-disk bridge-log redaction** — set `logs.redact_session_url: true`. The bridge
      writes a private `0600` raw debug log; the public on-disk bridge log becomes a
      redacted mirror (session/env ids masked) while readiness + the deep link still work
      (parsed from the raw copy) and the WS stream is unchanged. **`[auto]`** (runner
      integration test). **Scope:** the bridge debug log only — the pty keeper sidecar +
      `state.json` still record ids as perms-protected operational state (follow-up #8c).
- [ ] **PTY true-resume mode** `[auto]` — set `claude.resume_mode: pty` (POSIX only). Start a
      bridge: it spawns the `claude --remote-control` flag form under a PTY keeper and
      reaches RUNNING with a `claude.ai/code/session_…` link.
      - A **`↻ true-resume`** badge shows on the card (purple); hovering it explains
        "Resume restores prior conversation context (single session)." It is absent for
        `standard` bridges. `[auto]`
      - `[auto]` Drive a conversation (give the agent a codeword), then **Stop**. The card must
        then show a **Resume** button — a stopped pty bridge is *resumable* even though it
        has **no `environment_id`** (regression guard: `isResumable` accepts
        `resume_mode === "pty"`). Click **Resume**: the respawn argv carries `--continue`
        (asserted by the E2E). The *content* check — the new session **restores the prior
        conversation** (the agent recalls the codeword with no tools), true resume not just
        the recap, continuing the prior transcript rather than a fresh `.jsonl` — stays
        **manual** (the fake bridge has no conversation to restore).
      - Beside **Resume** the card also shows **Start new session**. Clicking it raises a
        warning (a new session won't restore the prior one and Resume may no longer reach
        it); confirming launches a **fresh** bridge (no `--continue`, the codeword is NOT
        recalled). Cancel leaves the stopped bridge resumable. The fresh session **keeps the
        bridge's recorded mode** (a stopped pty bridge starts a new *pty* session, not a
        silent drop to the `standard` config default — its Mode picker is hidden, so `_spawn`
        posts the instance's own `resume_mode`), even across a page reload.
      - It is **single-session** (no multi-chat capacity) — the card reflects that.
      - **Stop** cleanly ends both the bridge and its keeper (no stray processes).
      - The keeper is reparented to init, so the bridge survives a Clauster restart, and
        on restart the card **rediscovers the pty bridge** (still RUNNING, badge present)
        and **Stop still reaps the keeper** (keeper_pid recovered from the sidecar).
      - **Mode is fixed per bridge:** after starting a bridge, edit `claude.resume_mode`
        in `clauster.yml` to the *other* value and restart Clauster. The rediscovered
        bridge keeps the mode it launched with — Stop and Resume agree (a `standard`
        bridge is not silently resumed as pty, nor vice-versa).
      - With the flag unset (default `standard`), bridges use the subcommand server and
        Resume produces a fresh, empty-context session (no conversation resume).

## When adding a new gated/config feature

Add a row here **in the same PR** that introduces the flag, naming the flag and
what to verify both with it on and off. A gated feature without a checklist row
is the exact thing this file exists to prevent.
