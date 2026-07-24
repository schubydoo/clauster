"""Configuration loading for Clauster.

Search order:  $CLAUSTER_CONFIG  ->  ./clauster.yml  ->  $CLAUSTER_HOME/clauster.yml
Any scalar key is overridable via env: CLAUSTER_<UPPER_SNAKE_CASE_PATH>=value.
Every such var also has a CLAUSTER_<...>_FILE form that reads the value from a file
(file wins; trailing whitespace stripped) — for secrets rendered to /run/secrets by
Docker/K8s/Vault, keeping them out of the process environment.
Schema is additive-only: old configs must always validate against newer versions.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import types
from pathlib import Path
from typing import Literal, Union, get_args, get_origin

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from .reconcile import resume_mode_to_launch_mode, show_cost_to_mode

_log = logging.getLogger("clauster.config")

SCHEMA_VERSION = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# The spawn/permission modes `claude remote-control` accepts (this list last
# verified against `claude remote-control --help` at claude 2.1.156 — a check
# version, distinct from the `min_version` support floor below). worktree requires
# a git repo; bypassPermissions is footgun-gated (see `ProjectConfig.allow_bypass_permissions`).
SpawnMode = Literal["same-dir", "worktree", "session"]
PermissionMode = Literal["default", "plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions"]
# How a bridge is launched. "standard" = Server Mode: the headless `claude remote-control`
# subcommand server (multi-session, survives a restart, no conversation resume).
# "pty" = Interactive Session: the `claude --remote-control` flag form under a PTY keeper,
# which is single-session but genuinely restores prior context on restart.
ResumeMode = Literal["standard", "pty"]
# Orthogonal to ResumeMode: which substrate hosts a managed session. "remote-control"
# is the bridge (standard/pty modes above); "hosted" is the Direct Session channel — the
# claustrum-daemon headless stream-json channel (CL-4). ResumeMode applies only to the
# remote-control channel.
SessionChannel = Literal["remote-control", "hosted"]
# Display-name <-> wire-token map (UI/docs say the left, code/config keep the right):
#   "Server Mode"         == ResumeMode "standard"  (claude remote-control, multi-session)
#   "Interactive Session" == ResumeMode "pty"       (claude --remote-control, true resume)
#   "Direct Session"      == SessionChannel "hosted" (claustrum stream-json channel)
#   "Background Agent"    == claude --bg agent-view run (no ResumeMode/SessionChannel token)
# Never rename the wire tokens above — only the display names are user-facing.
SPAWN_MODES: tuple[str, ...] = ("same-dir", "worktree", "session")
RESUME_MODES: tuple[str, ...] = ("standard", "pty")
# Per-launch sandbox toggle for the STANDARD (server-mode) bridge (#780). Tri-state:
# "default" appends NEITHER flag (claude's own setting wins — zero behavior change),
# "on" appends `--sandbox`, "off" appends `--no-sandbox`. These are undocumented/hidden
# flags on `claude remote-control` (absent from --help) but empirically accepted on
# claude 2.1.198 — a genuinely-unknown flag errors "Unknown argument", whereas
# `--sandbox`/`--no-sandbox` parse and connect. Version-coupled: revisit if a future
# claude removes them.
SandboxMode = Literal["default", "on", "off"]
SANDBOX_MODES: tuple[str, ...] = ("default", "on", "off")
# The per-launch sandbox toggle (#780) is DISABLED for the 1.0 release (#1037). Evidence from
# the pre-RC dogfood: `--sandbox` reaches the remote-control bridge but is NOT passed to the
# server-mode session worker that actually runs Bash, so the security-labeled control silently
# did nothing (fully unsandboxed, no warning) — a "fail closed visibly" violation. Rather than
# ship a toggle that lies, clauster stops emitting the flag and coerces every requested/persisted
# value to "default". The plumbing (this enum, the API/CLI/MCP params, the runner threading) is
# kept intact so re-enabling behind dependency preflight + platform gating in #1046 is a one-line
# flip of this flag. Left as a plain bool (not Final/Literal) so it stays runtime-togglable.
SANDBOX_TOGGLE_ENABLED = False
PERMISSION_MODES: tuple[str, ...] = (
    "default",
    "plan",
    "acceptEdits",
    "auto",
    "dontAsk",
    "bypassPermissions",
)

# Canonical permission-mode labels — the SINGLE source of truth for every label
# surface (#685). Each mode carries three forms:
#   short  — compact chips/badges (Run button, hosted row): "Plan only"
#   long   — pickers/dropdowns (launch <select>, config editor): "Plan only (read-only)"
#   effect — the plain-language inline hint under the picker: a full sentence.
# Server-injected into the template once; the launch <select>, the JS permLabel()/
# permissionEffect() helpers, and the config editor's choice labels all read from
# THIS map — never a hand-maintained copy. The bypassPermissions "(Desktop only)"
# caveat lives OUT of the label as a separate contextual hint (BYPASS_DESKTOP_HINT)
# so it isn't baked into the label string.
PERMISSION_LABELS: dict[str, dict[str, str]] = {
    "default": {
        "short": "Ask each time",
        "long": "Ask each time (default)",
        "effect": "Asks before each tool use — the safe default.",
    },
    "plan": {
        "short": "Plan only",
        "long": "Plan only (read-only)",
        "effect": "Read-only planning; makes no changes until you switch modes.",
    },
    "acceptEdits": {
        "short": "Auto-accept edits",
        "long": "Auto-accept edits",
        "effect": "Auto-accepts file edits; still asks for other tools.",
    },
    "auto": {
        "short": "Auto-approve safe",
        "long": "Auto-approve safe",
        "effect": "The model auto-approves what it judges safe; asks otherwise.",
    },
    "dontAsk": {
        "short": "Never prompt",
        "long": "Never prompt — deny unknowns",
        "effect": "Never prompts — denies anything not pre-approved.",
    },
    "bypassPermissions": {
        "short": "Skip all checks ⚠",
        "long": "Skip all checks ⚠",
        "effect": "Skips every permission check — the agent may do anything.",
    },
}

# Contextual hint for bypassPermissions in the launch picker — kept OUT of the
# label (#685) because it is true only of the Desktop/bridge launch, not of the
# mode itself. The picker appends it to the long label only on that option.
BYPASS_DESKTOP_HINT = "(Desktop only)"


class ClaudeConfig(BaseModel):
    """Settings for the `claude` binary and bridge-spawn behavior."""

    binary: str = Field(
        default="claude",
        description="The `claude` binary name or path (resolved to an absolute path "
        "before spawning).",
    )
    min_version: str = Field(default="2.1.145", description="Minimum acceptable `claude` version.")
    agents_json_poll_interval_seconds: int = Field(
        default=30,
        ge=1,
        description="How often (≥1) the inspector cross-checks `claude agents --json` "
        "for liveness; lower = snappier live indicators + crash detection, at the cost of "
        "more subprocess spawns.",
    )
    startup_grace_seconds: float = Field(
        default=60.0,
        gt=0,
        description="How long (>0) a freshly-spawned bridge may stay alive without "
        'registering an environment before it is marked `ERROR`. Liveness alone is not "running".',
    )
    auto_enable_remote_control: bool = Field(
        default=True,
        description="Before the first spawn, mark remote control acknowledged "
        "(`hasUsedRemoteControl` / `remoteDialogSeen`) in `~/.claude.json` so a "
        "detached-stdin bridge isn't stuck on the one-time \"Enable Remote Control? "
        '(y/n)" prompt. Set `false` to manage it yourself.',
    )
    resume_recap: bool = Field(
        default=False,
        description="Install a `SessionStart` hook in the runtime user's "
        "`~/.claude/settings.json` that recaps the most recent prior transcript for the "
        "cwd into a restarted (standard-mode) bridge. Opt-in: edits the user's Claude "
        "settings and injects prior turns.",
    )
    resume_recap_max_chars: int = Field(
        default=8000,
        ge=500,
        description="Character budget (≥500) for the recap injection (most recent turns kept).",
    )
    launch_mode: ResumeMode = Field(
        default="standard",
        description="Launch mode for **new** bridges. `pty` = native true-resume under a "
        "PTY keeper (a POSIX pty, or a ConPTY keeper on Windows with the `pty` extra "
        "installed; falls back to standard when that extra is absent). A bridge keeps the "
        "mode it launched with — editing this never re-modes a running or stopped bridge. "
        "(Renamed from `resume_mode`, still accepted as a deprecated alias.)",
    )
    pty_screen_enabled: bool = Field(
        default=False,
        description="(pty mode) Publish a redacted, read-only render of the bridge's live "
        "terminal screen for the dashboard's live-terminal view (#534). Off by default; "
        "needs the optional `pyte` dependency (`pip install 'clauster[pty]'`) — without it the "
        "feature stays dormant. The standalone binary does not bundle `pyte` (LGPL): install "
        "it there with `clauster deps install pty` (#904), or manually side-load `pyte` by "
        "setting `CLAUSTER_PYTE_PATH` to a directory holding an installed "
        "`pyte` (#702) — the binary appends it to `sys.path` only when set. The "
        "render is best-effort secret-redacted, so treat the live view as auth-gated, not "
        "secret-proof.",
    )
    path_append: list[str] = Field(
        default_factory=list,
        description="Directories appended to the bridge subprocess `PATH` so a `claude` "
        "session can resolve user-local tools (e.g. `~/.local/bin`) that a minimal service "
        "`PATH` omits. `~` is expanded; entries are appended in order after the inherited "
        "`PATH`, never replacing it. Applies to both standard and pty bridges.",
    )
    node_from_nvm: bool = Field(
        default=True,
        description="Resolve nvm's `default` node version at each bridge spawn and append "
        "its bin dir to the bridge subprocess `PATH` (after `path_append`). Puts `node`/"
        "`npx`/`npm` AND any nvm-global CLI (e.g. `agent-browser`) — all of which live in "
        "that one bin dir — on the raw process `PATH`, so they resolve in EVERY spawn "
        "context, not just `bash -c`: dash/`sh -c`, direct `execvp` (how Claude Code "
        "spawns MCP stdio servers), and subagents all inherit it, unlike a `BASH_ENV` "
        "nvm-init which only non-interactive bash sources. Fixes `npx`/`node`-based MCP "
        "servers (e.g. codecov, context7) showing `✘ Failed to connect` under a systemd "
        "deployment. On by default and fail-safe: a no-op (never raises) when nvm, its "
        "`default` alias, or POSIX `bash` aren't available — spawn is never blocked by "
        "this, and the resolved dir is appended last so it never shadows a `path_append` "
        "entry. POSIX-only (nvm is a bash function); ignored on Windows. Set to `false` "
        "to opt out (e.g. you pin node another way).",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables overlaid on the bridge subprocess. Applied "
        "AFTER Clauster's secret scrub, so a key matching a Clauster secret name "
        "(`CLAUSTER_*` with SECRET/PASSWORD/TOKEN/HASH) is dropped and can never "
        "re-introduce a scrubbed credential. Applies to both standard and pty bridges.",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_resume_mode(cls, data: object) -> object:
        """Accept the legacy `resume_mode` key as an alias for the renamed `launch_mode`.

        `claude.resume_mode` was renamed to `claude.launch_mode` (the old name read like a
        resume on/off toggle, #540). Existing `clauster.yml` files keep working: the legacy
        key maps to `launch_mode` with a deprecation warning. If both are set, `launch_mode`
        wins and the legacy key is ignored (also warned) rather than silently picking one.
        """
        if isinstance(data, dict) and "resume_mode" in data:
            data = dict(data)
            legacy = data.pop("resume_mode")
            if "launch_mode" in data:
                _log.warning(
                    "config: both `claude.launch_mode` and the deprecated `claude.resume_mode` "
                    "are set — using `launch_mode`, ignoring `resume_mode`. Remove `resume_mode`."
                )
            else:
                data["launch_mode"] = resume_mode_to_launch_mode(legacy)
                _log.warning(
                    "config: `claude.resume_mode` was renamed to `claude.launch_mode`; the old "
                    "key still works but is deprecated. Please rename it in your clauster.yml."
                )
        return data


class InstanceDefaults(BaseModel):
    """Per-bridge defaults: spawn/permission mode, session-name prefix, and capacity limits."""

    spawn_mode: SpawnMode = Field(
        default="same-dir",
        description="Default spawn mode for new **bridge** sessions (the standard / pty launch "
        "flow) — where the session's working directory lives. `worktree` requires a git repo; "
        "`session` runs in a fresh sandbox. Bridge launches only — hosted (browser) sessions "
        "ignore this.",
    )
    permission_mode: PermissionMode = Field(
        default="default", description="Default permission mode for new bridges."
    )
    verbose: bool = Field(
        default=False,
        description="Pass `--verbose` to spawned standard `claude remote-control` bridges "
        "for detailed connection/session logs — every spawn mode (same-dir/worktree/session). "
        "The pty (flag-form) bridge is never passed --verbose. Off by default.",
    )
    session_name_prefix: str | None = Field(
        default=None,
        description="Optional prefix for auto-generated Remote Control session names (maps to "
        "`claude remote-control --remote-control-session-name-prefix`); applies to the standard "
        "multi-session bridge only. Unset → claude's default (the hostname).",
    )
    capacity: int = Field(
        default=32,
        ge=1,
        description="Max concurrent sessions a single standard bridge runs in `same-dir`/"
        "`worktree` spawn mode (≥1); passed to `claude remote-control --capacity`. Ignored for "
        "`session` spawn mode and the pty bridge (both single-session).",
    )
    max_bridges: int | None = Field(
        default=None,
        ge=1,
        description="Best-effort clauster cap on concurrent remote-control bridges "
        "(standard/pty; ≥1) — NOT hosted/bg-agent sessions. A bridge spawn over the cap is "
        "refused (409); cross-project concurrent spawns may transiently overshoot by a few. "
        "Unset → no limit. Distinct from `capacity` (per-bridge sessions).",
    )


class ProjectConfig(BaseModel):
    """Per-project config (the `projects:` map). Additive-only; unknown keys ignored.

    `allow_bypass_permissions` is the *hard ceiling* for the footgun gate: a project
    can never be spawned with `--permission-mode bypassPermissions` unless this is set
    here in clauster.yml. The dashboard's per-session typed-confirm is the second layer.
    """

    allow_bypass_permissions: bool = Field(
        default=False,
        description="The **hard ceiling** for the bypassPermissions footgun gate. A "
        "project can never be spawned with `--permission-mode bypassPermissions` unless "
        "this is set here in `clauster.yml`. The dashboard's per-session typed-confirm "
        "is the second layer.",
    )


class ReverseProxyConfig(BaseModel):
    """Trusted-reverse-proxy auth: user header, HMAC-signed secret, and IP allowlist."""

    enabled: bool = Field(default=False, description="Enable trusted-reverse-proxy auth.")
    user_header: str = Field(
        default="Remote-User", description="Header carrying the authenticated user."
    )
    shared_secret_header: str = Field(
        default="X-Proxy-Auth",  # noqa: S105 — HTTP header name, not a secret
        description="Header carrying the HMAC signature.",
    )
    trusted_ips: list[str] = Field(
        default_factory=list,
        description="Peer-IP allowlist for the proxy. Each entry is an IP or CIDR, validated "
        "at load (a malformed entry fails fast rather than silently never matching).",
    )
    shared_secret: str | None = Field(
        default=None, description="HMAC key the proxy signs `X-Proxy-Auth` with."
    )
    hmac_window_seconds: int = Field(
        default=60, ge=0, description="Clock-skew / replay window (≥0)."
    )
    require_hmac: bool = Field(
        default=True,
        description="When true (default, higher assurance), a request from a `trusted_ips` "
        "peer must also carry a valid HMAC in `shared_secret_header` to authenticate. Set "
        "false ONLY behind a forward-auth proxy (Authelia, authentik, Caddy `forward_auth`, "
        "Traefik, oauth2-proxy) that asserts `user_header` but signs no HMAC: clauster then "
        "trusts `user_header` from a trusted peer alone — so the proxy MUST strip that header "
        "from inbound client requests and be the sole route to clauster, since anyone able to "
        "reach a `trusted_ips` peer can forge the user.",
    )

    @field_validator("trusted_ips")
    @classmethod
    def _validate_trusted_ips(cls, v: list[str]) -> list[str]:
        # Fail fast on a malformed IP/CIDR rather than letting `auth.peer_trusted` silently
        # skip it at runtime — a quiet no-op entry in an auth allowlist is a footgun (the
        # proxy peer it was meant to admit silently never matches).
        for entry in v:
            ipaddress.ip_network(entry, strict=False)
        return v

    @model_validator(mode="after")
    def _require_secret_or_trusted_ips(self) -> ReverseProxyConfig:
        # Fail closed on an un-runnable proxy config rather than silently authenticating
        # nobody. `trusted_ips` is the FIRST gate for BOTH modes: every enforcement point
        # checks `peer_trusted(peer_ip, trusted_ips)` before anything else, and that is
        # always False with an empty list — so an empty allowlist makes proxy auth admit no
        # one regardless of mode. HMAC mode additionally needs a `shared_secret` to verify
        # the signature the proxy adds on top of the peer-IP gate.
        if not self.enabled:
            return self
        if not self.trusted_ips:
            raise ValueError(
                "reverse_proxy.enabled is true but trusted_ips is empty — the peer-IP "
                "allowlist is the first gate for both HMAC and header-only modes "
                "(peer_trusted() is always False with an empty list, so proxy auth would "
                "admit no one). Pin the proxy's IP/CIDR in trusted_ips."
            )
        if self.require_hmac and not self.shared_secret:
            raise ValueError(
                "reverse_proxy.require_hmac is true but no shared_secret is set — HMAC mode "
                "needs the key the proxy signs with. Set shared_secret, or set "
                "require_hmac: false for a forward-auth (header-only) proxy."
            )
        return self


class AuthConfig(BaseModel):
    """v0.2 auth foundation. Parsed (and ignored) since v0.1; enforced when enabled."""

    enabled: bool = Field(
        default=False,
        description="**Master auth switch.** Must be `true` for password / "
        "reverse-proxy auth to actually gate requests. The `false` default is safe "
        "only on loopback: a non-loopback bind **refuses to start** without enforced "
        "auth (fail-closed) unless `allow_unauthenticated_network` explicitly opts "
        "out.",
    )
    password_required: bool = Field(
        default=False, description="Require password login. Needs `password_hash`."
    )
    password_hash: str | None = Field(
        default=None, description="argon2id hash from `clauster hash-password`."
    )
    api_token_hash: str | None = Field(
        default=None,
        description="SHA-256 hash of an inbound API bearer token from "
        "`clauster hash-token`. Enables `Authorization: Bearer <token>` auth for "
        "headless/API clients. Only the hash is stored; the raw token is shown once.",
    )
    reverse_proxy: ReverseProxyConfig = Field(
        default_factory=ReverseProxyConfig,
        description="Trusted-reverse-proxy auth settings.",
    )
    allow_unauthenticated_network: bool = Field(
        default=False,
        description="Explicit opt-out: permit a non-loopback bind **without** enforced "
        "auth (e.g. a trusted LAN). `ops._check_auth` downgrades this to a warning. "
        "When `auth.enabled` is `false`, **anyone who can reach the port has full "
        "operator control of this host** — the dashboard drives a shell; treat it "
        "accordingly. A non-loopback bind auto-allows no `Origin`, so pair this with "
        "`allowed_origins` (the cross-site gate runs even with auth off) or the "
        "dashboard's own writes and live views are rejected.",
    )
    cookie_secure: Literal["auto", "always", "never"] = Field(
        default="auto",
        description="Session-cookie `Secure` flag. `auto` = Secure only over https (or "
        "a trusted proxy's `X-Forwarded-Proto=https`).",
    )
    session_max_age_seconds: int = Field(
        default=604800, ge=1, description="Session lifetime (≥1; default 7 days)."
    )
    allowed_origins: list[str] = Field(
        default_factory=list,
        description="Extra WebSocket / CSRF origins (e.g. the proxy domain). The `Origin` "
        "allowlist is enforced on unsafe methods and WebSocket handshakes **even when "
        "`enabled` is `false`** — it is a cross-site defence, not an authentication "
        "method. A loopback bind auto-allows only `127.0.0.1`/`localhost`/`[::1]` **at "
        "the configured port**, so list your real browser-facing origin here whenever it "
        "differs: a non-loopback bind, a reverse proxy or tunnel, or an SSH port-forward "
        "onto a different local port.",
    )

    @field_validator("api_token_hash", mode="before")
    @classmethod
    def _blank_token_hash_is_none(cls, v: object) -> object:
        # A blank / whitespace-only hash can never match a presented token (it fails
        # closed), but a truthy "  " would still satisfy the enforced-auth check in
        # _missing_enforced_auth and PERMIT a non-loopback bind that no token can ever
        # authenticate — a locked-out dashboard flying a false "enforced auth" flag.
        # Normalize it to None so only a REAL hash counts, mirroring the empty
        # password_hash being treated as unset.
        #
        # A non-empty value that is NOT a 64-char lowercase hex digest can never
        # match a token (``hash_token`` always returns SHA-256 hex) yet would still
        # satisfy the enforced-auth check and PERMIT a non-loopback bind — the same
        # false-"enforced auth" footgun. Reject it loudly so the operator fixes the
        # config instead of shipping a dashboard no token can ever unlock.
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, str) and not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError(
                "api_token_hash must be a 64-character lowercase hex string "
                "(the SHA-256 output from `clauster hash-token`)"
            )
        return v

    @field_validator("password_hash", mode="before")
    @classmethod
    def _blank_password_hash_is_none(cls, v: object) -> object:
        # A blank / whitespace-only password hash can never verify a password, but an
        # empty string is falsy-yet-not-None: it slips past the `password_hash is None`
        # unset checks and, paired with auth.verify_password's dummy-hash timing guard,
        # would make the source-visible dummy password a working login credential
        # (CWE-798). Normalize it to None so only a REAL hash counts — mirroring
        # _blank_token_hash_is_none, and closing the `password_hash: ""` yaml path and
        # the present-but-empty CLAUSTER_AUTH_PASSWORD_HASH env-var path at load time.
        # No hex-format check here: an argon2id hash is a structured `$argon2id$...`
        # string, not a fixed-width hex digest.
        if isinstance(v, str) and not v.strip():
            return None
        return v


class ApiConfig(BaseModel):
    """The versioned `/api/v1` public API surface (#302)."""

    openapi_enabled: bool = Field(
        default=False,
        description="Serve the OpenAPI docs (`/docs`) and schema (`/openapi.json`). "
        "**Off by default** — the documented HTTP surface isn't exposed until "
        "explicitly opted in. When `true`, both paths still require the same "
        "authentication as any `/api/...` route (a session cookie, reverse-proxy "
        "auth, or a Bearer token) whenever `auth.enabled` is set; an unauthenticated "
        "request gets a `401`, not a login redirect.",
    )


class UiConfig(BaseModel):
    """Web-dashboard kill switch (#806): serve the JSON API without the browser UI.

    ``enabled: false`` 404s the dashboard surface: the ``/`` page, ``/login`` +
    ``/logout``, ``/static/*``, and the internal HTML-fragment / per-session
    interactive routes (`/api/projects/{name}/row`, `/api/widget`, and the
    per-instance ``message`` / ``permissions/{request_id}`` / ``forget`` / ``qr``
    routes) that #302 already classified as "internal/unversioned only". Every
    other route — the rest of the bare ``/api/...`` JSON surface, ``/api/v1/...``,
    ``/healthz``, ``/metrics`` (per its own gate), and the WebSocket streams —
    keeps working exactly as before, behind whatever auth is configured.

    Deliberately **decoupled** from ``api.openapi_enabled`` and every ``auth.*``
    knob: this is an independent axis (web-UI+API, API-only, UI-only, docs
    on/off — any combination), never implied by another setting. Read once when
    the app is built, so a change needs a restart; **not** web-editable (see
    ``config_editor.EXCLUDED_FIELDS``) — a browser toggle that can kill the
    browser surface is a footgun with no way back in from the browser itself.

    With the UI off there is no login page, so session-cookie (and password)
    auth is unreachable — only a Bearer token or a trusted reverse proxy can
    authenticate. If ``auth.enabled`` is on with neither configured, Clauster
    logs a loud startup warning (never refuses to start; see ``app.py``).
    """

    enabled: bool = Field(
        default=True,
        description="Serve the web dashboard (`/`, `/login`, `/logout`, `/static/*`) and "
        "the internal HTML-fragment / per-session interactive routes (`/api/projects/"
        "{name}/row`, `/api/widget`, the per-instance `message`/`permissions`/`forget`/"
        "`qr` routes). Default `true` — zero behavior change. Set `false` to serve only "
        "the JSON API (the rest of `/api/...`, `/api/v1/...`, `/healthz`, `/metrics`, and "
        "the WebSocket streams): the dashboard surface then 404s. Independent of "
        "`api.openapi_enabled` and `auth.*` — turning the API on never implies turning "
        "the UI off, or vice versa. Restart-required.",
    )


def _missing_enforced_auth(host: str, auth: AuthConfig) -> bool:
    """Return True when binding ``host`` would NOT actually enforce authentication.

    The runtime guard gates on ``auth.enabled``, so a non-loopback bind only enforces
    auth when ``auth.enabled`` is set together with a method (``password_required``,
    ``reverse_proxy.enabled``, or an ``api_token_hash``). Loopback never needs auth. The
    explicit ``allow_unauthenticated_network`` opt-out is intentionally left to callers
    (the config validator permits it; ``ops._check_auth`` downgrades it to a warning) so
    both use one shared definition of "enforced auth" without conflating the opt-out.
    """
    if host in _LOOPBACK_HOSTS:
        return False
    method = auth.password_required or auth.reverse_proxy.enabled or bool(auth.api_token_hash)
    return not (auth.enabled and method)


class CloneConfig(BaseModel):
    """Project clone/create guards (spec §11 clone+trust chain).

    Clone URLs are user-supplied and hit the network from the host, so the
    defaults are strict.
    """

    enabled: bool = Field(default=True, description="Allow cloning/creating projects.")
    allowed_schemes: list[str] = Field(
        default_factory=lambda: ["https", "ssh"],
        description="Permitted clone URL schemes.",
    )
    allow_private_hosts: bool = Field(
        default=False,
        description="When false (default), block clone URLs whose host is a private/LAN/"
        "loopback IP (SSRF guard); when true, allow them — prefer `allowed_private_cidrs` for "
        "a targeted opt-in over opening every private range.",
    )
    allowed_private_cidrs: list[str] = Field(
        default_factory=list,
        description="Targeted LAN opt-in. Each entry is validated as a CIDR at load (a "
        "malformed entry fails fast rather than silently never matching).",
    )
    timeout_seconds: int = Field(default=300, ge=1, description="Clone timeout (≥1).")
    max_mb: int = Field(
        default=2048, ge=0, description="Post-clone size cap (≥0; `0` = unlimited)."
    )

    @field_validator("allowed_private_cidrs")
    @classmethod
    def _validate_cidrs(cls, v: list[str]) -> list[str]:
        # Fail fast on a malformed CIDR rather than letting it silently never match
        # (this is an SSRF allowlist — a quiet no-op entry is a footgun).
        for cidr in v:
            ipaddress.ip_network(cidr, strict=False)
        return v


class ReaperConfig(BaseModel):
    """Ghost-environment reaper (spec §11) dashboard gate.

    The CLI (`clauster reap-environments`) is always available; this gates only
    the *dashboard* surface, which exposes a destructive first-party API in the
    browser. Off by default — opt in explicitly.
    """

    ui_enabled: bool = Field(
        default=False,
        description="Expose the ghost-environment reaper in the **dashboard**. The CLI "
        "(`clauster reap-environments`) is always available; this gates only the "
        "destructive browser surface.",
    )


class ConfigWriteConfig(BaseModel):
    """Trust tier for code-executing config writes (#347/#687) — a fail-closed gate.

    Gates the (future) dashboard surface that writes Claude Code's *own* config
    (MCP servers, hooks, permission rules, skills). Every one of those is code the
    spawned ``claude`` will execute as the clauster runtime user, so a browser that
    can write them is transitively RCE — the gate assumes the browser is the threat.

    Both flags default **off** and are deliberately **not** in the config-editor
    Tier-A allowlist: the code-execution capability is file/CLI-managed only and can
    never be turned on from the browser (mirrors the auth/bind/secret exclusions).
    A missing/garbled flag means *off* (fail closed).
    """

    enabled: bool = Field(
        default=False,
        description="Master switch for the code-executing config-write capability "
        "(MCP servers / hooks / permission rules / skills). Off by default; the whole "
        "dashboard surface 404s when off. **Not** web-editable — file/CLI-managed only, "
        "exactly like the auth/bind/secret fields, because it is an RCE surface.",
    )
    allow_user_scope: bool = Field(
        default=False,
        description="A **second, independent** opt-in for user-scope writes "
        "(`~/.claude.json` / `~` settings), which affect every project and the live "
        "account — strictly more dangerous than a single project's `.mcp.json`. "
        "Project-scope can run with this off. Off by default; **not** web-editable.",
    )


class LoginShepherdConfig(BaseModel):
    """Dashboard-driven `claude` account login (#839) — a fail-closed gate.

    Gates the browser surface that drives a **live OAuth login** for the runtime
    `claude` account (`claude auth login` / `claude setup-token`) so an operator
    whose runtime account has logged out (or whose token expired) doesn't need SSH
    to fix it. This is security-sensitive: the flow writes the runtime user's own
    Claude Code credentials, so — mirroring `config_write.enabled` — it defaults
    **off** and is not offered anywhere unless explicitly turned on.
    """

    enabled: bool = Field(
        default=False,
        description="Master switch for the dashboard login-shepherd surface (`claude "
        "auth login` / `claude setup-token`, driven from the browser). Off by default; "
        "the whole surface 404s when off, same invisible-surface invariant as "
        "`config_write.enabled` and the reaper UI.",
    )
    allow_setup_token: bool = Field(
        default=False,
        description="A **second, independent** opt-in for the `setup-token` mode "
        "(`claude setup-token`), which mints a long-lived `CLAUDE_CODE_OAUTH_TOKEN` the "
        "operator copies out of the browser — a durable credential, strictly more "
        "dangerous than the ordinary `login` mode's short-lived OAuth handshake. Requires "
        "`login_shepherd.enabled` too; with this off, only the `login` mode is offered "
        "(a `setup-token` request 404s, the same invisible-surface shape as the whole "
        "disabled surface). Off by default; **not** web-editable.",
    )


class McpConfig(BaseModel):
    """Capability gate for the ``clauster mcp`` stdio server (#527/#950/#1010).

    The stdio MCP surface is **local-privileged and unauthenticated by design** —
    reachable only by a process the operator already launched on the host, so it
    carries no token auth. Its read tools (``list_sessions`` / ``session_status``)
    are always exposed; the write tools (``spawn_session`` / ``stop_session`` /
    ``resume_session``, added in #950) mutate bridge state and are gated behind this
    switch.

    ``allow_writes`` defaults **off** — the surface is read-only until the operator
    opts in — so attaching the server to an agent cannot start, stop, or resume a
    bridge unless writes are explicitly enabled. A missing/garbled flag means *off*
    (fail closed). **Not** in the config-editor Tier-A allowlist: a privileged-
    capability switch is file/CLI-managed only, never web-editable (mirrors the
    auth / config_write / login_shepherd exclusions).
    """

    allow_writes: bool = Field(
        default=False,
        description="Expose the `clauster mcp` **write** tools (`spawn_session` / "
        "`stop_session` / `resume_session`) that start, stop, and resume bridges. Off "
        "by default: the stdio MCP surface is read-only (`list_sessions` / "
        "`session_status` only) until you opt in. The surface is local-privileged and "
        "unauthenticated, so turning this on lets any agent the server is attached to "
        "drive the bridge lifecycle. **Not** web-editable — file/CLI-managed only, like "
        "the auth / config_write / login_shepherd gates.",
    )


class LogsConfig(BaseModel):
    """Bridge-log rotation sizing and WebSocket redaction/ANSI-stripping toggles."""

    bridge_log_max_size_mb: int = Field(
        default=10, ge=1, description="Per-bridge debug-log rotation size (≥1 MB)."
    )
    keep_rotated: int = Field(
        default=5, ge=0, description="Number of rotated log files to keep (≥0)."
    )
    redact_session_url: bool = Field(
        default=False,
        description="`false` = hybrid: the bridge debug log is verbatim on disk, "
        "redacted only over the WebSocket. `true` also redacts the on-disk bridge "
        "debug log — the bridge writes a private `0600` raw copy (which Clauster still "
        "parses for readiness + the deep link) and the public log becomes a redacted "
        "mirror of it. Scope is the bridge log only: the pty keeper sidecar and "
        "`state.json` still record session/environment ids as operational state, "
        "protected by `state_dir` permissions.",
    )
    strip_ansi_in_stream: bool = Field(
        default=True, description="Strip ANSI escape sequences from the streamed log."
    )
    retention_max_age_days: int = Field(
        default=30,
        ge=0,
        description="Delete a spawn's bridge-log set once its newest file is older than "
        "this many days (`0` = keep forever). Bounds unbounded disk growth and at-rest "
        "retention of session logs (which by default include the session URL). Pruned on "
        "each spawn.",
    )
    retention_max_files: int = Field(
        default=0,
        ge=0,
        description="Keep at most this many of the most recent bridge-log sets, deleting "
        "the oldest beyond it (`0` = unlimited). A 'set' is one spawn's `.log` + its "
        "`.raw.log` / `.stderr.log` / `.keeper.json` siblings.",
    )
    retention_max_total_mb: int = Field(
        default=0,
        ge=0,
        description="Cap the total size of the bridge-logs directory in MB, deleting the "
        "oldest sets until under the cap (`0` = unlimited).",
    )


class UsageConfig(BaseModel):
    """Per-project cost/token badge on the dashboard.

    ``mode`` selects what the badge shows:

    - ``"cost"``  — an approximate cost (token totals × a hand-maintained USD price
      table; see ``usage.py``), multiplied by ``fx_rate`` and prefixed with
      ``currency_symbol``.
    - ``"tokens"`` — the total token count only (no cost/currency at all).
    - ``"off"``   — hide the badge entirely; the dashboard also skips the
      ``/api/projects/{name}/usage`` fetch.

    The price table is **USD**. ``fx_rate`` is a *static, user-supplied* multiplier
    applied to the USD figure before display — there is no live FX lookup, so leave
    it ``1.0`` for USD and set it explicitly for any other currency. ``currency`` is
    the code shown in the tooltip; ``currency_symbol`` is what ``cost`` mode renders
    (defaults to ``$`` when unset and ``currency`` is ``USD``, otherwise the code).
    A non-USD ``currency`` left at ``fx_rate: 1.0`` is almost certainly a mistake —
    it stamps a foreign symbol on a dollar amount — so it is logged at load.

    ``token_total_includes_cache`` controls whether cache (creation + read) tokens
    count toward the displayed token total; they usually dominate, so set it false
    for a leaner "conversation size" figure. The per-category breakdown is always in
    the tooltip.

    ``show_cost`` is a **deprecated** back-compat alias. ``usage.mode`` is authoritative;
    ``show_cost: false`` is honored (mapped to ``mode: "off"``) only when ``mode`` is not
    set explicitly — if both are given, ``mode`` wins.
    """

    mode: Literal["cost", "tokens", "off"] = Field(
        default="cost",
        description="What the badge shows. `cost` = approximate cost (USD price table × "
        "`fx_rate`, prefixed with `currency_symbol`); `tokens` = total token count only; "
        "`off` = hide the badge and skip the `/api/projects/{name}/usage` fetch. "
        "(`mode: off` may be written unquoted — YAML's boolean `off` is coerced back.)",
    )
    currency: str = Field(
        default="USD",
        description="Currency code shown in the tooltip (normalized to upper-case).",
    )
    currency_symbol: str | None = Field(
        default=None,
        description="Symbol rendered in `cost` mode. Defaults to `$` when `currency` is "
        "`USD`, otherwise the currency code.",
    )
    fx_rate: float = Field(
        default=1.0,
        gt=0,
        description="**Static, user-supplied** multiplier applied to the USD cost before "
        "display (>0; no live FX lookup). Leave `1.0` for USD; a non-USD `currency` left "
        "at `1.0` logs a warning (it would label a USD figure with a foreign symbol).",
    )
    token_total_includes_cache: bool = Field(
        default=True,
        description="Whether cache (creation + read) tokens count toward the displayed "
        "token total; they usually dominate, so set `false` for a leaner figure. The "
        "per-category breakdown is always in the tooltip.",
    )
    show_cost: bool = Field(
        default=True,
        description="**Deprecated** back-compat alias. `usage.mode` is authoritative; "
        "`show_cost: false` maps to `mode: off` only when `mode` is unset "
        "(mode wins if both are set).",
    )

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, v: object) -> object:
        # Normalize the code so "usd"/" USD " compare equal to "USD" — otherwise a
        # lowercase code spuriously trips the no-FX warning and the symbol fallback.
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("currency_symbol", mode="before")
    @classmethod
    def _blank_symbol_to_none(cls, v: object) -> object:
        # An empty / whitespace-only symbol renders a blank badge; treat it as unset so
        # `effective_symbol` falls back to `$` (USD) or the currency code.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_yaml_off(cls, v: object) -> object:
        # YAML 1.1 parses an unquoted ``off`` as the boolean False (like yes/no/on/off),
        # so ``usage: {mode: off}`` would otherwise fail the Literal. Map it back to the
        # string so the config works without quoting. (``on``/True isn't a valid mode.)
        return "off" if v is False else v

    @model_validator(mode="after")
    def _resolve_mode_and_warn(self) -> UsageConfig:
        # `usage.mode` is authoritative. The deprecated `show_cost=false` is honored
        # (mapped to `mode: off`) only when `mode` was not set explicitly; if both are
        # given, mode wins. Mirrors the launch_mode/resume_mode alias precedence.
        if not self.show_cost:
            if "mode" in self.model_fields_set:
                if self.mode != "off":
                    _log.warning(
                        "usage.show_cost=false is ignored because usage.mode=%r is set "
                        "explicitly (mode wins); show_cost is deprecated — remove it and "
                        "set usage.mode='off' to hide the badge.",
                        self.mode,
                    )
            else:
                _log.warning(
                    "usage.show_cost=false is deprecated; mapping it to usage.mode='off'. "
                    "Set usage.mode='off' instead.",
                )
                # Reuse the reconcile registry's transform so the false->off mapping is
                # defined in exactly one place (clauster.reconcile.show_cost_to_mode).
                self.mode = show_cost_to_mode(self.show_cost)  # type: ignore[assignment]
        # A foreign currency with no FX rate paints a foreign symbol on a USD figure.
        if self.mode == "cost" and self.currency != "USD" and self.fx_rate == 1.0:
            _log.warning(
                "usage.currency=%r but usage.fx_rate=1.0 — the cost badge will show a "
                "USD amount labelled %r; set usage.fx_rate to convert from USD.",
                self.currency,
                self.currency,
            )
        return self

    @property
    def effective_symbol(self) -> str:
        """The symbol the ``cost`` badge renders (explicit, or ``$``/code fallback)."""
        if self.currency_symbol is not None:
            return self.currency_symbol
        return "$" if self.currency == "USD" else f"{self.currency} "


class MetricsConfig(BaseModel):
    """Live per-bridge resource metrics (CPU / memory / disk) on the dashboard card.

    A point-in-time sample of the bridge's process tree (see ``metrics.py``), shown
    only while a bridge runs. ``enabled`` hides the line **and** skips both the
    ``/api/projects/{name}/metrics`` fetch and the server-side sample.
    ``normalize_cpu`` divides the summed CPU% by the host core count (0–100% of the
    machine) instead of the raw across-cores figure (which can exceed 100%).
    ``show_disk`` toggles the disk read/write rate portion. ``sample_interval_seconds``
    is the two-snapshot window — longer is steadier but each fetch blocks a worker
    thread for that long. ``poll_seconds`` is the dashboard's metrics refresh cadence
    (decoupled from the status poll).
    """

    enabled: bool = Field(
        default=True,
        description="Show the per-session resource metrics line — live CPU, memory, and disk "
        "I/O for each running bridge. When `false`, the line is hidden and Clauster skips the "
        "work behind it entirely: no `/api/projects/{name}/metrics` polling from the browser "
        "and no server-side resource sampling.",
    )
    normalize_cpu: bool = Field(
        default=False,
        description="Divide summed CPU% by the host core count (0–100% of the machine) "
        "instead of the raw across-cores figure (which can exceed 100%).",
    )
    show_disk: bool = Field(default=True, description="Toggle the disk read/write rate portion.")
    sample_interval_seconds: float = Field(
        default=0.15,
        gt=0,
        le=2.0,
        description="Two-snapshot sampling window (>0, ≤2.0). Longer is steadier but "
        "each fetch blocks a worker thread for that long.",
    )
    poll_seconds: float = Field(
        default=4.0,
        ge=1.0,
        description="Dashboard metrics refresh cadence (≥1.0), decoupled from the status poll.",
    )


class ObservabilityConfig(BaseModel):
    """Read-only observability surfaces (a Prometheus ``/metrics`` exposition).

    ``prometheus_enabled`` gates a text-format ``/metrics`` endpoint that exposes a
    handful of point-in-time gauges (build info, bridge counts by status, project
    count) from live runner state. Off by default — opt in explicitly. When off,
    ``/metrics`` returns 404. The endpoint stays **behind** the auth guard, so a
    scraper must satisfy whatever auth the deployment enforces (see the PR note).
    """

    prometheus_enabled: bool = Field(
        default=False,
        description="Gate a text-format `/metrics` endpoint (build info, bridge counts "
        "by status, project count, per-bridge cpu/rss, crash counter, hosted/claustrum "
        "gauges). Off by default; when off, `/metrics` returns 404. The endpoint stays "
        "**behind** the auth guard unless `metrics_token_hash` is set.",
    )
    metrics_token_hash: str | None = Field(
        default=None,
        description="SHA-256 hash of an optional bearer token that lets a scraper (e.g. "
        "Prometheus) reach `/metrics` without a browser session — the scraper presents "
        "the raw token as `Authorization: Bearer <token>`. When set, a valid token OR a "
        "normal session grants access; when unset, `/metrics` stays behind the auth "
        "guard. Only the hash is stored (parity with `auth.api_token_hash`); the raw "
        "token is shown once by `clauster hash-metrics-token`. Supply via "
        "`CLAUSTER_OBSERVABILITY_METRICS_TOKEN_HASH_FILE` to keep it out of the config "
        "file.",
    )

    @field_validator("metrics_token_hash", mode="before")
    @classmethod
    def _blank_metrics_hash_is_none(cls, v: object) -> object:
        # Mirror auth.api_token_hash: a blank / whitespace-only hash can never match a
        # presented token (it fails closed), so normalize it to None so only a REAL hash
        # counts. A non-empty value that is NOT a 64-char lowercase hex digest can never
        # match a token (``hash_token`` always returns SHA-256 hex) — reject it loudly so
        # the operator fixes the config instead of shipping a /metrics token nothing can
        # ever present successfully.
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, str) and not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError(
                "metrics_token_hash must be a 64-character lowercase hex string "
                "(the SHA-256 output from `clauster hash-metrics-token`)"
            )
        return v


