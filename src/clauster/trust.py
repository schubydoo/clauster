"""Workspace-trust writer (spec §11 RESOLVED + guardrail).

A bridge refuses to spawn in an untrusted directory. Trust lives in
``~/.claude.json`` under ``projects[<resolved-abs-path>].hasTrustDialogAccepted``
and inherits down a tree. Clauster offers an explicit "Trust this directory"
action that sets the flag for exactly one project key.

The ``claude`` CLI writes this same file concurrently. The hardened locked
read-modify-write that guards it — advisory ``flock`` + atomic replace + one-time
``.bak`` — lives in :mod:`clauster.claude_json` (factored out so the config-write
trust tier shares the identical primitive). This module keeps its own narrow gate
(set exactly one key) and builds it on that shared transaction.

The shared private helpers (:func:`_locked`, :func:`_read_claude_json`,
:func:`_atomic_write_claude_json`) and ``fcntl`` are re-bound into this module's
namespace so existing monkeypatch-by-attribute tests (``trust.fcntl``,
``trust._read_claude_json``) keep targeting the names trust resolves.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from pathlib import Path

from .claude_json import _atomic_write_claude_json, _read_claude_json
from .claude_json import fcntl as fcntl  # re-bound so tests can patch trust.fcntl
from .discovery import CLAUDE_JSON, _load_trusted_paths, trust_state_for
from .models import TrustState

__all__ = [
    "CLAUDE_JSON",
    "ensure_remote_control_enabled",
    "is_trusted",
    "trust_directory",
]

_log = logging.getLogger("clauster.trust")


@contextlib.contextmanager
def _locked(claude_json: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for a read-modify-write of ``claude_json``.

    A thin wrapper over the shared lock in :mod:`clauster.claude_json` that resolves
    ``fcntl`` and ``os.open`` from *this* module's namespace, so the existing
    ``trust.fcntl`` / ``trust.os.open`` monkeypatch tests keep targeting the names
    trust looks up. Same degrade-to-no-op semantics as the shared primitive: where
    ``fcntl`` is unavailable (Windows) or the sidecar ``<file>.lock`` can't be
    opened, it yields without locking (the atomic replace still prevents a torn
    file).
    """
    if fcntl is None:
        yield
        return
    # The lock-path derivation MUST stay identical to ``clauster.claude_json._locked`` (the
    # canonical copy): the trust writer and the config-write writer serialize against each
    # other only by both computing this same sidecar path.
    lock_path = claude_json.with_suffix(claude_json.suffix + ".lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        _log.warning("could not open %s; proceeding without a lock: %s", lock_path, exc)
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # implicitly releases the flock


def is_trusted(path: Path, claude_json: Path = CLAUDE_JSON) -> bool:
    """Whether ``path`` (or an ancestor) has accepted the Claude trust dialog."""
    return trust_state_for(path, _load_trusted_paths(claude_json)) is TrustState.TRUSTED


def trust_directory(path: Path, claude_json: Path = CLAUDE_JSON) -> None:
    """Atomically set ``hasTrustDialogAccepted=true`` for ``path`` only.

    Reads, mutates one key, and replaces atomically under :func:`_locked` (the shared
    primitive in :mod:`clauster.claude_json`) so a concurrent CLI writer never sees a
    half-written file and clauster's own overlapping writers can't lose each other's
    updates. Idempotent.
    """
    resolved = str(path.resolve())

    with _locked(claude_json):
        raw, data = _read_claude_json(claude_json)

        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        entry = projects.get(resolved)
        if not isinstance(entry, dict):
            entry = {}
            projects[resolved] = entry
        entry["hasTrustDialogAccepted"] = True

        _atomic_write_claude_json(claude_json, raw, data)


# Top-level ~/.claude.json flags that record the operator already acknowledged
# remote control, so `claude remote-control` skips its one-time interactive
# "Enable Remote Control? (y/n)" prompt (which a detached-stdin bridge can never
# answer — empirically verified 2026-05-31).
_REMOTE_CONTROL_FLAGS = ("hasUsedRemoteControl", "remoteDialogSeen")


def ensure_remote_control_enabled(claude_json: Path = CLAUDE_JSON) -> bool:
    """Atomically set the remote-control acknowledgment flags in ``claude_json``.

    Returns True if the file was changed, False if the flags were already set (or
    the file couldn't be read). Same locked, atomic temp+replace + one-time
    ``.bak`` as :func:`trust_directory`, since the ``claude`` CLI writes this file
    too. Idempotent.
    """
    with _locked(claude_json):
        raw, data = _read_claude_json(claude_json)

        if all(data.get(flag) is True for flag in _REMOTE_CONTROL_FLAGS):
            return False  # already acknowledged — nothing to do

        for flag in _REMOTE_CONTROL_FLAGS:
            data[flag] = True

        _atomic_write_claude_json(claude_json, raw, data)
    return True
