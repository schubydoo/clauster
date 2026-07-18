"""Structured audit trail for config-write mutations (#958 Part 6 / #818).

Every committed config-write records ONE compact JSON line to ``config_audit.log``
under the deployment state dir — surface, scope, target file, and the keys touched.
For MCP-server writes it additionally records a **before/after fingerprint** of each
affected file (:func:`file_fingerprints` / :func:`diff_fingerprints`) — for BOTH the
direct writer and the CLI path, since either can touch several files. The CLI case is
the motivator: the subprocess (`claude mcp …`) does Claude Code's own bookkeeping
across files, so recording which files changed — by path + sha256, never their
contents — makes that side effect visible. It answers the
forensic question "where did this config change land?" without trawling the app log.
(Recording the redacted ``claude …`` argv itself, and extending the fingerprint to
the plugins/reset/approvals handlers, is a further slice of #958 Part 6.)

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
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: One shared JSON-lines file under the state dir (was #818's ``claude_md_audit.log``).
AUDIT_FILE = "config_audit.log"
_log = logging.getLogger("clauster.config_audit")


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
    """Append one audit record for a committed config write. Best-effort, never raises.

    ``surface`` is the config surface (``"permissions"`` / ``"mcp"`` / ``"claude-md"`` …),
    ``scope`` one of ``project`` / ``user`` / ``local``, ``target`` the file (or resource)
    the write landed on, and ``action`` the verb (``create`` / ``update`` / ``delete`` /
    the CLI ``op``). ``keys`` lists the top-level keys touched (omit when not meaningful).
    ``extra`` carries surface-specific fields the caller has ALREADY redacted — e.g.
    ``{"argv": [...], "before_sha256": ..., "after_sha256": ...}`` for the CLI surfaces.

    ``state_dir`` is ``None`` only in unit contexts that don't exercise the audit trail;
    the append is skipped rather than erroring so those callers need no state dir. A real
    :class:`OSError` on the append is swallowed + logged (the write already committed).
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
        with open(resolved / AUDIT_FILE, "a", encoding="utf-8", newline="") as fh:
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
    values invariant extends to file contents (``~/.claude.json`` holds real tokens). A
    missing/unreadable file fingerprints as ``None`` (so a create/remove is detectable);
    keyed by the absolute path (this is the operator's own local audit log — the path is
    exactly the "where did it land?" answer the trail exists to give).
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
    best-effort contract: never raises. ``fields`` are the keyword-only args of
    :func:`record` (``surface`` / ``scope`` / ``target`` / ``action`` / ``actor`` / ``keys``
    / ``extra``).
    """
    await asyncio.to_thread(lambda: record(state_dir, **fields))