# The notification event taxonomy (#541). Each maps to a ``notify_on_*`` toggle on
# :class:`NotificationsConfig`. ``crash`` is the original event and stays default-ON
# (no behaviour regression); every other event is default-OFF, so an existing config
# keeps firing only crash alerts until the operator opts each new event in. Order is
# the documented taxonomy, not significant.
_NOTIFY_EVENTS: dict[str, str] = {
    "crash": "notify_on_crash",
    "ready": "notify_on_ready",
    "stop": "notify_on_stop",
    "permission-needed": "notify_on_permission",
    "session-ended": "notify_on_session_end",
    "reconnect-failed": "notify_on_reconnect_failed",
}


class NotificationsConfig(BaseModel):
    """Lifecycle notifications, over an outbound (Apprise) channel and/or the browser.

    Two independent channels, each with its own switch:

    * **Outbound** (``enabled`` + ``urls``) — sent via Apprise (the optional
      ``notify`` extra; ``pip install 'clauster[notify]'``). If enabled but Apprise
      isn't installed, Clauster logs a warning at startup and sends nothing
      (fail-closed — a notify failure never affects the bridge lifecycle). Sends are
      best-effort and run off the event loop.
    * **Browser** (``browser_enabled``) — Web Notifications shown by the dashboard
      via the JS ``Notification`` API; needs no extra and no URL, but the browser
      grants it only after the user accepts the permission prompt.

    Both channels share the per-event ``notify_on_*`` toggles. ``crash`` defaults ON
    (the historical behaviour); every other event defaults OFF, so an upgrade never
    starts emitting a new "come look" signal without an explicit opt-in.
    """

    enabled: bool = Field(
        default=False, description="Master switch for the outbound (Apprise) channel."
    )
    urls: list[str] = Field(
        default_factory=list,
        description="Apprise notification URLs (e.g. `slack://`, `discord://`, "
        "`tgram://`). Requires the `notify` extra. A non-loopback secret in a URL is "
        "the operator's responsibility to keep out of shared configs.",
    )
    browser_enabled: bool = Field(
        default=False,
        description="Master switch for the browser (Web Notifications) channel — the "
        "dashboard shows a desktop notification once the browser grants permission.",
    )
    notify_on_crash: bool = Field(
        default=True,
        description="Notify when a bridge exits unexpectedly (CRASHED — i.e. not via "
        "the Stop button).",
    )
    notify_on_ready: bool = Field(
        default=False,
        description="Notify when a bridge finishes starting and becomes ready (RUNNING).",
    )
    notify_on_stop: bool = Field(
        default=False,
        description="Notify when a bridge is stopped normally (via the Stop button).",
    )
    notify_on_permission: bool = Field(
        default=False,
        description="Notify when a hosted session parks a tool-permission prompt — the "
        "'come look' signal.",
    )
    notify_on_session_end: bool = Field(
        default=False,
        description="Notify when a session ends (a single-shot session bridge exits "
        "after its session completes).",
    )
    notify_on_reconnect_failed: bool = Field(
        default=False,
        description="Notify when a resume/reconnect attempt fails to bring a bridge back up.",
    )

    def event_enabled(self, event: str) -> bool:
        """Whether ``event`` should fire a notification (looks up its ``notify_on_*`` toggle).

        ``event`` is a key of :data:`_NOTIFY_EVENTS`; an unknown key returns False so a
        typo never silently emits.
        """
        attr = _NOTIFY_EVENTS.get(event)
        return bool(getattr(self, attr)) if attr is not None else False


