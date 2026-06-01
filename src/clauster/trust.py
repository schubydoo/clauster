"""Workspace-trust writer (spec §11 RESOLVED + guardrail).

A bridge refuses to spawn in an untrusted directory. Trust lives in
``~/.claude.json`` under ``projects[<resolved-abs-path>].hasTrustDialogAccepted``
and inherits down a tree. Clauster offers an explicit "Trust this directory"
action that sets the flag for exactly one project key.

The ``claude`` CLI writes this same file concurrently, so the write is atomic
(temp + ``os.replace``) and preserves every other key. A one-time ``.bak`` is
taken before the first modification.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .discovery import CLAUDE_JSON, trust_state_for
from .models import TrustState

_log = logging.getLogger("clauster.trust")


def is_trusted(path: Path, claude_json: Path = CLAUDE_JSON) -> bool:
    """Whether ``path`` (or an ancestor) has accepted the Claude trust dialog."""
    from .discovery import _load_trusted_paths

    return trust_state_for(path, _load_trusted_paths(claude_json)) is TrustState.TRUSTED


def trust_directory(path: Path, claude_json: Path = CLAUDE_JSON) -> None:
    """Atomically set ``hasTrustDialogAccepted=true`` for ``path`` only.

    Reads the current file, mutates one key, and replaces atomically so a
    concurrent CLI writer never sees a half-written file. Idempotent.
    """
    resolved = str(path.resolve())

    try:
        raw = claude_json.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = None
        data = {}
    if not isinstance(data, dict):
        data = {}

    # One-time backup before the first modification.
    if raw is not None:
        backup = claude_json.with_suffix(claude_json.suffix + ".bak")
        if not backup.exists():
            try:
                backup.write_text(raw, encoding="utf-8")
            except OSError as exc:
                # Best-effort; never block the trust write, but surface it so a
                # missing pre-image isn't a silent loss of the rollback copy.
                _log.warning("could not write %s backup: %s", backup, exc)

    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects
    entry = projects.get(resolved)
    if not isinstance(entry, dict):
        entry = {}
        projects[resolved] = entry
    entry["hasTrustDialogAccepted"] = True

    tmp = claude_json.with_suffix(claude_json.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, claude_json)


# Top-level ~/.claude.json flags that record the operator already acknowledged
# remote control, so `claude remote-control` skips its one-time interactive
# "Enable Remote Control? (y/n)" prompt (which a detached-stdin bridge can never
# answer — empirically verified 2026-05-31).
_REMOTE_CONTROL_FLAGS = ("hasUsedRemoteControl", "remoteDialogSeen")


def ensure_remote_control_enabled(claude_json: Path = CLAUDE_JSON) -> bool:
    """Atomically set the remote-control acknowledgment flags in ``claude_json``.

    Returns True if the file was changed, False if the flags were already set (or
    the file couldn't be read). Same atomic temp+replace + one-time ``.bak`` as
    :func:`trust_directory`, since the ``claude`` CLI writes this file too.
    Idempotent.
    """
    try:
        raw = claude_json.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = None
        data = {}
    if not isinstance(data, dict):
        data = {}

    if all(data.get(flag) is True for flag in _REMOTE_CONTROL_FLAGS):
        return False  # already acknowledged — nothing to do

    if raw is not None:
        backup = claude_json.with_suffix(claude_json.suffix + ".bak")
        if not backup.exists():
            try:
                backup.write_text(raw, encoding="utf-8")
            except OSError as exc:
                _log.warning("could not write %s backup: %s", backup, exc)

    for flag in _REMOTE_CONTROL_FLAGS:
        data[flag] = True

    tmp = claude_json.with_suffix(claude_json.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, claude_json)
    return True
