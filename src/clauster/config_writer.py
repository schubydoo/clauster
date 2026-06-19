"""Comment-preserving, fail-closed config writer for the in-app editor (FE-3, #299).

Applies an allowlisted set of edits to the config file with defense-in-depth:
re-validate (via :func:`clauster.config_editor.validate_edits`, which trips the
auth fail-closed validator) → external-edit guard (stale-hash → reject) → backup
→ atomic ``os.replace`` of a ruamel round-trip render (operator comments kept).
No live reload — the running process keeps its startup config until restarted.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .config import load_config
from .config_editor import StaleConfigError, file_hash, validate_edits

_KEEP_BACKUPS = 5
# Serialize the read→validate→hash→replace critical section so two concurrent PUTs
# (each dispatched to a worker thread) can't apply to stale bytes or clobber each
# other's temp file. Single-process deployment, so a threading.Lock is sufficient.
_write_lock = threading.Lock()


def _set_ruamel(doc: Any, dotted: str, value: Any) -> None:
    """Set a dotted key into a ruamel mapping, creating intermediate mappings."""
    parts = dotted.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = CommentedMap()
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _prune_backups(path: Path) -> None:
    """Keep only the newest ``_KEEP_BACKUPS`` ``.bak-*`` files for this config."""
    backups = sorted(path.parent.glob(path.name + ".bak-*"))
    for old in backups[:-_KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def write_edits(
    path: str | os.PathLike, edits: dict[str, Any], *, expected_hash: str | None = None
) -> str:
    """Apply allowlisted ``edits`` to the config file; return the new file hash.

    Order is fail-closed: validate (disallowed key / bad value raises before any
    I/O), then the external-edit guard, then backup + atomic write. Raises
    :class:`StaleConfigError` if the file changed since ``expected_hash`` was read,
    and re-raises :class:`~clauster.config_editor.DisallowedFieldError` /
    :class:`~clauster.config_editor.ConfigValidationError` from validation.
    """
    path = Path(path)
    with _write_lock:
        original_bytes = path.read_bytes()

        # 1. Validate the merge first — a disallowed key or bad value never reaches disk.
        raw = yaml.safe_load(original_bytes.decode("utf-8")) or {}
        validate_edits(raw, edits)

        # 2. External-edit guard: compare the bytes we actually read — re-reading the file
        #    for the hash would open a TOCTOU window where an intervening edit slips through.
        if (
            expected_hash is not None
            and hashlib.sha256(original_bytes).hexdigest() != expected_hash
        ):
            raise StaleConfigError("config file changed on disk since it was loaded")

        # 3. Render the edit onto a comment-preserving round-trip of the current file.
        ruamel = YAML()
        ruamel.preserve_quotes = True
        doc = ruamel.load(original_bytes.decode("utf-8")) or CommentedMap()
        for dotted, value in edits.items():
            _set_ruamel(doc, dotted, value)

        # 4. Backup the prior content, then atomically replace via a UNIQUE same-dir temp
        #    file (mkstemp — never a shared `clauster.yml.tmp` two writers could clobber).
        # Microsecond precision so two edits in the same second don't collide on one backup.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
        # Capture the source mode BEFORE writing the backup and create the backup with it
        # (umask only narrows the O_CREAT bits, never widens), so a hardened 0600 config —
        # which holds auth.password_hash / api_token_hash — never leaks its secrets into a
        # world-readable .bak. The atomic-replace temp below already preserves this mode.
        mode = stat.S_IMODE(path.stat().st_mode)
        backup = path.with_name(path.name + f".bak-{stamp}")
        backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            fh = os.fdopen(backup_fd, "wb")
        except BaseException:  # fdopen didn't take ownership of the fd — close it ourselves
            os.close(backup_fd)
            raise
        with fh:
            fh.write(original_bytes)
        os.chmod(backup, mode)  # exact source mode even where umask narrowed the create bits
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                ruamel.dump(doc, fh)
            os.chmod(tmp, mode)  # preserve the config's permissions (mkstemp creates 0600)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _prune_backups(path)

        # 5. Post-write re-parse — if the rendered file somehow fails to load, restore.
        try:
            load_config(path)
        except Exception:
            path.write_bytes(original_bytes)
            raise

        return file_hash(path)
