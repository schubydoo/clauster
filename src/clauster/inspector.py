"""`claude agents --json` cross-check (spec §7, observability source #2).

Secondary, ~5-min cadence. The JSON is a flat list of working sessions with no
bridge/env grouping (Capture B), so attribution joins on ``cwd`` — the only link
back to a managed bridge. ``sessionId`` here is the local RFC-4122 UUID, never
the API ULID.

Agent view (Claude Code 2.1.139+) lists `claude --bg` background sessions in the
same output, tagged ``kind: "background"`` and carrying a lifecycle ``state``.
The cwd join is therefore gated on ``kind`` (a background session in a managed
project's dir is not the bridge's session) and terminal-state entries are
dropped at parse (a finished session is not a working session).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import procutil
from .claude_cli import resolve_binary
from .models import Attribution, WorkingSession

# Agent-view lifecycle states that mean the session is over — not live anywhere,
# neither for bridge attribution nor for the ghost-reaper's keep set.
_TERMINAL_STATES = frozenset({"done", "failed", "stopped"})
# Kinds eligible for the cwd→bridge join. Bridge child sessions are observed
# "interactive"; "" tolerates a pre-agent-view CLI that omits the field. Anything
# else ("background", future kinds) is allowlisted out — fail-closed attribution.
_BRIDGE_KINDS = frozenset({"", "interactive"})


def list_working_sessions(binary: str, *, timeout: float = 10.0) -> list[WorkingSession]:
    """Invoke ``claude agents --json`` and parse the working-session list.

    Blocking — callers run it via ``asyncio.to_thread``.
    """
    resolved = resolve_binary(binary)
    proc = subprocess.run(
        [resolved, "agents", "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=procutil.child_env(),
        check=True,
    )
    return parse_agents_json(proc.stdout)


def parse_agents_json(stdout: str) -> list[WorkingSession]:
    """Parse the JSON array; tolerate empty output / unexpected shape, skip malformed items."""
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except RecursionError as exc:
        # Deeply-nested JSON overflows CPython's recursive scanner before json can
        # raise JSONDecodeError; surface it as the same strict "unparseable" failure
        # so callers already handling JSONDecodeError (e.g. the runner's best-effort
        # cross-check, runner.py) degrade uniformly instead of a stray RecursionError.
        raise json.JSONDecodeError("Exceeded maximum recursion depth", text, 0) from exc
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("agents", data.get("sessions"))
    else:
        items = None
    if not isinstance(items, list):
        # Valid JSON but an unexpected shape — a top-level scalar/None, or a dict
        # whose agents/sessions value isn't a list ({"agents": null}, {"agents": 5}).
        # Return empty rather than raising AttributeError on ``.get`` or TypeError on
        # iteration. (A genuinely malformed payload still fails at ``json.loads`` above,
        # which stays strict by design — fail-closed liveness.)
        return []
    sessions: list[WorkingSession] = []
    for item in items:
        try:
            session = WorkingSession.from_agents_json(item)
        except (KeyError, TypeError, ValueError):
            continue
        if session.state in _TERMINAL_STATES:
            continue
        sessions.append(session)
    return sessions


def reconcile(
    sessions: list[WorkingSession], managed_cwds: dict[Path, str]
) -> list[WorkingSession]:
    """Attribute each working session to a managed bridge by resolved cwd.

    ``managed_cwds`` maps a resolved project path → instance id. Sessions whose
    cwd matches become TRACKED (and carry ``parent_instance``); the rest are
    EXTERNAL (a bridge/session Clauster doesn't manage). Non-bridge kinds never
    join: a `claude --bg` session sharing a managed cwd must not read as the
    bridge's session (TRACKED = false liveness) nor as an unmanaged bridge
    (EXTERNAL phantom-deletes a stopped record) — it stays UNTRACKED.
    """
    resolved = {p.resolve(): inst_id for p, inst_id in managed_cwds.items()}
    for s in sessions:
        if s.kind not in _BRIDGE_KINDS:
            s.attribution = Attribution.UNTRACKED
            continue
        inst_id = resolved.get(s.cwd.resolve())
        if inst_id is not None:
            s.parent_instance = inst_id
            s.attribution = Attribution.TRACKED
        else:
            s.attribution = Attribution.EXTERNAL
    return sessions
