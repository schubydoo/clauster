"""Domain models.

Two ID namespaces (see spec §8):
  - API namespace:   session_<ULID>, cse_<ULID>, env_<ULID>  (bridge log, pointer, URL)
  - Local UUID:      RFC 4122  (claude agents --json `sessionId`, JSONL transcript filenames)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class InstanceStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    CRASHED = "crashed"
    ERROR = "error"


class TrustState(str, Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class Project(BaseModel):
    """A discovered directory under projects_root (feature 1)."""

    name: str
    path: Path
    is_git_repo: bool = False
    has_claude_md: bool = False
    trust_state: TrustState = TrustState.UNTRUSTED


class RemoteControlInstance(BaseModel):
    """A bridge Clauster manages — one per project (spec §8)."""

    project: str
    label: str  # passed as --name to claude remote-control
    bridge_pid: int | None = None  # parent PID from subprocess
    bridge_proc_start: int | None = None  # psutil create-time; PID-reuse defense
    bridge_id: str | None = None  # UUID from bridge:init log
    environment_id: str | None = None  # env_<ULID>; the URL parameter
    starter_session_id: str | None = None  # session_<ULID> from "Created initial session"
    url: str | None = None  # https://claude.ai/code?environment=env_<ULID>
    spawn_mode: str = "same-dir"
    status: InstanceStatus = InstanceStatus.STARTING
    intentional_stop: bool = False
    started_at: datetime | None = None
    bridge_debug_log_path: Path | None = None


class Attribution(str, Enum):
    TRACKED = "tracked"
    UNTRACKED = "untracked"
    EXTERNAL = "external"


class WorkingSession(BaseModel):
    """Observed, not managed. Exact shape of a `claude agents --json` list item
    is {pid, cwd, kind, startedAt, sessionId}; the rest is Clauster-derived.
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
