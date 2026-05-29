"""Configuration loading for Clauster.

Search order:  $CLAUSTER_CONFIG  ->  ./clauster.yml  ->  $CLAUSTER_HOME/clauster.yml
Any scalar key is overridable via env: CLAUSTER_<UPPER_SNAKE_CASE_PATH>=value.
Schema is additive-only: old configs must always validate against newer versions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

SCHEMA_VERSION = 1
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ClaudeConfig(BaseModel):
    binary: str = "claude"
    min_version: str = "2.1.145"
    agents_json_poll_interval_seconds: int = 300


class InstanceDefaults(BaseModel):
    spawn_mode: Literal["same-dir"] = "same-dir"  # v0.2 adds worktree/session
    capacity: int = 32


class ReverseProxyConfig(BaseModel):
    enabled: bool = False
    user_header: str = "Remote-User"
    shared_secret_header: str = "X-Proxy-Auth"
    trusted_ips: list[str] = Field(default_factory=list)


class AuthConfig(BaseModel):
    """v0.2+ only; v0.1 ignores these entirely but still parses them."""

    enabled: bool = False
    password_required: bool = False
    reverse_proxy: ReverseProxyConfig = Field(default_factory=ReverseProxyConfig)
    allow_unauthenticated_network: bool = False


class LogsConfig(BaseModel):
    bridge_log_max_size_mb: int = 10
    keep_rotated: int = 5
    redact_session_url: bool = False  # false=hybrid (verbatim disk, redacted WS)
    strip_ansi_in_stream: bool = True


class ClausterConfig(BaseModel):
    schema_version: int = SCHEMA_VERSION
    projects_root: Path
    host: str = "127.0.0.1"
    port: int = 7621
    state_dir: Path = Path("~/.clauster")
    root_path: str = ""
    log_format: Literal["text", "json"] = "text"

    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    instance_defaults: InstanceDefaults = Field(default_factory=InstanceDefaults)
    projects: dict[str, dict] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    logs: LogsConfig = Field(default_factory=LogsConfig)

    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

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
    def _v01_loopback_only(self) -> ClausterConfig:
        if self.host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"v0.1 binds loopback only; refusing host={self.host!r}. "
                "Non-loopback bind with auth lands in v0.2."
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


def _scalar_env_map(model: type[BaseModel], prefix: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
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
