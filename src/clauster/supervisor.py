"""Read-only view of the agent-view supervisor's background sessions (bg-agents panel).

Claude Code's agent view (2.1.139+, research preview) hosts ``claude --bg``
background sessions under a per-user supervisor daemon. Its on-disk surface is
listed on the agent-view docs page — an acknowledged interface, not
reverse-engineering:

* ``~/.claude/jobs/<id>/state.json`` — per-session state (what agent view renders)
* ``~/.claude/daemon/roster.json``   — live workers (pid + procStart), kept by the
  supervisor for its own reconnect-after-restart

This slice only OBSERVES (list for the dashboard); dispatch/stop are later
slices. Conventions applied on the way in:

* **Tolerant per job, never silent** — one malformed ``state.json`` skips that
  job with a warning instead of failing the listing (or being swallowed).
* **Redact before display** — free-text fields (detail/result/intent/name/needs)
  pass the same redaction as bridge logs; the supervisor writes them verbatim.
  Structured identifiers (``sessionId``/``bridgeSessionId``) pass through like
  the equivalent fields on ``/api/instances`` — the API is auth-gated and they
  are this job's own resume/cloud handles, not foreign secrets.
* **PID-reuse defense** — a roster worker counts as alive only when the pid's
  create-time matches the roster's ``procStart`` (jiffies), the same posture as
  :class:`~clauster.models.BridgePointer` liveness. Fail-closed: an unparsable
  ``procStart`` reads as not-alive rather than trusting a bare pid.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from .claude_cli import resolve_binary
from .models import BackgroundJob
from .procutil import jiffies_to_epoch, proc_create_time
from .redact import redact_for_disk
from .trust import trust_directory

JOBS_DIR = Path("~/.claude/jobs").expanduser()
ROSTER_JSON = Path("~/.claude/daemon/roster.json").expanduser()

# Start-time slack for the jiffies→epoch comparison — same bound as
# procutil.is_live_bridge uses for a pointer's independently-derived jiffies.
_PROC_START_TOLERANCE = 2.0

# Cap on the redacted output.result text returned to the API/UI; the full text
# stays in the session transcript (transcript_path) — nothing is lost.
_RESULT_MAX_CHARS = 4000

_log = logging.getLogger("clauster.supervisor")


def _clean(value: object, *, limit: int | None = None) -> str:
    """Coerce a free-text field to a redacted ``str`` (``""`` for non-strings)."""
    if not isinstance(value, str):
        return ""
    text = redact_for_disk(value)
    if limit is not None and len(text) > limit:
        text = text[:limit] + " …<truncated>"
    return text


def _opt_str(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _opt_path(value: object) -> Path | None:
    """Coerce an expected path field to ``Path`` (None for non-strings/empty)."""
    return Path(value) if isinstance(value, str) and value else None


def load_roster_workers(roster_json: Path | None = None) -> dict[str, dict]:
    """Return the roster's ``workers`` map (short id → entry), ``{}`` when absent.

    The roster only exists while the supervisor runs, so a missing file is the
    normal idle state — only an unreadable or malformed file warns. ``None``
    resolves to :data:`ROSTER_JSON` at call time (late-bound so tests can
    monkeypatch the module constant).
    """
    if roster_json is None:
        roster_json = ROSTER_JSON
    try:
        raw = roster_json.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _log.warning("could not read %s: %s", roster_json, exc)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("malformed roster %s: %s", roster_json, exc)
        return {}
    workers = data.get("workers") if isinstance(data, dict) else None
    return workers if isinstance(workers, dict) else {}


def worker_alive(
    pid: object, proc_start: object, *, tolerance: float = _PROC_START_TOLERANCE
) -> bool:
    """Whether a roster worker's pid is alive AND matches its recorded start time.

    Fail-closed: a recycled pid (create-time mismatch), a dead/zombie pid, or an
    unparsable ``procStart`` all read as not-alive — never trust a bare pid.
    (Unlike ``procutil.is_live_bridge`` there is no cmdline shape to fall back
    on for these workers, so the start-time match is the only trust signal.)
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    created = proc_create_time(pid)
    if created is None:
        return False
    try:
        jiffies = int(str(proc_start))
    except (TypeError, ValueError):
        return False
    expected = jiffies_to_epoch(jiffies)
    if expected is None:
        return False
    return abs(created - expected) <= tolerance


