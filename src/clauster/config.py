"""Configuration loading for Clauster.

Search order:  $CLAUSTER_CONFIG  ->  ./clauster.yml  ->  $CLAUSTER_HOME/clauster.yml
Any scalar key is overridable via env: CLAUSTER_<UPPER_SNAKE_CASE_PATH>=value.
Schema is additive-only: old configs must always validate against newer versions.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

SCHEMA_VERSION = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# The spawn/permission modes `claude remote-control` accepts (verified against
# `claude remote-control --help`, claude 2.1.156). worktree requires a git repo;
# bypassPermissions is footgun-gated (see `ProjectConfig.allow_bypass_permissions`).
SpawnMode = Literal["same-dir", "worktree", "session"]
PermissionMode = Literal["default", "plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions"]
SPAWN_MODES: tuple[str, ...] = ("same-dir", "worktree", "session")
PERMISSION_MODES: tuple[str, ...] = (
    "default",
    "plan",
    "acceptEdits",
    "auto",
    "dontAsk",
    "bypassPermissions",
)


class ClaudeConfig(BaseModel):
    binary: str = "claude"
    min_version: str = "2.1.145"
    agents_json_poll_interval_seconds: int = Field(default=300, ge=1)
    # How long a freshly-spawned bridge may stay alive without registering an
    # environment before Clauster gives up and marks it ERROR. A bridge that
    # launches but can't authenticate to the remote-control controller stays
    # alive yet never becomes connectable; liveness alone is not "running".
    startup_grace_seconds: float = Field(default=60.0, gt=0)
    # Before spawning the first bridge, mark remote control as acknowledged in the
    # runtime user's ~/.claude.json (hasUsedRemoteControl/remoteDialogSeen).
    # `claude remote-control` otherwise blocks on a one-time interactive "Enable
    # Remote Control? (y/n)" prompt that Clauster can never answer (the bridge's
    # stdin is detached) — so the bridge would sit alive-but-unregistered forever.
    # Set false to manage those flags yourself.
    auto_enable_remote_control: bool = True
    # `claude remote-control` restart spawns a fresh session with an empty
    # context window — it has no resume flag, so a restarted bridge "forgets"
    # the prior conversation. When true, Clauster installs a SessionStart hook
    # (in the runtime user's ~/.claude/settings.json) that recaps the most
    # recent prior transcript for the cwd back into the new session. Opt-in: it
    # edits the user's Claude settings and injects prior turns into context.
    resume_recap: bool = False
    # Character budget for the recap injection (most recent turns kept).
    resume_recap_max_chars: int = Field(default=8000, ge=500)


class InstanceDefaults(BaseModel):
    spawn_mode: SpawnMode = "same-dir"
    permission_mode: PermissionMode = "default"
    capacity: int = Field(default=32, ge=1)


class ProjectConfig(BaseModel):
    """Per-project config (the `projects:` map). Additive-only; unknown keys ignored.

    `allow_bypass_permissions` is the *hard ceiling* for the footgun gate: a project
    can never be spawned with `--permission-mode bypassPermissions` unless this is set
    here in clauster.yml. The dashboard's per-session typed-confirm is the second layer.
    """

    allow_bypass_permissions: bool = False


class ReverseProxyConfig(BaseModel):
    enabled: bool = False
    user_header: str = "Remote-User"
    shared_secret_header: str = "X-Proxy-Auth"
    trusted_ips: list[str] = Field(default_factory=list)
    shared_secret: str | None = None  # HMAC key the proxy signs X-Proxy-Auth with
    hmac_window_seconds: int = Field(default=60, ge=0)  # clock skew / replay window


class AuthConfig(BaseModel):
    """v0.2 auth foundation. Parsed (and ignored) since v0.1; enforced when enabled."""

    enabled: bool = False
    password_required: bool = False
    password_hash: str | None = None  # argon2id hash; see `clauster hash-password`
    reverse_proxy: ReverseProxyConfig = Field(default_factory=ReverseProxyConfig)
    allow_unauthenticated_network: bool = False
    # auto = Secure only over https (or trusted-proxy X-Forwarded-Proto=https)
    cookie_secure: Literal["auto", "always", "never"] = "auto"
    session_max_age_seconds: int = Field(default=604800, ge=1)  # 7 days
    allowed_origins: list[str] = Field(
        default_factory=list
    )  # extra WS/CSRF origins (proxy domain)


class CloneConfig(BaseModel):
    """Project clone/create guards (spec §11 clone+trust chain). Clone URLs are
    user-supplied and hit the network from the host, so the defaults are strict."""

    enabled: bool = True
    allowed_schemes: list[str] = Field(default_factory=lambda: ["https", "ssh"])
    allow_private_hosts: bool = False  # block private/LAN IPs by default (SSRF)
    allowed_private_cidrs: list[str] = Field(default_factory=list)  # targeted LAN opt-in
    timeout_seconds: int = Field(default=300, ge=1)
    max_mb: int = Field(default=2048, ge=0)  # post-clone size cap; 0 = unlimited

    @field_validator("allowed_private_cidrs")
    @classmethod
    def _validate_cidrs(cls, v: list[str]) -> list[str]:
        # Fail fast on a malformed CIDR rather than letting it silently never match
        # (this is an SSRF allowlist — a quiet no-op entry is a footgun).
        for cidr in v:
            ipaddress.ip_network(cidr, strict=False)
        return v


class ReaperConfig(BaseModel):
    """Ghost-environment reaper (spec §11). The CLI (`clauster reap-environments`)
    is always available; this gates only the *dashboard* surface, which exposes a
    destructive first-party API in the browser. Off by default — opt in explicitly."""

    ui_enabled: bool = False


class LogsConfig(BaseModel):
    bridge_log_max_size_mb: int = Field(default=10, ge=1)
    keep_rotated: int = Field(default=5, ge=0)
    redact_session_url: bool = False  # false=hybrid (verbatim disk, redacted WS)
    strip_ansi_in_stream: bool = True


class ClausterConfig(BaseModel):
    schema_version: int = SCHEMA_VERSION
    projects_root: Path
    host: str = "127.0.0.1"
    port: int = Field(default=7621, ge=1, le=65535)
    state_dir: Path = Path("~/.clauster")
    root_path: str = ""
    log_format: Literal["text", "json"] = "text"

    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    instance_defaults: InstanceDefaults = Field(default_factory=InstanceDefaults)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    logs: LogsConfig = Field(default_factory=LogsConfig)
    clone: CloneConfig = Field(default_factory=CloneConfig)
    reaper: ReaperConfig = Field(default_factory=ReaperConfig)

    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    def allows_bypass(self, project_name: str) -> bool:
        """Whether the config hard-ceiling permits bypassPermissions for this project."""
        pc = self.projects.get(project_name)
        return bool(pc and pc.allow_bypass_permissions)

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
        # Non-loopback bind is only allowed once authentication can gate it.
        if self.host not in _LOOPBACK_HOSTS:
            a = self.auth
            if not (
                a.password_required or a.reverse_proxy.enabled or a.allow_unauthenticated_network
            ):
                raise ValueError(
                    f"refusing non-loopback host={self.host!r} without auth. Set one of "
                    "auth.password_required, auth.reverse_proxy.enabled, or (to opt out on a "
                    "trusted LAN) auth.allow_unauthenticated_network."
                )
        # Fail closed: password auth required but no hash configured would lock everyone out
        # (or, worse, be skipped) — refuse to start with a clear message.
        if self.auth.password_required and not self.auth.password_hash:
            raise ValueError(
                "auth.password_required is set but auth.password_hash is empty. "
                "Generate one with `clauster hash-password` (or set CLAUSTER_AUTH_PASSWORD_HASH)."
            )
        return self


ConfigPath = Annotated[Path, "resolved path the config was loaded from, or None"]


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


def _apply_env_overrides(data: dict) -> dict:
    for env_name, path in _scalar_env_map(ClausterConfig).items():
        if env_name in os.environ:
            _set_nested(data, path, os.environ[env_name])
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

    raw = yaml.safe_load(found.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping, got {type(raw).__name__}: {found}")

    raw = _apply_env_overrides(raw)
    config = ClausterConfig.model_validate(raw)
    config._source_path = found
    return config
