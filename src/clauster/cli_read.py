"""Headless read-only CLI commands over the shared engine facade (#775, Slice A).

``projects`` / ``status`` / ``sessions`` / ``logs`` / ``open`` drive
:class:`~clauster.engine.ClausterEngine` with no web server running. Each builds a
private engine (context-managed so its runner's persistence is disposed), hydrates
the live registry when it reports live state, prints a human table (or ``--json``)
to **stdout**, sends diagnostics to **stderr**, and returns an exit code (``0`` ok,
``2`` unknown target / precondition). Lazy-imported by ``__main__`` so the hot
``run`` path never pays for it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .engine import ClausterEngine

if TYPE_CHECKING:
    from .config import ClausterConfig

_FOLLOW_INTERVAL = 0.5


def ambiguous_id_message(engine: ClausterEngine, identity: str) -> str | None:
    """Build the "several bridges match" diagnostic, or ``None`` when unambiguous.

    Every engine resolve returns ``None`` for an ambiguous id prefix exactly as it does
    for an unknown one (#1099) — failing closed, since acting on the wrong live session
    is unrecoverable. Without this the operator would read "unknown instance" for an id
    that names several *real* bridges, and have no idea to type more characters.

    Lives here rather than in each command because ``cli_write``'s ``stop`` needs the
    identical wording; a second copy would drift the moment either is reworded.
    """
    candidates = engine.bridge_id_candidates(identity)
    if not candidates:
        return None
    # Project-name ambiguity (#1150): none of the candidate ids starts with the identity.
    # Telling the operator to "use more characters" would be wrong here — they need an id.
    is_prefix_ambiguity = any(c.startswith(identity) for c in candidates)
    hint = "use more characters" if is_prefix_ambiguity else "use an instance id directly"
    return f"clauster: ambiguous {identity!r} — matches {', '.join(candidates)}; {hint}"


def _print_json(obj: Any) -> None:
    """Dump ``obj`` as indented JSON to stdout (``Path``/enum coerced via ``str``)."""
    print(json.dumps(obj, indent=2, default=str))


def _table(headers: list[str], rows: list[list[str]]) -> None:
    """Print an aligned column table to stdout (header row + left-justified cells)."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line.rstrip())
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def cmd_projects(config: ClausterConfig, *, as_json: bool) -> int:
    """List discoverable projects with their git / trust / bypass state."""
    with ClausterEngine(config) as engine:
        projects = engine.list_projects()
    if as_json:
        _print_json([p.model_dump(mode="json") for p in projects])
        return 0
    if not projects:
        print("No projects found.", file=sys.stderr)
        return 0
    rows = [
        [
            p.name,
            "git" if p.is_git_repo else "-",
            p.trust_state.value,
            "yes" if p.allow_bypass_permissions else "-",
            str(p.path),
        ]
        for p in projects
    ]
    _table(["PROJECT", "GIT", "TRUST", "BYPASS", "PATH"], rows)
    return 0


def cmd_status(config: ClausterConfig, *, as_json: bool) -> int:
    """List the known bridge instances and their live status (mode preserved)."""
    with ClausterEngine(config) as engine:
        asyncio.run(engine.hydrate())  # read-only reattach (never writes shared state)
        instances = engine.list_instances()
    if as_json:
        _print_json([i.model_dump(mode="json") for i in instances])
        return 0
    if not instances:
        print("No bridge instances.", file=sys.stderr)
        return 0
    rows = [
        [i.instance_id[:8], i.project, i.channel, i.resume_mode, i.status.value, i.label]
        for i in instances
    ]
    _table(["INSTANCE", "PROJECT", "CHANNEL", "MODE", "STATUS", "LABEL"], rows)
    return 0


def cmd_sessions(config: ClausterConfig, *, as_json: bool) -> int:
    """List the host's live working sessions (``claude agents --json``, read-only)."""
    with ClausterEngine(config) as engine:
        sessions = engine.working_sessions()
    if as_json:
        _print_json([s.model_dump(mode="json") for s in sessions])
        return 0
    if not sessions:
        print("No working sessions.", file=sys.stderr)
        return 0
    rows = [[s.local_uuid[:8], s.kind, s.state or "-", str(s.pid), str(s.cwd)] for s in sessions]
    _table(["SESSION", "KIND", "STATE", "PID", "CWD"], rows)
    return 0


def _log_read_error(path: Path) -> str | None:
    """Return why ``path`` can't be read as a log file, or ``None`` if it opens fine.

    The tail helpers (:mod:`clauster.logstream`) deliberately swallow file errors so a
    momentary hiccup never crashes the live WebSocket stream — but for a one-shot CLI
    read that turns a missing / directory / permission-denied path into a silent
    "0 lines, exit 0". A single ``open`` probe surfaces all of those uniformly so
    ``logs`` can fail closed instead.
    """
    try:
        with path.open("rb"):
            return None
    except OSError as exc:
        return exc.strerror or str(exc)


def cmd_logs(config: ClausterConfig, identity: str, *, follow: bool) -> int:
    """Tail a bridge's redacted log; ``--follow`` streams new lines until interrupted."""
    with ClausterEngine(config) as engine:
        asyncio.run(engine.hydrate())  # populate the registry so the id resolves
        path = engine.bridge_log_path(identity)
        if path is None:
            print(
                ambiguous_id_message(engine, identity)
                or f"clauster: no bridge log for {identity!r} (unknown instance or no log yet)",
                file=sys.stderr,
            )
            return 2
        if (err := _log_read_error(path)) is not None:
            print(
                f"clauster: cannot read bridge log for {identity!r} ({path}): {err}",
                file=sys.stderr,
            )
            return 2
        offset = engine.initial_log_offset(path)
        offset, lines = engine.read_log_lines(path, offset)
        for line in lines:
            print(line)
        if not follow:
            return 0
        try:
            while True:
                time.sleep(_FOLLOW_INTERVAL)
                if (err := _log_read_error(path)) is not None:
                    # The log became unreadable mid-follow (rotated/deleted/replaced):
                    # the reader would swallow it and the loop would sleep forever
                    # showing nothing. Fail closed rather than a silent idle stream.
                    print(
                        f"clauster: bridge log for {identity!r} became unreadable ({path}): "
                        f"{err}; stopping follow",
                        file=sys.stderr,
                    )
                    return 1
                offset, new_lines = engine.read_log_lines(path, offset)
                for line in new_lines:
                    print(line)
        except KeyboardInterrupt:  # Ctrl-C is the normal way to stop a follow
            return 0


def cmd_open(config: ClausterConfig, identity: str, *, launch: bool) -> int:
    """Print a bridge's connect URL (``--launch`` also opens it in a browser)."""
    with ClausterEngine(config) as engine:
        asyncio.run(engine.hydrate())
        url = engine.connect_url(identity)
        # Resolved inside the block: the engine (and its registry) is disposed on exit,
        # so the ambiguity lookup has to happen while it is still live.
        ambiguous = ambiguous_id_message(engine, identity) if url is None else None
    if url is None:
        print(
            ambiguous
            or f"clauster: no connect URL for {identity!r} (unknown instance or not ready yet)",
            file=sys.stderr,
        )
        return 2
    print(url)
    if launch:
        import webbrowser

        webbrowser.open(url)
    return 0