def _job_from_state(job_id: str, data: dict, workers: dict[str, dict]) -> BackgroundJob:
    """Map one parsed ``state.json`` object to a :class:`BackgroundJob`."""
    output = data.get("output")
    result = output.get("result") if isinstance(output, dict) else None
    worker = workers.get(job_id)
    pid = worker.get("pid") if isinstance(worker, dict) else None
    alive = worker_alive(pid, worker.get("procStart")) if isinstance(worker, dict) else False
    return BackgroundJob(
        id=job_id,
        state=_clean(data.get("state")),
        detail=_clean(data.get("detail")),
        tempo=_clean(data.get("tempo")),
        needs=_clean(data.get("needs")) or None,
        result=_clean(result, limit=_RESULT_MAX_CHARS) or None,
        intent=_clean(data.get("intent")),
        name=_clean(data.get("name")) or None,
        cwd=_opt_path(data.get("cwd")),
        session_id=_opt_str(data.get("sessionId")),
        bridge_session_id=_opt_str(data.get("bridgeSessionId")),
        transcript_path=_opt_path(data.get("linkScanPath")),
        cli_version=_clean(data.get("cliVersion")),
        created_at=_clean(data.get("createdAt")),
        updated_at=_clean(data.get("updatedAt")),
        worker_pid=pid if alive and isinstance(pid, int) else None,
        worker_alive=alive,
    )


