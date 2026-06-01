"""Domain models.

Two ID namespaces (see spec §8):
  - API namespace:   session_<ULID>, cse_<ULID>, env_<ULID>  (bridge log, pointer, URL)
  - Local UUID:      RFC 4122  (claude agents --json `sessionId`, JSONL transcript filenames)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, computed_field

from .config import PermissionMode, SpawnMode


class InstanceStatus(StrEnum):
    """Lifecycle state of a managed bridge, as surfaced to the dashboard."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    CRASHED = "crashed"
    ERROR = "error"


class TrustState(StrEnum):
    """Whether a project directory has accepted Claude's workspace-trust dialog."""

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class Project(BaseModel):
    """A discovered directory under projects_root (feature 1)."""

    name: str
    path: Path
    is_git_repo: bool = False
    has_claude_md: bool = False
    has_claude_dir: bool = False  # presence of .claude/ — warns "code runs on Start" after a clone
    trust_state: TrustState = TrustState.UNTRUSTED
    # Config hard-ceiling for the bypassPermissions footgun gate; surfaced so the
    # dashboard can decide whether to even offer the option. Set at the app layer
    # from config (discovery has no config knowledge).
    allow_bypass_permissions: bool = False


class RemoteControlInstance(BaseModel):
    """A bridge Clauster manages — one per project (spec §8)."""

    project: str
    label: str  # passed as --name to claude remote-control
    bridge_pid: int | None = None  # bridge parent PID (matches bridge-pointer pid)
    bridge_proc_start: float | None = None  # psutil create-time (epoch); PID-reuse defense
    bridge_id: str | None = None  # UUID from bridge:init log
    environment_id: str | None = None  # env_<ULID>; the URL parameter
    starter_session_id: str | None = None  # session_<ULID> from "Created initial session"
    url: str | None = None  # https://claude.ai/code?environment=env_<ULID>
    spawn_mode: SpawnMode = "same-dir"
    permission_mode: PermissionMode = "default"
    status: InstanceStatus = InstanceStatus.STARTING
    intentional_stop: bool = False
    started_at: datetime | None = None
    bridge_debug_log_path: Path | None = None
    # Tail of the bridge's stdout/stderr, captured when a spawn fails (ERROR/CRASHED)
    # so the UI can show *why* instead of a bare "Failed to start". None on success.
    error_detail: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def session_url(self) -> str | None:
        """Primary "Open session in Claude" deep link.

        Lands directly in the ready starter session named after the project
        (feature 5). ``url`` is the secondary "New session" composer link.
        """
        if self.starter_session_id is None:
            return None
        return f"https://claude.ai/code/{self.starter_session_id}?from=cli"


class ClaudeMdDoc(BaseModel):
    """A project's root CLAUDE.md as seen by the viewer/editor (spec §5)."""

    exists: bool
    content: str
    sha256: str | None = None  # of the on-disk content; None when the file is absent
    size: int = 0  # bytes (UTF-8)
    bridge_running: bool = False  # set at the app layer; drives the stale-bridge banner


class Attribution(StrEnum):
    """How a working session relates to a managed bridge (tracked/untracked/external)."""

    TRACKED = "tracked"
    UNTRACKED = "untracked"
    EXTERNAL = "external"


class WorkingSession(BaseModel):
    """A `claude agents --json` working session — observed, not managed.

    The raw list-item shape is {pid, cwd, kind, startedAt, sessionId}; the rest of
    the fields here are Clauster-derived.
    """

    pid: int
    cwd: Path  # the only link back to a bridge (join key)
    kind: str  # observed "interactive" for bridge child sessions
    started_at: int  # epoch ms (NOT an ISO string)
    local_uuid: str  # the JSON `sessionId` (RFC 4122), never the API ULID
    parent_instance: str | None = None  # derived by matching cwd to a managed bridge
    attribution: Attribution = Attribution.UNTRACKED

    @classmethod
    def from_agents_json(cls, item: dict) -> WorkingSession:
        """Build a ``WorkingSession`` from one raw ``claude agents --json`` item."""
        return cls(
            pid=item["pid"],
            cwd=Path(item["cwd"]),
            kind=item.get("kind", ""),
            started_at=item["startedAt"],
            local_uuid=item["sessionId"],
        )


class BridgePointer(BaseModel):
    """Shape of ~/.claude/projects/<sanitized-cwd>/bridge-pointer.json.

    Anthropic-controlled. Liveness must be validated via pid + procStart, since
    a dead bridge leaves a stale pointer behind.
    """

    session_id: str = Field(alias="sessionId")
    environment_id: str = Field(alias="environmentId")
    source: str
    pid: int
    proc_start: str = Field(alias="procStart")  # Linux jiffies, as a string

    model_config = {"populate_by_name": True}
