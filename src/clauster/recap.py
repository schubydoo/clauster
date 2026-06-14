"""Wire the resume-recap SessionStart hook into a Claude ``settings.json``.

Clauster can't pass a hook to ``claude remote-control`` on the command line, so
to give a restarted bridge its prior conversation back (see
:mod:`clauster.hooks.resume_recap`) the hook is registered in the runtime
user's ``~/.claude/settings.json``. This mirrors how the runner already nudges
``~/.claude.json`` to pre-acknowledge remote control: opt-in, idempotent,
best-effort.

The hook itself is env-gated (``CLAUSTER_RESUME_RECAP=1``), so installing it
globally only affects sessions Clauster spawns — never the user's other Claude
sessions that share this config.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HOOK_SCRIPT = Path(__file__).parent / "hooks" / "resume_recap.py"

# Hidden CLI subcommand a frozen binary re-invokes to run the recap hook (see
# clauster.__main__). In a PyInstaller one-file build the loose resume_recap.py
# above lives in the ephemeral ``_MEIxxx`` extraction dir, gone once clauster
# exits — so the hook command must point at the persistent executable instead.
RECAP_SUBCOMMAND = "__recap-hook__"

# Substrings that identify OUR recap hook command regardless of install mode:
# source/venv -> "…resume_recap.py"; frozen binary -> "<exe> __recap-hook__".
# Matching either lets a pip<->binary switch self-heal the one hook in place
# rather than leaving a stale duplicate behind.
_HOOK_MARKERS = ("resume_recap.py", RECAP_SUBCOMMAND)


def _is_frozen() -> bool:
    """Whether we're running from a PyInstaller-style one-file binary."""
    return bool(getattr(sys, "frozen", False))


def hook_command(python: str | None = None, script: Path | None = None) -> str:
    """Build the ``settings.json`` command that runs the recap hook.

    Source/venv installs run the bare stdlib script under the *current* interpreter
    (fast, no Clauster import). A frozen binary instead re-invokes the persistent
    executable with the hidden :data:`RECAP_SUBCOMMAND`, because its bundled copy
    of the script sits in an ephemeral ``_MEIxxx`` dir that vanishes on exit.
    """
    if _is_frozen():
        return f'"{sys.executable}" {RECAP_SUBCOMMAND}'
    return f'"{python or sys.executable}" "{script or HOOK_SCRIPT}"'


def _matching_hook(entry: object) -> dict | None:
    """Return the command-hook dict in this entry that is OUR recap hook, or None.

    Matches on a stable signature (the script *filename* OR the frozen-binary
    subcommand) rather than the full path, so a moved venv, changed interpreter,
    or a pip<->binary switch is recognized as the same hook and updated in place
    instead of duplicated.
    """
    if not isinstance(entry, dict):
        return None
    for hook in entry.get("hooks", []):
        if isinstance(hook, dict) and any(
            m in str(hook.get("command", "")) for m in _HOOK_MARKERS
        ):
            return hook
    return None


def _atomic_write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_recap_hook_installed(
    settings_path: Path,
    *,
    command: str | None = None,
    script: Path | None = None,
) -> bool:
    """Idempotently register the SessionStart recap hook. Returns True if changed.

    Identifies our entry by the hook *script filename* so a changed interpreter
    (e.g. a moved venv) rewrites the command in place rather than duplicating.
    Unrelated SessionStart hooks (context-mode, the user's own) are preserved.
    """
    script = script or HOOK_SCRIPT
    command = command or hook_command(script=script)
    settings_path = Path(settings_path).expanduser()

    data: dict = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
            if isinstance(loaded, dict):
                data = loaded
        except (ValueError, OSError):
            data = {}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    session_start = hooks.get("SessionStart")
    if not isinstance(session_start, list):
        session_start = []
        hooks["SessionStart"] = session_start

    for entry in session_start:
        hook = _matching_hook(entry)
        if hook is not None:
            if hook.get("command") == command:
                return False  # already registered with the right command
            hook["command"] = command  # self-heal: interpreter/path changed
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(settings_path, data)
            return True

    session_start.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(settings_path, data)
    return True