def list_background_jobs(
    jobs_dir: Path | None = None, roster_json: Path | None = None
) -> list[BackgroundJob]:
    """List agent-view background sessions from the supervisor's on-disk state.

    Newest-updated first. A missing jobs dir means the feature is unused (agent
    view is a research preview — absence is the common case) and returns ``[]``.
    ``None`` args resolve to the module constants at call time (late-bound so
    tests can monkeypatch :data:`JOBS_DIR` / :data:`ROSTER_JSON`).
    """
    if jobs_dir is None:
        jobs_dir = JOBS_DIR
    try:
        entries = sorted(path for path in jobs_dir.iterdir() if path.is_dir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        _log.warning("could not list %s: %s", jobs_dir, exc)
        return []
    workers = load_roster_workers(roster_json)
    jobs: list[BackgroundJob] = []
    for entry in entries:
        state_file = entry / "state.json"
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # half-created or just-reaped job dir — not an error
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("skipping background job %s: unreadable state.json (%s)", entry.name, exc)
            continue
        if not isinstance(data, dict):
            _log.warning("skipping background job %s: state.json is not an object", entry.name)
            continue
        jobs.append(_job_from_state(entry.name, data, workers))
    jobs.sort(key=lambda job: (job.updated_at, job.id), reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# Dispatch (BG-2) — fire a `claude --bg` background session.
# ---------------------------------------------------------------------------

# Generous wall-clock budget: the dispatcher backgrounds the worker and returns
# in seconds, but it may internally retry once on an `ack-timeout` (observed),
# so the bound leaves room for that without hanging the request forever.
_DISPATCH_TIMEOUT = 60.0

# `claude --bg` prints `backgrounded · <id>` on success; <id> is the first eight
# hex chars of the session UUID. The labelled form is authoritative; a bare
# 8-hex token is a fallback so a future banner reword still yields the id.
_DISPATCH_ID_RE = re.compile(r"backgrounded[^\n0-9a-f]*([0-9a-f]{8})\b")
_BARE_ID_RE = re.compile(r"\b[0-9a-f]{8}\b")


class DispatchError(RuntimeError):
    """A ``claude --bg`` dispatch failed (bad exit, timeout, or no job id printed)."""


def parse_job_id(stdout: str) -> str | None:
    """Extract the dispatched 8-hex job id from ``claude --bg`` stdout.

    The success banner is ``backgrounded · <id>``; that labelled form wins. A
    bare 8-hex token is accepted as a fallback. Returns None when no id-shaped
    token is present (so the caller can fail closed rather than invent an id).
    """
    labelled = _DISPATCH_ID_RE.search(stdout)
    if labelled is not None:
        return labelled.group(1)
    bare = _BARE_ID_RE.search(stdout)
    return bare.group(0) if bare is not None else None


def _flag_value_ok(value: object) -> bool:
    """Whether a flag value is a safe argv item: a non-empty str with no leading dash.

    Total over its input type (the body is arbitrary JSON) — a non-string reads as
    not-ok rather than raising, so the caller fails closed with a clear error.
    """
    return isinstance(value, str) and bool(value) and not value.startswith("-")


def build_dispatch_argv(
    binary: str,
    *,
    rc_name: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    prompt: str | None = None,
) -> list[str]:
    """Build the ``claude --bg [...]`` argv. Pure (no side effects) — unit-testable.

    ``rc_name`` opens the cloud door (``--rc`` → a cloud-visible Remote Control
    session); omit it for a purely local background job. ``prompt`` is the
    trailing positional (the session's initial instruction) and may be omitted.
    """
    argv = [binary, "--bg"]
    if rc_name:
        argv += ["--rc", rc_name]
    if model:
        argv += ["--model", model]
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    if prompt:
        # `--` end-of-options separator (verified honored by `claude --bg`): a
        # prompt that starts with a dash is then a positional, never parsed as a
        # flag — so the prompt can't smuggle an argv flag past the flag guard.
        argv += ["--", prompt]
    return argv


def dispatch_background_job(
    cwd: Path,
    *,
    prompt: str | None = None,
    rc_name: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    binary: str = "claude",
    claude_json: Path | None = None,
    timeout: float = _DISPATCH_TIMEOUT,
) -> str:
    """Dispatch a ``claude --bg`` background session in ``cwd``; return its job id.

    Fire-and-return: the CLI backgrounds the worker, prints ``backgrounded ·
    <id>``, and exits; the bg-agents panel (``GET /api/agents``) reflects the
    session's live state thereafter. Invariants:

    * **Validate before spawning** — ``cwd`` must be an existing directory, the
      binary resolves to an absolute path (``ClaudeNotFound`` otherwise), argv is
      a list (never ``shell=True``), and flag-valued options are rejected when
      they could be mistaken for an argv flag.
    * **Pre-trust** — the detached dispatcher cannot answer the one-time trust
      dialog (it wedges on "stuck on a startup dialog"), so the cwd is trusted
      first via the same writer the bridge spawn uses.
    * **Fail closed, never silently** — a non-zero exit, a timeout, or output
      with no job id raises :class:`DispatchError` (stderr redacted) rather than
      returning a bogus or empty id.

    ``rc_name`` opens the cloud door. NOTE: a consent-gated ``permission_mode``
    (auto / acceptEdits / bypassPermissions) is passed through, but the cloud
    surface may still re-gate mode entry — the panel surfaces that as the job's
    ``needs``; clauster does not assume the mode is live cloud-side.
    """
    cwd = Path(cwd)
    if not cwd.is_dir():
        raise DispatchError(f"dispatch cwd is not a directory: {cwd}")
    if prompt is not None and not isinstance(prompt, str):
        raise DispatchError(f"invalid prompt: {prompt!r}")
    flag_values = (("rc name", rc_name), ("model", model), ("permission mode", permission_mode))
    for label, value in flag_values:
        if value is not None and not _flag_value_ok(value):
            raise DispatchError(f"invalid {label}: {value!r}")
    resolved = resolve_binary(binary)  # absolute path, or ClaudeNotFound
    if claude_json is None:
        trust_directory(cwd)
    else:
        trust_directory(cwd, claude_json)
    argv = build_dispatch_argv(
        resolved, rc_name=rc_name, model=model, permission_mode=permission_mode, prompt=prompt
    )
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(f"`claude --bg` timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        detail = _clean(proc.stderr) or _clean(proc.stdout) or f"exit {proc.returncode}"
        raise DispatchError(f"`claude --bg` failed (exit {proc.returncode}): {detail}")
    job_id = parse_job_id(proc.stdout or "")
    if job_id is None:
        raise DispatchError("`claude --bg` exited 0 but printed no job id")
    return job_id
