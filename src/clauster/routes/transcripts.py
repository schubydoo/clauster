"""Read-only transcript viewer routes, split from ``create_app`` (#1156).

Claude owns these ``.jsonl`` transcripts; every route here reads them and never
writes (invariant 5). Redaction happens inside the :mod:`clauster.usage` reader
before any turn leaves the process (invariant 4).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException

from .. import usage
from ..dependencies import ConfigDep, HostedDep, RunnerDep
from ..discovery import is_valid_project_name
from ..models import InstanceStatus

if TYPE_CHECKING:
    from ..hosted import HostedManager
    from ..runner import SessionRunner

logger = logging.getLogger(__name__)

router = APIRouter()


def _live_session_uuids(
    runner: SessionRunner, hosted: HostedManager, project_path: Path, name: str
) -> set[str]:
    """Session ids of currently-running sessions writing into this project's dir.

    Bridge/agent sessions come from the runner's reconcile snapshot (keyed by
    sanitized cwd); hosted (claustrum) sessions run no ``agents --json`` session,
    so their captured session uuid is folded in by project name, status-filtered
    to RUNNING/STARTING (``_instances`` is not pruned on session end). The hosted
    half is a pure in-memory read; the runner half also does a little ``Path.resolve``
    work on disk, each call of which is OSError-guarded — so this liveness join can't
    fail a transcript listing or tail; at worst a just-started/just-stopped session
    flips a poll late. Shared by the list (#614 Part 1) and tail (#614 Part 2)
    routes so they agree on exactly which sessions are live.
    """
    live_uuids = runner.live_session_uuids(project_path)
    live_uuids |= {
        inst.claude_session_uuid
        for inst in hosted.list_instances()
        if inst.project == name
        and inst.claude_session_uuid
        and inst.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING)
    }
    return live_uuids


@router.get("/api/projects/{name}/transcripts")
async def api_project_transcripts(
    name: str, config: ConfigDep, runner: RunnerDep, hosted: HostedDep
) -> dict:
    """List a project's session transcripts for the read-only viewer (issue #431, #614).

    Returns ``{project, sessions: [{session, mtime, turn_count, live, first_prompt,
    first_ts, last_ts, is_subagent}]}``,
    live-first then newest-first (by mtime). ``session`` is the transcript filename
    stem (the per-session uuid); ``live`` is True when that session id maps to a
    currently-running bridge/agent or hosted session (#614). ``is_subagent`` marks a
    sidechain (dispatched-subagent) transcript: the listing still carries it so the
    read-only viewer can show everything, and only the fork picker leaves it out
    (#1092). Mirrors
    :func:`api_project_usage`: the name is validated for path-component safety (422),
    the on-disk walk runs off the event loop, and a broken directory or unreadable
    file (``OSError``) degrades to a defined 503 — never a bare 500 and never echoing
    the on-disk path to the browser. We deliberately skip a discovery scan (an
    unknown-but-safe name simply has no transcripts and lists empty).

    The live set is computed before the off-thread walk, mostly from in-memory
    snapshots (the runner's ``agents --json`` reconcile join + the hosted registry);
    the runner half also does a little OSError-guarded path resolution, so the
    liveness cross-reference still can't fail the listing — at worst a
    just-started/just-stopped session badges a poll late.
    """
    if not is_valid_project_name(name):
        raise HTTPException(status_code=422, detail="invalid project name")

    project_path = config.projects_root / name
    live_uuids = _live_session_uuids(runner, hosted, project_path, name)

    def _list() -> list[dict]:
        """Build one summary entry per transcript file, skipping any that vanished.

        Only a file removed mid-walk is skipped; any other read error propagates and
        the caller turns it into a 503 for the whole listing.
        """
        out: list[dict] = []
        for path in usage.transcript_paths_for(project_path):
            try:
                mtime = path.stat().st_mtime
                # turn_count AND the resume-picker fields (#303) come from a cached
                # per-file summary (#1035): an unchanged transcript skips the full
                # re-parse, so reopening the selector is near-instant. Every derived
                # turn is redaction-safe (sanitize_line in _line_to_turn).
                summary = usage.read_transcript_summary(path)
            except FileNotFoundError:
                # A session removed mid-walk (racing cleanup) is skipped, not fatal.
                continue
            # Timestamps bound the conversation for the picker's "when · duration"
            # display ("" when the record has none); first_prompt labels it.
            out.append(
                {
                    "session": path.stem,
                    "mtime": mtime,
                    "turn_count": summary.turn_count,
                    "live": path.stem in live_uuids,
                    "first_prompt": summary.first_prompt,
                    "first_ts": summary.first_ts,
                    "last_ts": summary.last_ts,
                    "is_subagent": summary.is_subagent,
                }
            )
        # Live sessions first (a glance at what's running now), then newest-first
        # within each group so a stable, predictable order survives every poll.
        out.sort(key=lambda s: (not s["live"], -s["mtime"]))
        return out

    # Log the full error server-side (it can carry an absolute on-disk path) but
    # return only the static prefix so the path never leaks.
    try:
        sessions = await asyncio.to_thread(_list)
    except OSError as exc:
        logger.warning("transcript list failed for %r: %s", name, exc)
        raise HTTPException(status_code=503, detail="could not read transcripts") from exc
    return {"project": name, "sessions": sessions}


@router.get("/api/projects/{name}/transcripts/{session}")
async def api_project_transcript_session(
    name: str,
    session: str,
    config: ConfigDep,
    cursor: int = 0,
    limit: int = 50,
    order: str = "desc",
    q: str = "",
) -> dict:
    """Return one page of a session's turns, ordered + optionally filtered (#431, #612).

    Turns are ``{role, content, model, timestamp}`` already redacted by
    :func:`usage.read_transcript_turns` (every rendered field passes through
    ``redact.sanitize_line`` before it leaves the reader). ``cursor`` is an
    opaque offset into the ordered turn list and ``next_cursor`` is the offset to
    fetch the following page (``None`` at the end).

    ``order`` (``desc`` default, newest-first; ``asc`` flips to oldest-first) and
    the optional ``q=`` substring filter (#612) are applied *before* paging, so a
    toggle/search re-fetches from cursor 0 and pagination still terminates without
    double-rendering. The search matches against the **redacted** ``content`` (the
    text that already left the reader) so it can never be used to confirm a
    redacted secret, and it is case-insensitive. ``total`` reflects the count
    *after* filtering, so the modal's turn count and the load-more terminator
    track the visible set.

    BOTH ``name`` and ``session`` are validated path-safe: the name via the
    project-name regex (422), and ``session`` via
    :func:`usage.resolve_session_transcript`, which fails closed against ``..`` /
    separators and confirms the resolved file sits strictly inside the project's
    transcript dir (a bad/unknown session → 404, never a directory escape). An
    unreadable transcript (``OSError``) degrades to a defined 503 with no path in
    the body — never a bare 500.
    """
    if not is_valid_project_name(name):
        raise HTTPException(status_code=422, detail="invalid project name")
    # Clamp paging args defensively: a negative cursor/limit or an absurd limit
    # must not let a client over-read or index from the tail.
    cursor = max(cursor, 0)
    limit = max(1, min(limit, 500))
    # Normalize the sort: anything that isn't an explicit "asc" stays newest-first
    # (the historical default), so a typo'd order never silently reverses the view.
    ascending = order == "asc"
    # The filter matches the already-redacted content, case-insensitively. An empty
    # (or whitespace-only) term means "no filter" — the full ordered list pages.
    needle = q.strip().casefold()

    def _read() -> dict:
        """Resolve and read one session transcript, applying the filter and page order."""
        project_path = config.projects_root / name
        path = usage.resolve_session_transcript(project_path, session)
        if path is None:
            # Fail closed: unsafe or unknown session is a 404, never a path escape.
            raise HTTPException(status_code=404, detail="transcript not found")
        turns = usage.read_transcript_turns(path)
        if not ascending:
            turns.reverse()  # newest-first page order (default)
        if needle:
            # Match against the redacted content only — searching the text that
            # already left the reader means a query can't confirm a masked secret.
            turns = [t for t in turns if needle in (t.get("content") or "").casefold()]
        page = turns[cursor : cursor + limit]
        end = cursor + limit
        next_cursor = end if end < len(turns) else None
        return {
            "project": name,
            "session": session,
            "turns": page,
            "next_cursor": next_cursor,
            "total": len(turns),
        }

    try:
        return await asyncio.to_thread(_read)
    except HTTPException:
        # Re-raise the in-thread 404 unchanged (don't fold it into the 503 below).
        raise
    except (FileNotFoundError, OSError) as exc:
        logger.warning("transcript read failed for %r/%r: %s", name, session, exc)
        raise HTTPException(status_code=503, detail="could not read transcript") from exc


@router.get("/api/projects/{name}/transcripts/{session}/tail")
async def api_project_transcript_tail(
    name: str,
    session: str,
    config: ConfigDep,
    runner: RunnerDep,
    hosted: HostedDep,
    offset: int = 0,
) -> dict:
    """Return a live session's transcript turns appended since byte ``offset`` (#614 Part 2).

    The front-end opens a session, then — *only while it is live* — polls this
    endpoint on a timer to follow new turns as the agent works (the maintainer's
    decided mechanism: poll the ``.jsonl`` from a known byte offset, not the
    bridge-log WebSocket). Returns
    ``{project, session, turns, offset, reset, live}``:

    - ``turns`` — the renderable turns appended after ``offset``, in **file
      order** (oldest-first append order) so the client appends them to the
      bottom of the tail. Each is already redacted by
      :func:`usage.read_transcript_turns_from_offset` (shared
      ``redact.sanitize_line`` path) — never raw.
    - ``offset`` — the byte position to poll from next. It only advances past
      **complete** lines, so a half-written final record is reparsed next poll
      rather than surfaced as a corrupt/empty turn.
    - ``reset`` — ``True`` when the file shrank below the requested offset
      (rotated/truncated): the read restarts from 0 and the client replaces
      its tail buffer instead of appending.
    - ``live`` — whether the session still maps to a running bridge/agent/hosted
      session. The client stops polling once this is ``False`` (the session
      ended), after draining this final delta.

    Same fail-closed boundary as the paged reader: ``name`` is project-name
    validated (422); ``session`` is resolved strictly inside the project's
    transcript dir via :func:`usage.resolve_session_transcript` (unsafe/unknown
    → 404, never a directory escape); an unreadable transcript (``OSError``)
    degrades to a defined 503 with no on-disk path in the body — never a bare
    500. Read-only throughout: it never writes or mutates the transcript.
    """
    if not is_valid_project_name(name):
        raise HTTPException(status_code=422, detail="invalid project name")
    # Clamp a negative offset to 0 defensively (the reader clamps too, but keep
    # the wire contract clean); a client can't seek to a negative position.
    start = max(offset, 0)
    project_path = config.projects_root / name
    # In-memory liveness join (never disk) — computed before the off-thread read.
    live = session in _live_session_uuids(runner, hosted, project_path, name)

    def _tail() -> dict:
        """Read the turns added since ``start`` and return them with the next offset."""
        path = usage.resolve_session_transcript(project_path, session)
        if path is None:
            # Fail closed: unsafe or unknown session is a 404, never a path escape.
            raise HTTPException(status_code=404, detail="transcript not found")
        turns, new_offset, reset = usage.read_transcript_turns_from_offset(path, start)
        return {
            "project": name,
            "session": session,
            "turns": turns,
            "offset": new_offset,
            "reset": reset,
        }

    try:
        body = await asyncio.to_thread(_tail)
    except HTTPException:
        raise
    except (FileNotFoundError, OSError) as exc:
        logger.warning("transcript tail failed for %r/%r: %s", name, session, exc)
        raise HTTPException(status_code=503, detail="could not read transcript") from exc
    body["live"] = live
    return body
