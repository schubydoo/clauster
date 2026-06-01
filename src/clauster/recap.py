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


def hook_command(python: str | None = None, script: Path | None = None) -> str:
    """The ``settings.json`` command that runs the recap hook.

    Defaults to the *current* interpreter and the installed script path so the
    hook runs under the same Python (and venv) Clauster runs in.
    """
    return f'"{python or sys.executable}" "{script or HOOK_SCRIPT}"'


def _entry_has_script(entry: object, script_str: str) -> bool:
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks", []):
        if isinstance(hook, dict) and script_str in str(hook.get("command", "")):
            return True
    return False


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

    Identifies our entry by the hook *script path* so a changed interpreter
    (e.g. a moved venv) updates in place rather than duplicating. Unrelated
    SessionStart hooks (context-mode, the user's own) are preserved.
    """
    script_str = str(script or HOOK_SCRIPT)
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

    if any(_entry_has_script(entry, script_str) for entry in session_start):
        return False

    session_start.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(settings_path, data)
    return True
