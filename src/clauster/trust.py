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
