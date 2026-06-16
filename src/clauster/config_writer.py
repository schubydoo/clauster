"""Comment-preserving, fail-closed config writer for the in-app editor (FE-3, #299).

Applies an allowlisted set of edits to the config file with defense-in-depth:
re-validate (via :func:`clauster.config_editor.validate_edits`, which trips the
auth fail-closed validator) → external-edit guard (stale-hash → reject) → backup
→ atomic ``os.replace`` of a ruamel round-trip render (operator comments kept).
No live reload — the running process keeps its startup config until restarted.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .config import load_config
from .config_editor import StaleConfigError, file_hash, validate_edits

_KEEP_BACKUPS = 5


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
    original_bytes = path.read_bytes()

    # 1. Validate the merge first — a disallowed key or bad value never reaches disk.
    raw = yaml.safe_load(original_bytes.decode("utf-8")) or {}
    validate_edits(raw, edits)

    # 2. External-edit guard: refuse if the file moved under us since the editor loaded it.
    if expected_hash is not None and file_hash(path) != expected_hash:
        raise StaleConfigError("config file changed on disk since it was loaded")

    # 3. Render the edit onto a comment-preserving round-trip of the current file.
    ruamel = YAML()
    ruamel.preserve_quotes = True
    doc = ruamel.load(original_bytes.decode("utf-8")) or CommentedMap()
    for dotted, value in edits.items():
        _set_ruamel(doc, dotted, value)

    # 4. Backup the prior content, then atomically replace via a same-dir temp file.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path.with_name(path.name + f".bak-{stamp}").write_bytes(original_bytes)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        ruamel.dump(doc, fh)
    os.replace(tmp, path)
    _prune_backups(path)

    # 5. Post-write re-parse — if the rendered file somehow fails to load, restore.
    try:
        load_config(path)
    except Exception:
        path.write_bytes(original_bytes)
        raise

    return file_hash(path)
