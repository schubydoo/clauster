"""Structured audit trail for config-write mutations (#958 Part 6 / #818).

Every committed config-write records ONE compact JSON line to ``config_audit.log``
under the deployment state dir — surface, scope, target file, and the keys touched.
For the MCP + plugins surfaces it additionally records, in ``extra``:

* a **before/after fingerprint** of each affected file (:func:`file_fingerprints` /
  :func:`diff_fingerprints`) — by path + sha256 + byte size, never the file contents —
  so the several files a write touches (``~/.claude.json``, ``settings.json`` /
  ``settings.local.json``, ``.mcp.json``, ``known_marketplaces.json``) are all visible; and
* the **redacted ``claude mcp``/``claude plugin`` argv** the CLI actually ran, captured via
  :data:`~clauster.config_write.cli_argv_sink` (empty for the non-spawning direct writers).

The subprocess (`claude mcp/plugin …`) does Claude Code's own bookkeeping across files, so
this answers the forensic question "where did this change land, and what ran?" without
trawling the app log.

Generalises #818's CLAUDE.md-only audit into one trail for every surface. The append
is **best-effort**: the config write already committed before we get here, so a failed
audit append must never be reported as a failed save — but it is logged at ERROR so the
gap is never silent (the state dir is the same local volume as the rest of the app).

Nothing here is ever read back into a write path, and no value is executed — it is an
append-only record. Callers pass ALREADY-REDACTED argv/diff values (via
:func:`~clauster.config_write.redact_secret_lines` / ``redact_secrets``); this module
does not re-derive secrets.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: One shared JSON-lines file under the state dir (was #818's ``claude_md_audit.log``).
AUDIT_FILE = "config_audit.log"
_log = logging.getLogger("clauster.config_audit")

# Size-based rotation ceiling for the audit log (#1011). One compact JSON line is appended per
# committed config write — a rare event — so at 5 MB the current file already holds tens of
# thousands of records; past it the file rotates to `.1`, shifting `.i` -> `.i+1` and dropping
# anything beyond `_AUDIT_KEEP_ROTATED`. Total on-disk audit is therefore bounded at
# ~(keep + 1) x ceiling (~30 MB), instead of growing forever on a long-lived instance. A fixed
# ceiling (not a config key) keeps rotation surgical on this deliberately best-effort path.
_AUDIT_MAX_BYTES = 5 * 1024 * 1024
_AUDIT_KEEP_ROTATED = 5

# Serializes the rotate-then-append against itself: config writes audit from concurrent
# ``asyncio.to_thread`` workers (see :func:`arecord`), and the unlink+rename rotation sequence
# would otherwise interleave and clobber archive generations. Intra-process only — a rare edge of
# two clauster processes sharing one state_dir isn't covered (single-operator norm), and the worst
# case there is a lost archive generation, never a corrupt live log.
_AUDIT_LOCK = threading.Lock()


def _rotate_audit_log_if_needed(path: Path) -> None:
    """Rotate the audit log when it reaches the size ceiling. Best-effort — never raises.

    ``<name>`` -> ``<name>.1``, shifting ``<name>.i`` -> ``<name>.i+1`` up to
    :data:`_AUDIT_KEEP_ROTATED` (older dropped). A rotation error must never block the append
    (itself best-effort, on an already-committed write), so it is swallowed + logged and the
    append then continues against the existing file.
    """
    try:
        if path.stat().st_size < _AUDIT_MAX_BYTES:
            return
    except OSError:
        return  # no file yet, or un-stat-able — nothing to rotate
    try:
        path.with_name(f"{path.name}.{_AUDIT_KEEP_ROTATED}").unlink(missing_ok=True)
        for i in range(_AUDIT_KEEP_ROTATED - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{i + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError as exc:
        _log.warning("config audit log rotation failed (append will continue): %s", exc)


def record(
    state_dir: Path | None,
    *,
    surface: str,
    scope: str,
    target: str,
    action: str,
    actor: str = "?",
    keys: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one audit record for a committed config write. Best-effort.

    ``surface`` is the config surface (``"permissions"`` / ``"mcp"`` / ``"claude-md"`` …),
    ``scope`` one of ``project`` / ``user`` / ``local``, ``target`` the file (or resource)
    the write landed on, and ``action`` the verb (``create`` / ``update`` / ``delete`` /
    the CLI ``op``). ``keys`` lists the top-level keys touched (omit when not meaningful).
    ``extra`` carries surface-specific fields the caller has ALREADY redacted — e.g.
    ``{"argv": [...], "before_sha256": ..., "after_sha256": ...}`` for the CLI surfaces.

    ``state_dir`` is ``None`` only in unit contexts that don't exercise the audit trail;
    the append is skipped rather than erroring so those callers need no state dir. An
    :class:`OSError` on the append is swallowed + logged (the write already committed).
    Only ``OSError`` is caught, so a :class:`ValueError` from ``json.dumps`` — in practice
    a circular reference in ``extra`` — still propagates. A merely unserializable value
    does not: ``default=str`` coerces it, and ``ensure_ascii`` escapes a surrogate in
    ``target`` instead of failing to encode it.
    """
    if state_dir is None:
        return
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "actor": actor,
        "surface": surface,
        "scope": scope,
        "target": target,
        "action": action,
    }
    if keys is not None:
        entry["keys"] = keys
    if extra:
        # Caller-supplied, already-redacted fields; never let them shadow the core schema.
        entry.update({k: v for k, v in extra.items() if k not in entry})
    try:
        resolved = state_dir.expanduser()
        resolved.mkdir(parents=True, exist_ok=True)
        audit_path = resolved / AUDIT_FILE
        # Hold the lock across rotate + append so a concurrent audit can't interleave with the
        # unlink/rename sequence (both run from asyncio worker threads). The critical section is a
        # size stat + a rare append, so contention is negligible.
        with _AUDIT_LOCK:
            _rotate_audit_log_if_needed(audit_path)
            with open(audit_path, "a", encoding="utf-8", newline="") as fh:
                fh.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
    except OSError as exc:
        _log.error(
            "config write to %s (%s/%s) committed but audit append failed: %s",
            target,
            surface,
            scope,
            exc,
        )