# The four original bridge-lifecycle events (#371). An absent key for one of these
# defaults to ENABLED — the historical contract, preserved so an existing config that
# only lists a subset keeps emitting the rest.
_WEBHOOK_DEFAULT_ON_EVENTS = frozenset({"spawn", "ready", "stop", "crash"})

# Lifecycle events added beyond the original bridge four (#432). These default to
# DISABLED when absent: they can carry a "come look" signal (a parked permission
# prompt) or fire from a subsystem the operator didn't opt into, so a config that
# turns webhooks on must explicitly request each one rather than have it appear
# silently on upgrade. Order is the documented event taxonomy, not significant.
_WEBHOOK_DEFAULT_OFF_EVENTS = frozenset({"bg-settled", "permission-needed", "clone-done"})

# Every accepted ``events`` key. A key outside this set is a typo and fails load.
_WEBHOOK_KNOWN_EVENTS = _WEBHOOK_DEFAULT_ON_EVENTS | _WEBHOOK_DEFAULT_OFF_EVENTS


class WebhooksConfig(BaseModel):
    """Outbound HTTP webhooks on Clauster lifecycle transitions (the first extension seam).

    Off by default. When enabled, each configured URL receives a JSON ``POST`` on a
    lifecycle event. The original four are bridge events (``spawn`` / ``ready`` /
    ``stop`` / ``crash``); #432 adds ``bg-settled`` (a ``claude --bg`` background job
    settled), ``permission-needed`` (a hosted session parked a tool-permission prompt
    — the "come look" signal), and ``clone-done`` (a project clone finished). **Fail-
    open:** a slow or failing webhook is bounded by ``timeout_seconds`` and its error
    is logged and swallowed — it never blocks or breaks a lifecycle transition. URLs
    come only from this config (an operator-trusted source), not from runtime/user
    input.

    Default policy differs by event age: the **original four default to enabled** when
    their key is absent (the historical contract); the **three #432 events default to
    disabled** when absent (an operator must opt in to each, so a sensitive "come look"
    signal never starts egressing on upgrade alone).

    Scope: bridge events fire for bridges Clauster spawns and manages. A bridge
    **adopted** from an external session, or **reattached** on a Clauster restart, does
    not emit ``spawn``/``ready`` (it was not spawned here) — so ``ready`` is not a
    guarantee of "every bridge that is RUNNING", just "every bridge Clauster brought to
    RUNNING".

    ``block_private_targets`` (default off) is an opt-in SSRF guard that drops webhook
    URLs whose host is a loopback/link-local/private IP literal — or a DNS name that
    resolves to one; see its field description.
    """

    enabled: bool = Field(default=False, description="Master switch for outbound webhooks.")
    urls: list[str] = Field(
        default_factory=list,
        description="HTTP(S) endpoint URLs that receive a JSON POST per lifecycle event. "
        "Only `http`/`https` schemes are accepted; others are rejected at startup. A "
        "secret embedded in a URL is the operator's responsibility to keep out of shared "
        "configs.",
    )
    timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Per-request POST timeout in seconds (>0). A slow endpoint can't stall "
        "a lifecycle transition beyond this.",
    )
    events: dict[str, bool] = Field(
        default_factory=lambda: {"spawn": True, "ready": True, "stop": True, "crash": True},
        description="Which lifecycle events to emit. Bridge keys `spawn`/`ready`/`stop`/"
        "`crash` default to enabled when absent; the #432 keys `bg-settled`/"
        "`permission-needed`/`clone-done` default to DISABLED when absent (opt in "
        "explicitly).",
    )
    block_private_targets: bool = Field(
        default=False,
        description="Opt-in SSRF guard. When True, skip any webhook URL whose host is — or "
        "resolves to — an internal/non-routable IP: loopback, link-local (incl. the "
        "169.254.169.254 metadata IP), RFC1918 private, unspecified (0.0.0.0/::), reserved, "
        "multicast, IPv6 ULA (fc00::/7), and CGNAT (100.64/10) — including the non-canonical "
        "IPv4 encodings the resolver still honors (decimal-int, hex, short 127.1). A DNS "
        "hostname is resolved best-effort at filter time so a name pointing straight at a "
        "private IP can't bypass the guard; a rebinding domain that re-resolves at dial time "
        "is an acknowledged TOCTOU residual (same class as the clone-URL guard). Exotic IPv6 "
        "embeddings (NAT64, IPv4-compatible) are not normalized. Default False preserves the "
        "LAN-receiver use case.",
    )

    @field_validator("events")
    @classmethod
    def _known_event_keys(cls, value: dict[str, bool]) -> dict[str, bool]:
        """Reject an unknown event key so a typo (e.g. `spwan`) fails loudly, not silently."""
        unknown = set(value) - _WEBHOOK_KNOWN_EVENTS
        if unknown:
            raise ValueError(
                f"webhooks.events has unsupported key(s) {sorted(unknown)}; "
                f"allowed: {sorted(_WEBHOOK_KNOWN_EVENTS)}"
            )
        return value

    def event_enabled(self, event: str) -> bool:
        """Whether ``event`` should emit, applying the per-event absent-key default.

        An explicit key in ``events`` always wins. When absent, an original bridge
        event defaults to enabled and a #432 event defaults to disabled — so a new
        "come look" signal never starts egressing without an explicit opt-in.
        """
        if event in self.events:
            return self.events[event]
        return event in _WEBHOOK_DEFAULT_ON_EVENTS


