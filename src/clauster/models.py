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

import re
import uuid
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import BaseModel, Field, computed_field

from .config import PermissionMode, ResumeMode, SandboxMode, SessionChannel, SpawnMode


def new_instance_id() -> str:
    """Generate a fresh RFC 4122 UUID for a new managed instance."""
    return str(uuid.uuid4())


# The shape a `claude_session_uuid` must have before it may become the `--resume` value
# token. `claude`'s `--resume [sessionId]` takes an *optional* value, so a flag-shaped
# token in that slot is read as a fresh FLAG rather than consumed as data — a persisted
# string would then contribute an ARGUMENT to the spawn argv, which invariant 2 forbids
# (#1392). The leading-alnum class is what closes that; the body class also rules out
# whitespace, path separators and control characters, and the 64-char cap matches the
# `String(64)` column the value round-trips through. `\A`/`\Z`, never `^`/`$`: `$` also
# matches *before* a trailing newline, so `$` would admit "uuid\n".
#
# Deliberately a SHAPE, not the 8-4-4-4-12 format `runner._SESSION_UUID_RE` and
# `supervisor.valid_session_id` demand — hence the different name. Those two guard an
# *operator-supplied* id that must name a transcript file on disk, so they can insist on
# claude's current filename format. This one guards an id claude *itself* minted and
# handed us in an init frame; re-specifying the format here would silently cost a whole
# session its resume the day claude changes it (a ULID, say). Keeping the token out of
# the flag namespace is the whole job. Not format-*independence*, though: the length cap
# is a narrower bet in the same direction, so an id longer than the column could hold
# would be refused at the argv seam too. Widen the cap with the column if that day comes.
#
# Lives here rather than in `hosted.py` (its executing consumer) so both the model — via
# the `claude_session_uuid_usable` computed field the dashboard gates on — and
# `build_hosted_argv` share ONE shape definition. `hosted.py` imports `models`, never the
# reverse, so the predicate cannot live there without a cycle.
_SESSION_UUID_SHAPE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def is_session_uuid(value: Any) -> TypeGuard[str]:
    """Report whether ``value`` is a str shaped like a session id.

    The shape, and why it is this one, is :data:`_SESSION_UUID_SHAPE_RE`. Public because
    two callers share it: :func:`clauster.hosted.build_hosted_argv`, the executing seam,
    and :attr:`RemoteControlInstance.claude_session_uuid_usable`, the display gate.
    """
    return isinstance(value, str) and _SESSION_UUID_SHAPE_RE.match(value) is not None


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
    """A bridge Clauster manages — keyed by instance_id in the runner registry (#777).

    Standard (server-mode) bridges are capped at one per project.  Interactive
    (pty) sessions may run any number per project (especially with ``--worktree``).
    The ``instance_id`` is a stable RFC 4122 UUID generated at spawn time and
    persisted so a restart can reconstruct the same key.
    """

    instance_id: str = Field(default_factory=new_instance_id)
    project: str
    label: str  # passed as --name to claude remote-control
    bridge_pid: int | None = None  # bridge parent PID (matches bridge-pointer pid)
    bridge_proc_start: float | None = None  # psutil create-time (epoch); PID-reuse defense
    # Boot-relative start ticks (/proc/<pid>/stat field 22), Linux-only (#1399). The epoch
    # above is re-derived from a btime that NTP moves, so on its own it declares a live
    # bridge dead after a clock correction; this one does not move. See
    # procutil.is_live_process.
    bridge_start_ticks: int | None = None
    # The boot this bridge started in (/proc/sys/kernel/random/boot_id), Linux-only (#1401).
    # Ticks restart at zero each boot, so a row from an earlier boot could name a recycled
    # pid holding the same count; a boot_id mismatch rejects that on identity, and being
    # boot-relative it survives a clock step the coarse epoch cannot, so is_live_process uses
    # it INSTEAD of the epoch. None on a pre-#1401 row (which then keeps the coarse epoch
    # fallback) and off Linux.
    bridge_boot_id: str | None = None
    bridge_id: str | None = None  # UUID from bridge:init log
    environment_id: str | None = None  # env_<ULID>; the URL parameter
    starter_session_id: str | None = None  # session_<ULID> from "Created initial session"
    url: str | None = None  # https://claude.ai/code?environment=env_<ULID>
    spawn_mode: SpawnMode = "same-dir"
    permission_mode: PermissionMode = "default"
    # Per-launch OS-level filesystem/network sandbox toggle for a standard bridge (#780).
    # "default" = neither flag (claude's off-by-default / sandbox.* settings win); "on" =
    # --sandbox; "off" = --no-sandbox. Recorded so a resume re-applies the same choice.
    # Always "default" for pty bridges (out of scope for #780).
    sandbox_mode: SandboxMode = "default"
    # How this bridge was launched. "pty" bridges (Interactive Session) run the flag
    # form under a PTY keeper for true conversation resume; their `bridge_pid` is the
    # bridge the keeper spawned and `keeper_pid` is the keeper holding its terminal.
    resume_mode: ResumeMode = "standard"
    keeper_pid: int | None = None  # PTY keeper holding the bridge's terminal ("pty" mode only)
    # The `--worktree <name>` a spawn_mode="worktree" pty session actually runs under, when
    # it is known EXPLICITLY rather than derived from the instance_id (#1241). Recovered
    # from the keeper sidecar on a reattach that had to mint a fresh id, and persisted so
    # the recovery survives the next restart too. None -> derive it (the normal case, where
    # the instance_id is the original one). See `SessionRunner._pty_worktree_name`.
    worktree_name: str | None = None
    # The keeper pid's psutil create-time, snapshotted when we spawned or classified it.
    # Same PID-reuse defense `bridge_proc_start` gives the bridge half (#1178): a cmdline
    # gate alone rules out a recycled pid running something else, but not a DIFFERENT live
    # keeper on that pid. Always set and cleared together with `keeper_pid` — a start-time
    # left over from a previous generation is worse than none, because it would report a
    # live keeper as gone. None means "unknown", which degrades to the cmdline-only gate.
    keeper_proc_start: float | None = None
    # Boot-relative start ticks for the keeper pid (/proc/<pid>/stat field 22), Linux-only
    # (#1402) — the keeper's half of what `bridge_start_ticks` gives the bridge. The epoch
    # above is re-derived from a btime NTP moves, so on a drifting host `forget`'s keeper
    # gate reads a live keeper as dead and drops the record of a running process. This one
    # does not move. Read WITH the epoch, in one `procutil.proc_start_pair` call, and set
    # and cleared with the rest of the keeper trio.
    keeper_start_ticks: int | None = None
    status: InstanceStatus = InstanceStatus.STARTING
    intentional_stop: bool = False
    started_at: datetime | None = None
    bridge_debug_log_path: Path | None = None
    # The private, verbatim parse-source the bridge actually writes its --debug-file to
    # when `logs.redact_session_url` is on; `bridge_debug_log_path` is then a redacted
    # at-rest mirror of it. When redaction is off this equals `bridge_debug_log_path`
    # (a single verbatim file). Readers parse markers from here; the mirror redacts it.
    bridge_raw_log_path: Path | None = None
    # Tail of the bridge's stdout/stderr, captured when a spawn fails (ERROR/CRASHED)
    # so the UI can show *why* instead of a bare "Failed to start". None on success.
    error_detail: str | None = None
    # A non-fatal advisory about a bridge that is otherwise HEALTHY (#1390) — currently the
    # pty keeper's "the screen could not be rendered, so no connect link was captured".
    # Deliberately NOT `error_detail`: the dashboard ties that field to the error/ended
    # states, so a note routed through it would either render nowhere on a running row or
    # make a working session read as failed. Set from the keeper sidecar's `note`.
    # Not persisted (`SessionRunner._persist_subset` builds its rows field-by-field), for the
    # same reason as `error_detail` — it describes this run of this bridge, and a reattach
    # re-reads the sidecar that is still on disk.
    notice: str | None = None
    # The `claude` release the bridge PROCESS is running (#1275) — read off the live process
    # tree by `procutil.running_claude_version` on every liveness poll, and cleared the tick
    # a bridge stops. Deliberately NOT persisted (`SessionRunner._persist_subset` builds its
    # rows field-by-field): a version is only true of a process that exists right now, so a
    # restart must re-observe it rather than revive a value from state.json. `None` whenever
    # it can't be resolved — the card then shows nothing rather than a stale guess.
    claude_version: str | None = None

    # --- hosted channel (CL-4) ------------------------------------------------
    # Orthogonal axis to resume_mode: "hosted" sessions (Direct Session) run a headless
    # stream-json `claude` on the claustrum daemon's pipes rather than a remote-control bridge.
    # All fields below are nullable/defaulted so existing remote-control state.json
    # rows load unchanged (additive-only schema). They are populated only when
    # channel == "hosted"; they are persisted in hosted_state.json (CL-6), not state.json.
    channel: SessionChannel = "remote-control"
    claustrum_process_id: str | None = None  # client-chosen ULID for the daemon spawn
    agent_pid: int | None = None  # the agent's OS pid (claustrum CT-1 opt-in; None pre-CT-1)
    # Clauster's own psutil create_time of agent_pid, measured at spawn (NOT the CT-1
    # daemon startTime token, which is daemon-internal). Backs CL-8 orphan validation:
    # a not-found reattach whose (pid, this) still matches a live process is a survivor.
    agent_proc_start: float | None = None
    # The drift-immune half of the same pair (#1404), the hosted twin of
    # `bridge_start_ticks`. `agent_proc_start` is psutil's create_time, which on Linux is
    # re-derived from /proc/stat btime on every read — btime moves with NTP, so the epoch
    # of an agent that never restarted wanders by seconds and the 0.05s compare reads a
    # survivor as lost. Ticks are measured from the boot instant and do not move. Neither
    # half suffices alone (ticks restart at zero each boot); see procutil.is_live_process.
    agent_start_ticks: int | None = None
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def claude_session_uuid_usable(self) -> bool:
        """Whether ``claude_session_uuid`` is shaped like a usable ``--resume`` token.

        One shape-checked signal for the dashboard's Resume gate, so a live-captured OR
        persisted off-shape uuid is refused Resume consistently — before AND after a
        restart. The bytes are kept on the record for an operator to repair by hand
        (:func:`clauster.hosted._as_session_uuid` keeps a non-empty value regardless of
        shape); this reports whether they are *usable*, which the executing seam
        (:func:`clauster.hosted.build_hosted_argv`) enforces with the same
        :func:`is_session_uuid` predicate.
        """
        return is_session_uuid(self.claude_session_uuid)


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
    # owned by Clauster's Direct Session (claustrum) registry, not a bridge (#592)
    HOSTED = "hosted"
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
