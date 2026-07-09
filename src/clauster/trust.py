"""Workspace-trust writer (spec §11 RESOLVED + guardrail).

A bridge refuses to spawn in an untrusted directory. Trust lives in
``~/.claude.json`` under ``projects[<resolved-abs-path>].hasTrustDialogAccepted``
and inherits down a tree. Clauster offers an explicit "Trust this directory"
action that sets the flag for exactly one project key.

The ``claude`` CLI writes this same file concurrently. The hardened locked
read-modify-write that guards it — advisory ``flock`` + atomic replace + one-time
``.bak`` — lives in :mod:`clauster.claude_json` (factored out so the config-write
trust tier shares the identical primitive). This module keeps its own narrow gate
(set exactly one key) and runs it through the shared
:func:`~clauster.claude_json.update_claude_json` transaction.
"""

from __future__ import annotations

from pathlib import Path

from .claude_json import update_claude_json
from .discovery import CLAUDE_JSON, _load_trusted_paths, trust_state_for
from .models import TrustState

__all__ = [
    "CLAUDE_JSON",
    "ensure_remote_control_enabled",
    "is_trusted",
    "trust_directory",
]


def is_trusted(path: Path, claude_json: Path = CLAUDE_JSON) -> bool:
    """Whether ``path`` (or an ancestor) has accepted the Claude trust dialog."""
    return trust_state_for(path, _load_trusted_paths(claude_json)) is TrustState.TRUSTED


def trust_directory(path: Path, claude_json: Path = CLAUDE_JSON) -> None:
    """Atomically set ``hasTrustDialogAccepted=true`` for ``path`` only.

    Mutates exactly one key under the shared locked, atomic, one-time-``.bak``
    transaction (:func:`~clauster.claude_json.update_claude_json`) so a concurrent CLI
    writer never sees a half-written file and clauster's own overlapping writers can't
    lose each other's updates. Idempotent.
    """
    resolved = str(path.resolve())

    def _set_trust(data: dict) -> None:
        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        entry = projects.get(resolved)
        if not isinstance(entry, dict):
            entry = {}
            projects[resolved] = entry
        entry["hasTrustDialogAccepted"] = True

    update_claude_json(claude_json, _set_trust)


# Top-level ~/.claude.json flags that record the operator already acknowledged
# remote control, so `claude remote-control` skips its one-time interactive
# "Enable Remote Control? (y/n)" prompt (which a detached-stdin bridge can never
# answer — empirically verified 2026-05-31).
_REMOTE_CONTROL_FLAGS = ("hasUsedRemoteControl", "remoteDialogSeen")


def ensure_remote_control_enabled(claude_json: Path = CLAUDE_JSON) -> bool:
    """Atomically set the remote-control acknowledgment flags in ``claude_json``.

    Returns True if the file was changed, False if the flags were already set (or the
    file couldn't be read). Runs through the same shared locked, atomic temp+replace +
    one-time ``.bak`` transaction as :func:`trust_directory`, since the ``claude`` CLI
    writes this file too. Idempotent.
    """

    def _set_flags(data: dict) -> bool | None:
        if all(data.get(flag) is True for flag in _REMOTE_CONTROL_FLAGS):
            return False  # already acknowledged — signal "nothing to write"
        for flag in _REMOTE_CONTROL_FLAGS:
            data[flag] = True
        return None

    return update_claude_json(claude_json, _set_flags)
