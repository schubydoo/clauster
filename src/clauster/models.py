"""Domain models.

Two ID namespaces (see spec §8):
  - API namespace:   session_<ULID>, cse_<ULID>, env_<ULID>  (bridge log, pointer, URL)
  - Local UUID:      RFC 4122  (claude agents --json `sessionId`, JSONL transcript filenames)

`RemoteControlInstance` spans both managed-session channels via its `channel`
field: "remote-control" (the bridge — its `resume_mode` and env/url fields) and
"hosted" (the claustrum stream-json session — its `claustrum_process_id`,
`agent_pid`, `claude_session_uuid`, … fields). Channel-specific fields are
nullable so a row of either channel validates.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, computed_field

from .config import PermissionMode, ResumeMode, SessionChannel, SpawnMode


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
    # How this bridge was launched. "pty" bridges run the flag form under a PTY
    # keeper for true conversation resume; their `bridge_pid` is the bridge the
    # keeper spawned and `keeper_pid` is the keeper holding its terminal.
    resume_mode: ResumeMode = "standard"
    keeper_pid: int | None = None  # PTY keeper holding the bridge's terminal ("pty" mode only)
    status: InstanceStatus = InstanceStatus.STARTING
    intentional_stop: bool = False
    started_at: datetime | None = None
    bridge_debug_log_path: Path | None = None
    # The private, verbatim parse-source the bridge actually writes its --debug-file to
    # when `logs.redact_session_url` is on; `bridge_debug_log_path` is then a redacted
    # at-rest mirror of it. When redaction is off this equals `bridge_debug_log_path`
    # (a single verbatim file). Readers parse markers from here; the mirror redacts it.
    bridge_raw_log_path: Path | None = None
    # Live read-only terminal capture for a "pty" bridge (#534): the PTY keeper mirrors
    # the drained master frames to this size-bounded file, which `/ws/pty-terminal` tails
    # (redacted in-flight). None for standard bridges (no PTY) and for any bridge whose
    # capture file no longer exists (retention pruned it).
    bridge_pty_log_path: Path | None = None
    # Tail of the bridge's stdout/stderr, captured when a spawn fails (ERROR/CRASHED)
    # so the UI can show *why* instead of a bare "Failed to start". None on success.
    error_detail: str | None = None

    # --- hosted channel (CL-4) ------------------------------------------------
    # Orthogonal axis to resume_mode: "hosted" sessions run a headless stream-json
    # `claude` on the claustrum daemon's pipes rather than a remote-control bridge.
    # All fields below are nullable/defaulted so existing remote-control state.json
    # rows load unchanged (additive-only schema). They are populated only when
    # channel == "hosted". CL-4b wired spawn dispatch + endpoints; state.json
    # persistence of these is CL-6 and the live-view UI is CL-4c.
    channel: SessionChannel = "remote-control"
    claustrum_process_id: str | None = None  # client-chosen ULID for the daemon spawn
    agent_pid: int | None = None  # the agent's OS pid (claustrum CT-1 opt-in; None pre-CT-1)
    # Clauster's own psutil create_time of agent_pid, measured at spawn (NOT the CT-1
    # daemon startTime token, which is daemon-internal). Backs CL-8 orphan validation:
    # a not-found reattach whose (pid, this) still matches a live process is a survivor.
    agent_proc_start: float | None = None
    claude_session_uuid: str | None = None  # RFC 4122 from the init frame; drives --resume
    daemon_last_seq: int = 0  # highest daemon frame seq seen; reattach cursor across restarts
    hosted_log_path: Path | None = None  # redacted on-disk mirror of the hosted stream
    is_orphan: bool = False  # CL-8: survived a daemon restart (live pid, no daemon session)

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
    """How a working session relates to a managed session (tracked/hosted/untracked/external)."""

    TRACKED = "tracked"
    HOSTED = "hosted"  # owned by Clauster's hosted (claustrum) registry, not a bridge (#592)
    UNTRACKED = "untracked"
    EXTERNAL = "external"


class WorkingSession(BaseModel):
    """A `claude agents --json` working session — observed, not managed.

    The raw list-item shape is {pid, cwd, kind, startedAt, sessionId}; agent view
    (Claude Code 2.1.139+) adds id/state/status/waitingFor/name and lists
    `claude --bg` background sessions alongside bridge children. The rest of the
    fields here are Clauster-derived.
    """

    pid: int
    cwd: Path  # the only link back to a bridge (join key)
    kind: str  # "interactive" for bridge child sessions; "background" = `claude --bg`
    state: str = ""  # agent-view lifecycle (working/blocked/done/failed/stopped); "" pre-2.1.139
    started_at: int  # epoch ms (NOT an ISO string)
    local_uuid: str  # the JSON `sessionId` (RFC 4122), never the API ULID
    parent_instance: str | None = None  # the owning managed bridge (cwd join) or hosted id (#592)
    attribution: Attribution = Attribution.UNTRACKED

    @classmethod
    def from_agents_json(cls, item: dict) -> WorkingSession:
        """Build a ``WorkingSession`` from one raw ``claude agents --json`` item."""
        return cls(
            pid=item["pid"],
            cwd=Path(item["cwd"]),
            kind=item.get("kind", ""),
            state=item.get("state", ""),
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


class BackgroundJob(BaseModel):
    """One agent-view background session (`claude --bg`), observed — not managed.

    Sourced from ``~/.claude/jobs/<id>/state.json`` joined with
    ``~/.claude/daemon/roster.json`` — the supervisor's on-disk surface, listed
    in the agent-view docs (Claude Code 2.1.139+, research preview). The shape
    is Anthropic-controlled; ``clauster.supervisor`` maps it tolerantly and
    redacts every free-text field before it gets here. ``worker_pid`` /
    ``worker_alive`` are Clauster-derived: a roster pid counts only after the
    pid+procStart liveness check (same posture as :class:`BridgePointer`).
    """

    id: str  # the supervisor's short id (jobs/<id>/ dir name), e.g. "3c509237"
    state: str = ""  # working/blocked/done/failed/stopped ("" if absent)
    detail: str = ""  # human-readable progress line, redacted
    tempo: str = ""  # active/blocked/idle ("" if absent)
    needs: str | None = None  # what the session is waiting on, redacted
    result: str | None = None  # output.result final text, redacted + truncated
    intent: str = ""  # the original dispatch prompt, redacted
    name: str | None = None  # display name (often supervisor auto-named), redacted
    cwd: Path | None = None
    session_id: str | None = None  # local RFC-4122 uuid — the `--resume` handle
    bridge_session_id: str | None = None  # cse_… when --rc registered the cloud door
    transcript_path: Path | None = None  # linkScanPath: the session's .jsonl
    cli_version: str = ""
    created_at: str = ""  # ISO-8601 strings, kept verbatim
    updated_at: str = ""
    worker_pid: int | None = None  # set only when worker_alive
    worker_alive: bool = False
