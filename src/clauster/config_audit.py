"""Structured audit trail for config-write mutations (#958 Part 6 / #818).

Every committed config-write records ONE compact JSON line to ``config_audit.log``
under the deployment state dir — surface, scope, target file, the keys touched, and
(for the CLI-driven MCP/plugins mutations) the redacted ``claude …`` argv plus a
before/after fingerprint of the affected file. It answers the forensic question
"where did this config change land, and what ran?" without trawling the app log.

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

import json
import logging
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
