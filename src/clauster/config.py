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
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

_log = logging.getLogger("clauster.config")

SCHEMA_VERSION = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# The spawn/permission modes `claude remote-control` accepts (this list last
# verified against `claude remote-control --help` at claude 2.1.156 — a check
# version, distinct from the `min_version` support floor below). worktree requires
# a git repo; bypassPermissions is footgun-gated (see `ProjectConfig.allow_bypass_permissions`).
SpawnMode = Literal["same-dir", "worktree", "session"]
PermissionMode = Literal["default", "plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions"]
# How a bridge is launched. "standard" = the headless `claude remote-control`
# subcommand server (multi-session, survives a restart, no conversation resume).
# "pty" = the `claude --remote-control` flag form under a PTY keeper, which is
# single-session but genuinely restores prior context on restart (true resume).
ResumeMode = Literal["standard", "pty"]
# Orthogonal to ResumeMode: which substrate hosts a managed session. "remote-control"
# is the bridge (standard/pty modes above); "hosted" is the claustrum-daemon headless
# stream-json channel (CL-4). ResumeMode applies only to the remote-control channel.
SessionChannel = Literal["remote-control", "hosted"]
SPAWN_MODES: tuple[str, ...] = ("same-dir", "worktree", "session")
RESUME_MODES: tuple[str, ...] = ("standard", "pty")
PERMISSION_MODES: tuple[str, ...] = (
    "default",
    "plan",
    "acceptEdits",
    "auto",
    "dontAsk",
    "bypassPermissions",
)


class ClaudeConfig(BaseModel):
    """Settings for the `claude` binary and bridge-spawn behavior."""

    binary: str = Field(
        default="claude",
        description="The `claude` binary name or path (resolved to an absolute path "
        "before spawning).",
    )
    min_version: str = Field(default="2.1.145", description="Minimum acceptable `claude` version.")
    agents_json_poll_interval_seconds: int = Field(
        default=300,
        ge=1,
        description="How often (≥1) the inspector cross-checks `claude agents --json` "
        "for liveness.",
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
        "PTY keeper (POSIX only; falls back to standard on Windows). A bridge keeps the "
        "mode it launched with — editing this never re-modes a running or stopped bridge. "
        "(Renamed from `resume_mode`, still accepted as a deprecated alias.)",
    )
    path_append: list[str] = Field(
        default_factory=list,
        description="Directories appended to the bridge subprocess `PATH` so a `claude` "
        "session can resolve user-local tools (e.g. `~/.local/bin`) that a minimal service "
        "`PATH` omits. `~` is expanded; entries are appended in order after the inherited "
        "`PATH`, never replacing it. Applies to both standard and pty bridges.",
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
                data["launch_mode"] = legacy
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
        default_factory=list, description="Peer-IP allowlist for the proxy."
    )
    shared_secret: str | None = Field(
        default=None, description="HMAC key the proxy signs `X-Proxy-Auth` with."
    )
    hmac_window_seconds: int = Field(
        default=60, ge=0, description="Clock-skew / replay window (≥0)."
    )