class ClaustrumConfig(BaseModel):
    """Settings for the optional claustrum hosted live-view channel (CL-2).

    Off by default. When enabled, Clauster connect-or-spawns a single
    ``claustrum`` daemon per deployment (the maintainer's Go ``claude-ssh``
    reimpl) and surfaces its health in ``/healthz``. The daemon self-daemonizes
    (it survives Clauster restarts; Clauster reconnects + reattaches), so it is
    deliberately not a systemd unit in v1. Fail-closed: an unreachable daemon or
    rejected auth surfaces in health and never affects the bridge lifecycle.
    """

    enabled: bool = Field(
        default=False,
        description="Master switch for the claustrum hosted channel. When true, "
        "Clauster connect-or-spawns the daemon at startup.",
    )
    binary: str = Field(
        default="claustrum",
        description="The `claustrum` binary name or path (resolved to an absolute "
        "path before spawning).",
    )
    socket_path: str | None = Field(
        default=None,
        description="Path to the daemon's AF_UNIX socket. Defaults to "
        "`<state_dir>/claustrum/daemon.sock`.",
    )
    spawn_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="How long (>0) to wait for a freshly spawned daemon to detach "
        "and accept its first connection before giving up.",
    )
    keep_children: bool = Field(
        default=True,
        description="Spawn the daemon with -keep-children so a daemon restart/upgrade "
        "leaves hosted sessions running (Clauster reattaches or offers recovery on "
        "reconnect). Set false for clean-slate-on-restart. POSIX-only (the daemon "
        "ignores it with a warning on Windows).",
    )
    request_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Per-request timeout (>0) for RPCs on the daemon connection.",
    )


