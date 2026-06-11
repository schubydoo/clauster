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
from pathlib import Path

from .models import BackgroundJob
from .procutil import jiffies_to_epoch, proc_create_time
from .redact import redact_for_disk

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