class AuthConfig(BaseModel):
    """v0.2 auth foundation. Parsed (and ignored) since v0.1; enforced when enabled."""

    enabled: bool = Field(
        default=False,
        description="**Master auth switch.** Must be `true` for password / "
        "reverse-proxy auth to actually gate requests.",
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
        "auth (e.g. a trusted LAN). `ops._check_auth` downgrades this to a warning.",
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
        description="Extra WebSocket / CSRF origins (e.g. the proxy domain).",
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
        description="Block private/LAN IP targets by default (SSRF guard).",
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

    ``show_cost`` is a **deprecated** back-compat alias: ``show_cost: false`` forces
    ``mode: "off"`` (its only historical effect was hiding the badge).
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
        description="**Deprecated** back-compat alias: `show_cost: false` forces `mode: off`.",
    )

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, v: object) -> object:
        # Normalize the code so "usd"/" USD " compare equal to "USD" — otherwise a
        # lowercase code spuriously trips the no-FX warning and the symbol fallback.
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_yaml_off(cls, v: object) -> object:
        # YAML 1.1 parses an unquoted ``off`` as the boolean False (like yes/no/on/off),
        # so ``usage: {mode: off}`` would otherwise fail the Literal. Map it back to the
        # string so the config works without quoting. (``on``/True isn't a valid mode.)
        return "off" if v is False else v

    @model_validator(mode="after")
    def _resolve_mode_and_warn(self) -> UsageConfig:
        # Back-compat: show_cost=false historically hid the whole badge -> mode off.
        if not self.show_cost:
            if "mode" in self.model_fields_set and self.mode != "off":
                _log.warning(
                    "usage.show_cost=false overrides usage.mode=%r (badge hidden); "
                    "show_cost is deprecated — set usage.mode='off' instead.",
                    self.mode,
                )
            self.mode = "off"
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


class NotificationsConfig(BaseModel):
    """Outbound notifications on bridge lifecycle events, sent via Apprise.

    Off by default. Requires the optional ``notify`` extra
    (``pip install 'clauster[notify]'``); if it's enabled but Apprise isn't
    installed, Clauster logs a warning at startup and sends nothing (fail-closed —
    a notify failure never affects the bridge lifecycle). Sends are best-effort and
    run off the event loop.
    """

    enabled: bool = Field(default=False, description="Master switch for outbound notifications.")
    urls: list[str] = Field(
        default_factory=list,
        description="Apprise notification URLs (e.g. `slack://`, `discord://`, "
        "`tgram://`). Requires the `notify` extra. A non-loopback secret in a URL is "
        "the operator's responsibility to keep out of shared configs.",
    )
    notify_on_crash: bool = Field(
        default=True,
        description="Notify when a bridge exits unexpectedly (CRASHED — i.e. not via "
        "the Stop button).",
    )


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
    URLs whose host is a loopback/link-local/private IP literal; see its field description.
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
        description="Opt-in SSRF guard. When True, skip any webhook URL whose host is an "
        "internal/non-routable IP literal — loopback, link-local (incl. the 169.254.169.254 "
        "metadata IP), RFC1918 private, unspecified (0.0.0.0/::), reserved, multicast, IPv6 "
        "ULA (fc00::/7), and CGNAT (100.64/10) — including the non-canonical IPv4 encodings "
        "the resolver still honors (decimal-int, hex, short 127.1). Default False preserves "
        "the LAN-receiver use case. DNS hostnames are NOT resolved (rebinding) and exotic "
        "IPv6 embeddings (NAT64, IPv4-compatible) are not normalized — out of scope for this "
        "literal-IP seam.",
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
    database_url: str | None = Field(
        default=None,
        description="SQLAlchemy URL for the persistence database. Unset (the "
        "default) uses a SQLite file `clauster.db` under `state_dir`. Set a Postgres "
        "DSN (e.g. `postgresql+psycopg://…`) for a shared/multi-user deployment.",
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
    logs: LogsConfig = Field(default_factory=LogsConfig)
    clone: CloneConfig = Field(default_factory=CloneConfig)
    reaper: ReaperConfig = Field(default_factory=ReaperConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
    claustrum: ClaustrumConfig = Field(default_factory=ClaustrumConfig)

    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        """Filesystem path the config was loaded from, or None if not yet loaded."""
        return self._source_path

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


def _scalar_env_map(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> dict[str, tuple[str, ...]]:
    """Map CLAUSTER_<UPPER_SNAKE_PATH> -> dotted path for every scalar leaf.

    Nested models recurse; dict/list leaves (e.g. projects, trusted_ips) are
    skipped because a single env var can't express them unambiguously.
    """
    out: dict[str, tuple[str, ...]] = {}
    for name, field in model.model_fields.items():
        ann = field.annotation
        path = (*prefix, name)
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out.update(_scalar_env_map(ann, path))
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
    return config