def resolve_cert_path(field: str, raw: str) -> Path:
    """Resolve a TLS material path to a readable absolute file, or fail closed.

    Expands ``~``, resolves to an absolute path (collapsing any ``..`` traversal so
    the value can't quietly escape its intended directory), and confirms the target
    is a regular file the process can read. Every failure raises ``ValueError`` — a
    missing cert/key must abort startup, never fall back to plain HTTP. The path is
    echoed in the message (a filesystem path, not the key material), the bytes never.
    """
    expanded = Path(raw).expanduser()
    # resolve() collapses `..`/symlinks to a single canonical absolute path; strict
    # would raise its own FileNotFoundError, but we want our explicit message + the
    # readable-file checks below, so resolve non-strict then validate.
    resolved = expanded.resolve()
    if not resolved.exists():
        raise ValueError(f"tls.{field} does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"tls.{field} is not a regular file: {resolved}")
    if not os.access(resolved, os.R_OK):
        raise ValueError(f"tls.{field} is not readable: {resolved}")
    return resolved


class TlsConfig(BaseModel):
    """Native HTTPS termination: serve TLS directly from a cert + key pair.

    Off when absent. When set, uvicorn is handed ``ssl_certfile`` / ``ssl_keyfile``
    and terminates TLS itself — an alternative to an external reverse proxy or
    ``tailscale serve`` for operators who want neither.

    Two operating modes, selected by ``provision``:

    ``provision = off`` (default)
        Supply an already-provisioned cert + key via ``cert_file`` / ``key_file``.
        Both paths are validated at config-load (existence + readable + absolute,
        traversal collapsed) and re-checked at server start; a missing/unreadable
        file aborts startup rather than silently serving plain HTTP.

    ``provision = self-signed``
        Clauster generates a self-signed RSA-2048 cert+key pair under
        ``state_dir/tls/`` at startup (``cryptography`` package required).
        The cert is regenerated automatically when it is near-expiry or the
        SAN set changes. ``hostnames`` must list at least one name or IP.
        ``cert_file`` / ``key_file`` must be absent — they are written by the
        provisioner and passed to uvicorn directly.

    ACME / Let's Encrypt provisioning is deferred to issue 774.
    """

    provision: Literal["off", "self-signed"] = Field(
        default="off",
        description="TLS provisioning mode. `off` (default) = supply `cert_file` + "
        "`key_file` pointing at an already-provisioned cert + key. `self-signed` = "
        "Clauster generates a self-signed cert+key under `state_dir/tls/` at startup "
        "(requires the `cryptography` package; regenerates on expiry or SAN change). "
        "ACME / Let's Encrypt is deferred to issue 774.",
    )
    hostnames: list[str] = Field(
        default_factory=list,
        description="Hostnames and/or IP addresses to include as Subject Alternative "
        "Names in the generated certificate. The first entry becomes the Common Name. "
        "Required when `provision = self-signed`; ignored otherwise.",
    )
    cert_file: str | None = Field(
        default=None,
        description="Path to the PEM certificate (chain) file. `~` is expanded and "
        "the path is resolved to an absolute file at load — it must exist and be "
        "readable. Required when `provision = off`; must be absent when "
        "`provision = self-signed` (the cert is written by the provisioner).",
    )
    key_file: str | None = Field(
        default=None,
        description="Path to the PEM private-key file. `~` is expanded and the path "
        "is resolved to an absolute file at load — it must exist and be readable. "
        "Required when `provision = off`; must be absent when "
        "`provision = self-signed` (the key is written by the provisioner).",
    )

    @model_validator(mode="after")
    def _validate_material(self) -> TlsConfig:
        # Fail closed at load: validate config shape per provision mode and, for
        # provision=off, resolve both paths to readable absolute files (or raise).
        # Store the resolved paths back so the server hands uvicorn canonical absolutes
        # and a second defense-in-depth check at start-up sees the same values.
        if self.provision == "self-signed":
            if self.cert_file is not None or self.key_file is not None:
                raise ValueError(
                    "tls.cert_file / tls.key_file must not be set when "
                    "tls.provision = self-signed — the provisioner writes them under "
                    "state_dir/tls/; remove them from your config."
                )
            # Strip each entry and reject blank/whitespace-only ones: a "  " entry would
            # otherwise survive into a bogus (blank) DNSName SAN. Malformed-but-nonempty
            # entries are allowed through as DNS names (documented) — only truly empty
            # SANs are rejected here. Zero-padded IPv4 like "192.168.001.010" is NOT a
            # valid IP per ipaddress and becomes a DNS SAN (documented behaviour).
            stripped = [h.strip() for h in self.hostnames]
            if any(not h for h in stripped):
                raise ValueError(
                    "tls.hostnames contains a blank / whitespace-only entry; every "
                    "hostname or IP address must be non-empty."
                )
            self.hostnames = stripped
            if not self.hostnames:
                raise ValueError(
                    "tls.hostnames must list at least one hostname or IP address "
                    "when tls.provision = self-signed."
                )
        else:
            # provision = off: both paths are required and must be resolvable now.
            if self.cert_file is None:
                raise ValueError("tls.cert_file is required when tls.provision = off.")
            if self.key_file is None:
                raise ValueError("tls.key_file is required when tls.provision = off.")
            self.cert_file = str(resolve_cert_path("cert_file", self.cert_file))
            self.key_file = str(resolve_cert_path("key_file", self.key_file))
        return self


class DbConfig(BaseModel):
    """Persistence-layer knobs (#795)."""

    backup_before_migrate: bool = Field(
        default=True,
        description="Snapshot `clauster.db` (via `VACUUM INTO`) to `state_dir/backups/` "
        "before running a **pending** Alembic migration — never on a plain restart "
        "already at head. The last 5 pre-migration snapshots are kept, older ones "
        "pruned. A snapshot write failure is logged as a WARNING and startup "
        "proceeds (the migration itself is transactional and safe on its own); set "
        "this to `false` to skip the snapshot attempt entirely.",
    )


class ClausterConfig(BaseModel):
    """Top-level Clauster configuration (the parsed, validated ``clauster.yml``)."""

    schema_version: int = Field(
        default=SCHEMA_VERSION, description="Config schema version (additive-only)."
    )
    projects_root: Path = Field(
        description="Directory whose children become project cards. Must exist, be a "
        "directory, and be readable — validated at load. `~` is expanded.",
    )
    host: str = Field(
        default="127.0.0.1",
        description="Bind address. A non-loopback host requires enforced auth (see Networking).",
    )
    port: int = Field(default=7621, ge=1, le=65535, description="Bind port (1–65535).")
    state_dir: Path = Field(
        default=Path("~/.clauster"),
        description="Where `clauster.db` and runtime state live (`state.json` is a "
        "legacy import source). `~` is expanded.",
    )
    root_path: str = Field(
        default="",
        description="ASGI `root_path` for serving under a reverse-proxy sub-path.",
    )
    log_format: Literal["text", "json"] = Field(
        default="text",
        description="Application log format. `text` (default) is the human single-line "
        "format; `json` emits one structured JSON object per record. Both modes redact "
        "session URLs / bearer ids before the line is written.",
    )
    instance_name: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Optional label (≤32 chars, `[A-Za-z0-9_.-]`). When set, retitles "
        "the process to `clauster[<name>]` so co-resident instances are distinguishable "
        "in `ps`/`pgrep`. Cosmetic only.",
    )

    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    instance_defaults: InstanceDefaults = Field(default_factory=InstanceDefaults)
    projects: dict[str, ProjectConfig] = Field(
        default_factory=dict,
        description="Per-project settings, keyed by project name (additive-only; "
        "unknown keys ignored). See the `projects` section.",
    )
    auth: AuthConfig = Field(default_factory=AuthConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    db: DbConfig = Field(default_factory=DbConfig)
    logs: LogsConfig = Field(default_factory=LogsConfig)
    clone: CloneConfig = Field(default_factory=CloneConfig)
    reaper: ReaperConfig = Field(default_factory=ReaperConfig)
    config_write: ConfigWriteConfig = Field(default_factory=ConfigWriteConfig)
    login_shepherd: LoginShepherdConfig = Field(default_factory=LoginShepherdConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
    claustrum: ClaustrumConfig = Field(default_factory=ClaustrumConfig)
    tls: TlsConfig | None = Field(
        default=None,
        description="Native HTTPS termination. Unset (default) = serve plain HTTP and "
        "rely on a reverse proxy / `tailscale serve` for TLS. Set `tls` to have "
        "Clauster terminate TLS itself. Two modes: `provision = off` (default) requires "
        "`cert_file` + `key_file` pointing at an existing cert + key (validated "
        "fail-closed); `provision = self-signed` generates a self-signed cert+key under "
        "`state_dir/tls/` automatically (`cryptography` package required). ACME is "
        "deferred to issue 774.",
    )

    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        """Filesystem path the config was loaded from, or None if not yet loaded."""
        return self._source_path

    @property
    def tls_active(self) -> bool:
        """Whether native TLS termination is configured (a validated cert + key present).

        The single source of truth shared by the server bring-up (which hands uvicorn
        ``ssl_certfile``/``ssl_keyfile``) and the cookie-Secure startup warning (which
        stays quiet because the connection is now https).
        """
        return self.tls is not None

    def allows_bypass(self, project_name: str) -> bool:
        """Whether the config hard-ceiling permits bypassPermissions for this project."""
        pc = self.projects.get(project_name)
        return bool(pc and pc.allow_bypass_permissions)

    def bypass_denied(self, project_name: str, permission_mode: str | None) -> bool:
        """Whether ``bypassPermissions`` is requested but the project's ceiling forbids it.

        The single decision every spawn channel shares: it is the one place that
        combines "is bypass being asked for" with the per-project hard ceiling
        (:meth:`allows_bypass`). Each channel calls this and raises its own
        exception type, so a new spawn path cannot diverge from the ceiling by
        hand-rolling the check. Returns ``True`` only when the effective mode is
        ``bypassPermissions`` and the project does not allow it (fail closed:
        callers raise when this is ``True``).
        """
        return permission_mode == "bypassPermissions" and not self.allows_bypass(project_name)

    @field_validator("projects_root", "state_dir", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, (str, Path)):
            return Path(v).expanduser()
        return v

    @field_validator("projects_root")
    @classmethod
    def _projects_root_exists(cls, v: Path) -> Path:
        if not v.is_dir():
            raise ValueError(f"projects_root does not exist or is not a directory: {v}")
        if not os.access(v, os.R_OK):
            raise ValueError(f"projects_root is not readable: {v}")
        return v

    @model_validator(mode="after")
    def _loopback_or_authed(self) -> ClausterConfig:
        # Non-loopback bind is only allowed once authentication will ACTUALLY gate it.
        # The runtime guard enforces auth only when `auth.enabled` is true; with it false
        # every request passes through unauthenticated, so `password_required` /
        # `reverse_proxy.enabled` *without* `auth.enabled` is a silent open door — the
        # operator sets a password, the validator accepted it, yet the dashboard is served
        # to anyone. Require enforcement to be real here (fail closed) instead. Shared with
        # ops._check_auth via _missing_enforced_auth so validation and diagnostics agree.
        a = self.auth
        if _missing_enforced_auth(self.host, a) and not a.allow_unauthenticated_network:
            raise ValueError(
                f"refusing non-loopback host={self.host!r} without enforced auth. Set "
                "auth.enabled: true together with auth.password_required (+ a hash from "
                "`clauster hash-password`), auth.reverse_proxy.enabled, or auth.api_token_hash "
                "(from `clauster hash-token`) — or, to opt out on a trusted LAN, "
                "auth.allow_unauthenticated_network."
            )
        # Fail closed: password auth required but no hash configured would lock everyone out
        # (or, worse, be skipped) — refuse to start with a clear message.
        if self.auth.password_required and not self.auth.password_hash:
            raise ValueError(
                "auth.password_required is set but auth.password_hash is empty. "
                "Generate one with `clauster hash-password` (or set CLAUSTER_AUTH_PASSWORD_HASH)."
            )
        return self


def _candidate_paths(explicit: Path | None) -> list[Path]:
    if explicit is not None:
        return [explicit]
    paths: list[Path] = []
    env_config = os.environ.get("CLAUSTER_CONFIG")
    if env_config:
        paths.append(Path(env_config).expanduser())
    paths.append(Path.cwd() / "clauster.yml")
    home = os.environ.get("CLAUSTER_HOME")
    if home:
        paths.append(Path(home).expanduser() / "clauster.yml")
    return paths


def first_config_path(path: str | os.PathLike | None = None) -> Path:
    """Return the path :func:`load_config` reads (and writes) first (#978).

    The first-run setup wizard writes here so the re-exec'd ``load_config`` finds it: an
    explicit ``-c`` path when given, else the highest-priority default in the search order
    (``$CLAUSTER_CONFIG`` → ``./clauster.yml`` → ``$CLAUSTER_HOME/clauster.yml``).
    """
    explicit = Path(path).expanduser() if path is not None else None
    return _candidate_paths(explicit)[0]


def _nested_model(ann: object) -> type[BaseModel] | None:
    """Return the nested ``BaseModel`` an annotation wraps for env-var recursion, or ``None``.

    Handles a bare ``BaseModel`` subclass and an ``Optional[BaseModel]`` (``X | None``,
    e.g. ``tls: TlsConfig | None``) so an optional nested section's scalar leaves still
    get ``CLAUSTER_<SECTION>_<LEAF>`` env vars — without the ``Optional`` case the field
    short-circuits to a bogus ``CLAUSTER_TLS`` scalar no string could ever satisfy.

    Deliberately does NOT unwrap ``dict``/``list`` containers (e.g.
    ``dict[str, ProjectConfig]``): those stay unmappable leaves, because a single env
    var can't address one entry of a map — recursing into the value model would invent
    phantom keys like ``CLAUSTER_PROJECTS_<LEAF>`` that pollute the ``projects`` map.
    """
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    # Only unwrap a Union/Optional (`X | None` or `Optional[X]`) — never a generic
    # container like dict/list, whose args are a key/value type, not a section model.
    if get_origin(ann) in (Union, types.UnionType):
        for arg in get_args(ann):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _scalar_env_map(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> dict[str, tuple[str, ...]]:
    """Map CLAUSTER_<UPPER_SNAKE_PATH> -> dotted path for every scalar leaf.

    Nested models recurse (including ``Optional`` ones); dict/list leaves (e.g.
    projects, trusted_ips) are skipped because a single env var can't express them
    unambiguously.
    """
    out: dict[str, tuple[str, ...]] = {}
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        nested = _nested_model(field.annotation)
        if nested is not None:
            out.update(_scalar_env_map(nested, path))
        else:
            env_name = "CLAUSTER_" + "_".join(path).upper()
            out[env_name] = path
    return out


def _set_nested(d: dict, path: tuple[str, ...], value: object) -> None:
    cur = d
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def _read_secret_file(file_var: str, file_path: str) -> str:
    """Return the secret in ``file_path``, trailing whitespace stripped. Fail closed.

    Secret files (Docker/K8s/Vault render them under ``/run/secrets``) usually carry
    a trailing newline, so it is stripped. Every failure mode surfaces rather than
    silently falling back to the plain env var: an unreadable path, non-UTF-8 bytes
    (a binary/corrupt mount), or an empty file (a blank-rendered secret) all raise.
    """
    try:
        value = Path(file_path).read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        # The error carries the offending bytes (a secret fragment) — keep them out of
        # the message AND the traceback (``from None``) so nothing leaks.
        raise ValueError(f"{file_var} file {file_path!r} is not valid UTF-8 text") from None
    except OSError as exc:
        raise ValueError(f"{file_var} points to an unreadable file {file_path!r}: {exc}") from exc
    if not value:
        raise ValueError(f"{file_var} points to an empty file {file_path!r}")
    return value


# Legacy env-var aliases for renamed config keys: old name -> new dotted path. The new
# name always wins; the legacy var is honored (with a deprecation warning) only when the
# new one is unset, so a renamed key never silently loses its env override.
_LEGACY_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "CLAUSTER_CLAUDE_RESUME_MODE": ("claude", "launch_mode"),  # renamed in #540
}


def _apply_env_overrides(data: dict) -> dict:
    for env_name, path in _scalar_env_map(ClausterConfig).items():
        # Secret indirection: for any CLAUSTER_<X>, a CLAUSTER_<X>_FILE wins and reads
        # the value from a file, keeping the argon2 hash / session secret out of the
        # process environment. A blank _FILE is treated as unset (falls through).
        file_path = os.environ.get(f"{env_name}_FILE", "").strip()
        if file_path:
            _set_nested(data, path, _read_secret_file(f"{env_name}_FILE", file_path))
        elif env_name in os.environ:
            _set_nested(data, path, os.environ[env_name])
    for env_name, path in _LEGACY_ENV_ALIASES.items():
        new_env = "CLAUSTER_" + "_".join(path).upper()
        legacy_set = bool(env_name in os.environ or os.environ.get(f"{env_name}_FILE", "").strip())
        # The new name wins: skip the legacy var when the new one is set — but if the operator
        # set BOTH, warn that the legacy var is ignored (mirrors the YAML both-keys warning) so
        # a stale env override is never silently dropped.
        if new_env in os.environ or os.environ.get(f"{new_env}_FILE", "").strip():
            if legacy_set:
                _log.warning(
                    "config: both %s and the deprecated %s are set — using %s, ignoring %s.",
                    new_env,
                    env_name,
                    new_env,
                    env_name,
                )
            continue
        file_path = os.environ.get(f"{env_name}_FILE", "").strip()
        if file_path:
            value: str | None = _read_secret_file(f"{env_name}_FILE", file_path)
        elif env_name in os.environ:
            value = os.environ[env_name]
        else:
            continue
        _log.warning(
            "config: env var %s is deprecated; use %s. Honoring the old name for now.",
            env_name,
            new_env,
        )
        _set_nested(data, path, value)
    return data


def load_config(path: str | os.PathLike | None = None) -> ClausterConfig:
    """Load, env-override, and validate the Clauster config.

    Raises FileNotFoundError if no config file is found in the search order.
    """
    explicit = Path(path).expanduser() if path is not None else None
    candidates = _candidate_paths(explicit)
    found: Path | None = next((p for p in candidates if p.is_file()), None)
    if found is None:
        searched = ", ".join(str(p) for p in candidates)
        raise FileNotFoundError(f"no clauster.yml found (searched: {searched})")

    raw = yaml.safe_load(found.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping, got {type(raw).__name__}: {found}")

    raw = _apply_env_overrides(raw)
    config = ClausterConfig.model_validate(raw)
    config._source_path = found
    # database_url was removed in 0.13 (#796): clauster is SQLite-only. The old setting is
    # dropped so an old config still LOADS (additive-only), but do NOT drop it silently — an
    # operator who set a Postgres DSN would otherwise believe their data lives there while
    # every write goes to local SQLite ("fail closed, never silently"). Catch BOTH sources:
    # the YAML key AND the CLAUSTER_DATABASE_URL[_FILE] env override — removing the model
    # field also drops the env var from `_scalar_env_map`, so it never reaches `raw` and the
    # `in raw` check alone would miss the env path (Greptile #801 R2).
    env_dsn_set = "CLAUSTER_DATABASE_URL" in os.environ or bool(
        os.environ.get("CLAUSTER_DATABASE_URL_FILE", "").strip()
    )
    if "database_url" in raw or env_dsn_set:
        from .db.engine import resolve_url  # local import: avoid a config->db import cycle

        _log.warning(
            "`database_url` (config key or CLAUSTER_DATABASE_URL env) is no longer supported "
            "and was IGNORED — clauster is SQLite-only (#796). Data is stored at "
            "%s. Remove it to silence this warning.",
            resolve_url(config.state_dir),
        )
    return config
