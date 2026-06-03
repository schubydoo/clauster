# Clauster — Browser E2E Checklist

Manual browser-verification checklist. The pytest suite (`tests/test_*.py`)
covers logic at the route/unit level via Starlette's `TestClient`; **it does not
drive a real browser.** This checklist is the standing list of flows to click
through in an actual browser (Chromium) before a release or after touching the
dashboard JS/CSS — until the automated browser suite exists (see
`scratch/TODO.md` → "Automated browser E2E").

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
      `CLAUDE.md` / trust badges correct.
- [ ] **Start / Stop / Restart** a bridge; status transitions
      Starting → Running → Stopped; optimistic pending states + disabled buttons.
- [ ] **Trust directory** flips the badge in place (no full reload).
- [ ] **Spawn controls** — spawn-mode + permission-mode pickers render and pass
      through.
- [ ] **Open in Claude** deep link + **QR code** render and scan.
- [ ] **External sessions** (started outside Clauster) appear with their distinct
      indicator.
- [ ] **Live log tail** streams over WS; ANSI stripped; IDs/tokens redacted.
- [ ] **CLAUDE.md editor** — load, edit, save; stale-bridge banner; 409 conflict
      surfaces.
- [ ] **Create project** (empty) and **clone** a git URL — the new card appears
      **in place with no full-page reload** (empty grid → first card, or appended
      to existing). Clone shows the **live progress bar**; repos shipping
      `CLAUDE.md`/`.claude` show the "code runs on start" warning and the inserted
      card stays untrusted; errors render inline (not silent). The inserted card
      is fully interactive (Start/Trust/spawn selects) without a refresh.
- [ ] **Per-project cost badge** lazy-loads `≈$X.XX` after first paint; blank
      project shows no badge.
- [ ] **Login / logout** (when `auth.password_required`): login, then logout
      revokes everywhere (old cookie rejected).
- [ ] **Theme toggle** (dark/light) persists across reload — sun/moon Iconoir
      icons render (no broken `<use>` refs).
- [ ] **Action-button icons** (Iconoir) render on Start/Stop/Restart/Trust/Edit/
      logs/QR/copy/Open and follow the button text color in both themes.
- [ ] **Connection-lost banner** — stop the server (or block `/api/instances`);
      after ~2 failed polls a "Lost connection … retrying" banner appears; it
      **clears** when the server returns. A 401 mid-session bounces to `/login`.
- [ ] **Action errors surface** — a failed start/stop/restart/trust shows an
      inline error on the card (not just a toast that vanishes); a failed copy
      toasts rather than failing silently.

## Gated / opt-in flows — MUST set the flag first

These are off by default. Set the flag, restart, hard-refresh, then verify.

- [ ] **Ghost-environment reaper UI** — set `reaper.ui_enabled: true`.
      - Panel "👻 Reap ghost environments" appears above the grid.
      - Ghost list renders (id / directory / name); summary line counts ghosts,
        live dirs kept, and notes cloud `Default` is never touched.
      - **Archive (reversible)** button styled as a warning action.
      - **Permanently delete** stays **disabled** until `DELETE` is typed exactly.
      - With the flag unset, the panel is absent *and* `GET
        /api/environments/ghosts` 404s.
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
- [ ] **Non-loopback + auth** — bind a non-loopback host with
      `auth.password_required` (or reverse-proxy / `allow_unauthenticated_network`).
      - Login is enforced; the correct `allowed_origins` must be set or the login
        POST 403s on the origin check.
- [ ] **On-disk URL redaction** — set `logs.redact_session_url: true`; confirm the
      session URL is redacted in the on-disk bridge log too (not just over WS).
- [ ] **PTY true-resume mode** — set `claude.resume_mode: pty` (POSIX only). Start a
      bridge: it spawns the `claude --remote-control` flag form under a PTY keeper and
      reaches RUNNING with a `claude.ai/code/session_…` link.
      - A **`↻ true-resume`** badge shows on the card (purple); hovering it explains
        "Restart restores prior conversation context (single session)." It is absent for
        `standard` bridges.
      - Drive a conversation (give the agent a codeword), then **Restart**: the new
        session **restores the prior conversation** (the agent recalls the codeword with
        no tools) — true resume, not just the recap.
      - It is **single-session** (no multi-chat capacity) — the card reflects that.
      - **Stop** cleanly ends both the bridge and its keeper (no stray processes).
      - The keeper is reparented to init, so the bridge survives a Clauster restart, and
        on restart the card **rediscovers the pty bridge** (still RUNNING, badge present)
        and **Stop still reaps the keeper** (keeper_pid recovered from the sidecar).
      - With the flag unset (default `standard`), bridges use the subcommand server and
        Restart produces a fresh, empty-context session.

## When adding a new gated/config feature

Add a row here **in the same PR** that introduces the flag, naming the flag and
what to verify both with it on and off. A gated feature without a checklist row
is the exact thing this file exists to prevent.
