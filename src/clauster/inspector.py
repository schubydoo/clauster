"""`claude agents --json` cross-check (spec §7, observability source #2).

Secondary, ~5-min cadence. The JSON is a flat list of working sessions with no
bridge/env grouping (Capture B), so attribution joins on ``cwd`` — the only link
back to a managed bridge: an exact match to a bridge's cwd, or (for worktree-spawn
bridges, whose sessions run in per-session worktrees under the project) containment
in the ``.claude/worktrees`` subtree. ``sessionId`` here is the local RFC-4122 UUID,
never the API ULID.

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
# Where `claude remote-control --spawn worktree` places each session's git worktree,
# relative to the project root. A worktree bridge's sessions live in this subtree,
# never at the root, so containment attribution matches HERE specifically rather than
# the whole project tree — a stray interactive `claude` run by hand elsewhere under
# the project must not be claimed as the bridge's session.
_WORKTREE_SUBDIR = Path(".claude") / "worktrees"


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
    sessions: list[WorkingSession],
    managed_cwds: dict[Path, str],
    hosted_pids: dict[int, str] | None = None,
    hosted_cwds: dict[Path, str] | None = None,
    worktree_roots: dict[Path, str] | None = None,
) -> list[WorkingSession]:
    """Attribute each working session to a managed bridge by resolved cwd.

    ``managed_cwds`` maps a resolved project path → instance id. Sessions whose
    cwd matches become TRACKED (and carry ``parent_instance``); the rest are
    EXTERNAL (a bridge/session Clauster doesn't manage). Non-bridge kinds never
    join: a `claude --bg` session sharing a managed cwd must not read as the
    bridge's session (TRACKED = false liveness) nor as an unmanaged bridge
    (EXTERNAL phantom-deletes a stopped record) — it stays UNTRACKED.

    ``worktree_roots`` maps a *worktree-spawn* bridge's resolved project root →
    instance id. ``claude remote-control --spawn worktree`` runs each session in a
    per-session git worktree under ``<root>/.claude/worktrees/…``, so the session
    cwd never exactly matches the project-root key in ``managed_cwds`` and would
    wrongly read as EXTERNAL — hiding every session under a worktree bridge from the
    dashboard. Such a session is attributed by *containment* in that
    ``.claude/worktrees`` subtree (not the whole project, so a stray ``claude`` run
    by hand elsewhere under the project still reads EXTERNAL), most-specific root
    first so a nested project's bridge wins. Only worktree-spawn bridges opt in;
    same-dir/session bridges keep the exact-cwd join (their sessions share the bridge
    cwd).

    Clauster's own hosted (claustrum) sessions are spawned by it but run no bridge
    process, so they would otherwise fall through to EXTERNAL/unmanaged (#592).
    ``hosted_pids`` (agent pid → hosted id) and ``hosted_cwds`` (resolved cwd →
    hosted id) reclassify them as HOSTED: pid is the authoritative identity (a
    claustrum CT-1 ``agent_pid``) and is checked before the kind gate so a hosted
    session reads as HOSTED under any kind; cwd is the pre-CT-1 fallback (no pid to
    match) and joins only on a bridge kind, after the managed-bridge join so a real
    bridge at a shared cwd still wins.
    """
    resolved = {p.resolve(): inst_id for p, inst_id in managed_cwds.items()}
    # `is None` (not `or {}`): the contract is about an omitted arg, not an empty one,
    # and a fresh local avoids rebinding the parameter.
    hosted_by_pid = hosted_pids if hosted_pids is not None else {}
    hosted_by_cwd = {
        p.resolve(): hid for p, hid in (hosted_cwds if hosted_cwds is not None else {}).items()
    }
    # Match the `.claude/worktrees` subtree of each worktree-spawn root, most-specific
    # (deepest) first: a session under a nested worktree project must attribute to the
    # inner bridge, not an ancestor project that contains it.
    worktree_dirs = sorted(
        (
            ((p / _WORKTREE_SUBDIR).resolve(), inst_id)
            for p, inst_id in (worktree_roots if worktree_roots is not None else {}).items()
        ),
        key=lambda kv: len(kv[0].parts),
        reverse=True,
    )
    for s in sessions:
        hosted_id = hosted_by_pid.get(s.pid)
        if hosted_id is not None:
            s.parent_instance = hosted_id
            s.attribution = Attribution.HOSTED
            continue
        if s.kind not in _BRIDGE_KINDS:
            s.attribution = Attribution.UNTRACKED
            continue
        cwd = s.cwd.resolve()
        inst_id = resolved.get(cwd)
        if inst_id is not None:
            s.parent_instance = inst_id
            s.attribution = Attribution.TRACKED
            continue
        wt_id = next((iid for wt_dir, iid in worktree_dirs if cwd.is_relative_to(wt_dir)), None)
        if wt_id is not None:
            s.parent_instance = wt_id
            s.attribution = Attribution.TRACKED
            continue
        hosted_id = hosted_by_cwd.get(cwd)
        if hosted_id is not None:
            s.parent_instance = hosted_id
            s.attribution = Attribution.HOSTED
            continue
        s.attribution = Attribution.EXTERNAL
    return sessions
