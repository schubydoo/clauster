"""`claude agents --json` cross-check (spec §7, observability source #2).

Secondary, ~5-min cadence. The JSON is a flat list of working sessions with no
bridge/env grouping (Capture B), so attribution joins on ``cwd`` — the only link
back to a managed bridge. ``sessionId`` here is the local RFC-4122 UUID, never
the API ULID.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .claude_cli import resolve_binary
from .models import Attribution, WorkingSession


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
        check=True,
    )
    return parse_agents_json(proc.stdout)


def parse_agents_json(stdout: str) -> list[WorkingSession]:
    """Parse the JSON array; tolerate empty output and skip malformed items."""
    text = stdout.strip()
    if not text:
        return []
    data = json.loads(text)
    items = data if isinstance(data, list) else data.get("agents", data.get("sessions", []))
    sessions: list[WorkingSession] = []
    for item in items:
        try:
            sessions.append(WorkingSession.from_agents_json(item))
        except (KeyError, TypeError, ValueError):
            continue
    return sessions


def reconcile(
    sessions: list[WorkingSession], managed_cwds: dict[Path, str]
) -> list[WorkingSession]:
    """Attribute each working session to a managed bridge by resolved cwd.

    ``managed_cwds`` maps a resolved project path → instance id. Sessions whose
    cwd matches become TRACKED (and carry ``parent_instance``); the rest are
    EXTERNAL (a bridge/session Clauster doesn't manage).
    """
    resolved = {p.resolve(): inst_id for p, inst_id in managed_cwds.items()}
    for s in sessions:
        inst_id = resolved.get(s.cwd.resolve())
        if inst_id is not None:
            s.parent_instance = inst_id
            s.attribution = Attribution.TRACKED
        else:
            s.attribution = Attribution.EXTERNAL
    return sessions