def file_fingerprints(paths: Iterable[Path]) -> dict[str, dict[str, str | int] | None]:
    """Snapshot each path's ``{sha256, bytes}`` fingerprint, or ``None`` if absent.

    Used to take a before/after picture of the files a CLI-driven config mutation might
    touch, so :func:`diff_fingerprints` can report which ones actually changed. Records
    only a content HASH + byte size, never the bytes — the audit trail's key-names-never-
    values invariant extends to file contents (``~/.claude.json`` holds real tokens). An
    ABSENT file fingerprints as ``None``; one that exists but can't be read fingerprints as
    ``{"unreadable": True}``, so :func:`diff_fingerprints` reports ``indeterminate`` rather
    than miscalling a create/remove. Keyed by the absolute path (this is the operator's own
    local audit log — the path is exactly the "where did it land?" answer the trail exists
    to give).
    """
    out: dict[str, dict[str, Any] | None] = {}
    for path in paths:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            out[str(path)] = None  # genuinely ABSENT (distinct from unreadable below)
            continue
        except Exception:  # noqa: BLE001 - a snapshot on a committed write's critical path
            # must NEVER raise (audit is best-effort). A file that EXISTS but can't be read
            # (permissions, an exotic MemoryError on a huge file) is INDETERMINATE — mark it
            # so, so diff_fingerprints never miscalls it a create/remove (None means absent,
            # ONLY), and never 500s a write that already landed.
            out[str(path)] = {"unreadable": True}
            continue
        out[str(path)] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return out


def _unreadable(fp: dict[str, Any] | None) -> bool:
    """Whether a fingerprint marks a file that exists but couldn't be read (indeterminate)."""
    return isinstance(fp, dict) and fp.get("unreadable") is True


def diff_fingerprints(
    before: dict[str, dict[str, Any] | None], after: dict[str, dict[str, Any] | None]
) -> list[dict[str, Any]]:
    """List the files whose fingerprint changed between two :func:`file_fingerprints` snapshots.

    Each entry is ``{"file", "change", "before_sha256"?, "after_sha256"?, "after_bytes"?}``
    where ``change`` is ``created`` / ``modified`` / ``removed`` — or ``indeterminate`` when
    a side couldn't be read (never miscalled a create/remove). Unchanged files are omitted.
    Iterates ``before``'s keys (both snapshots watch the same path set), so a path present
    only in ``after`` still needs to be in ``before`` (as ``None``) to be seen.
    """
    changes: list[dict[str, Any]] = []
    for path, b in before.items():
        a = after.get(path)
        if b == a:
            continue
        # A side we couldn't read is indeterminate — record that honestly rather than
        # attributing a create/modify/remove from a hash we don't actually have.
        if _unreadable(b) or _unreadable(a):
            changes.append({"file": path, "change": "indeterminate"})
            continue
        if b is None:
            change = "created"
        elif a is None:
            change = "removed"
        else:
            change = "modified"
        entry: dict[str, Any] = {"file": path, "change": change}
        if b is not None:
            entry["before_sha256"] = b["sha256"]
        if a is not None:
            entry["after_sha256"] = a["sha256"]
            entry["after_bytes"] = a["bytes"]
        changes.append(entry)
    return changes


async def arecord(state_dir: Path | None, **fields: Any) -> None:
    """Offload :func:`record` to a worker thread — the async-handler entry point.

    The config writers already run their filesystem work via ``asyncio.to_thread``; the
    audit append is filesystem work too, so it is offloaded the same way rather than run
    on the event-loop thread, where a slow state dir would stall unrelated requests. Same
    best-effort contract, and the same caveat: :func:`record` swallows ``OSError`` only, so
    an encoding ``ValueError`` propagates through here too. ``fields`` are the args of
    :func:`record` (``surface`` / ``scope`` / ``target`` / ``action`` / ``actor`` / ``keys``
    / ``extra``).
    """
    await asyncio.to_thread(lambda: record(state_dir, **fields))
