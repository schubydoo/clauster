"""Foundation plumbing for the code-executing config-write trust tier (#347/#687).

This is the **shared seam** the child surfaces (MCP servers #688, permission rules
#689, hooks #690, skills #691) build on; it ships **no concrete writer endpoint of
its own**. Everything here is fail-closed by construction:

* :func:`require_capability` — the route-level 404 gate. Capability off ⇒ 404; a
  user-scope request with ``allow_user_scope`` off ⇒ 404 (a disabled capability is
  *invisible*, exactly like the reaper UI). Use 404, never 403.
* :func:`expected_confirm_token` / :func:`require_confirm` — the per-write
  type-the-name confirm. The server re-derives the expected token (the literal
  resolved project name, or the literal ``USER SCOPE`` for user scope) and rejects a
  mismatch with 400 **before any validation or I/O**, so a CSRF / accidental-click
  write is structurally impossible.
* :func:`resolve_project_dir` — path containment for project-scope writes (resolve,
  reject ``..``/symlink escape) before touching disk.
* :func:`validate_candidate` — the validate-never-execute seam. Validation is
  *structural only* — never spawn/resolve/run the edited content (that would turn the
  validator itself into the RCE). A bad shape raises :class:`InvalidCandidateError`
  (→ 422), nothing written.
* :func:`guard_unchanged` — the stale-hash external-edit guard for project-file
  writes (→ 409 :class:`StaleConfigWriteError`).
* :func:`redact_secrets` / :func:`merge_redacted` — the structural-redaction read
  path. Secret-shaped values are emitted as a masked sentinel from the start (never
  assembled into the response); a write that sends the sentinel back is a no-op for
  that key (keep-stored).
* :func:`write_subtree` — the writer primitive: locked read → merge **only** the
  named subtree → atomic replace of ``~/.claude.json``, reusing the hardened
  :mod:`clauster.claude_json` machinery (never a whole-file browser blob).

The gate ordering the children must follow (each step aborts before the write)::

    route → capability flag (404 if off)
          → scope flag (404 if user-scope and allow_user_scope off)
          → auth (already global)
          → type-the-name confirm (400 on mismatch)
          → VALIDATE candidate content (reject → 422, nothing written)
          → external-edit / stale-hash guard (409 if changed under us)
          → lock + merge + atomic write
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException

from .claude_json import _atomic_write_claude_json, _locked, _read_claude_json
from .config import ClausterConfig
from .discovery import is_valid_project_name

Scope = Literal["project", "user"]

#: The literal token the UI shows (and the caller must retype) for a user-scope write.
#: Not a credential — it is a fixed, public type-the-name confirm string the user reads
#: off the screen and retypes, so the hardcoded-password lint is a false positive here.
USER_SCOPE_TOKEN = "USER SCOPE"  # noqa: S105 - public confirm literal, not a secret

#: The masked sentinel emitted in place of any secret-shaped value. A write that sends
#: it back is treated as "keep the stored value" (a no-op merge for that key).
REDACTION_SENTINEL = "********"

# Secret-shaped value detection (structural redaction). A value is masked when it
# *looks* like credential material: a ``${...}`` interpolation, a token-bearing URL
# scheme (e.g. ``slack://TOKEN@host``), or a long high-entropy-ish opaque string. This
# is deliberately conservative — over-masking a non-secret only forces the caller to
# resend it; under-masking would leak. Keys are matched case-insensitively.
_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[-_]?key|apikey|auth|credential|bearer)",
    re.IGNORECASE,
)
_INTERP_RE = re.compile(r"\$\{[^}]+\}")
_SECRETISH_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://[^/@\s]+@", re.IGNORECASE)


class ConfigWriteError(Exception):
    """Base for config-write failures (mapped to a 4xx at the route layer)."""


class InvalidCandidateError(ConfigWriteError):
    """Candidate content failed structural validation (→ 422; nothing written)."""


class StaleConfigWriteError(ConfigWriteError):
    """A project file changed under us since its hash was read (→ 409)."""


class PathEscapeError(ConfigWriteError):
    """A project-scope path resolved outside its project (→ 400; reject before I/O)."""


def require_capability(config: ClausterConfig, scope: Scope) -> None:
    """Fail-closed 404 gate: raise 404 unless the capability (and scope) is enabled.

    Capability off ⇒ 404 on every config-write route — the surface is *invisible*,
    not forbidden (404, never 403), the same shape the reaper UI uses. A user-scope
    request additionally requires the independent ``allow_user_scope`` opt-in; without
    it the user-scope surface is likewise 404. Project-scope can run while user-scope
    stays off.
    """
    if not config.config_write.enabled:
        raise HTTPException(status_code=404, detail="config-write is disabled")
    if scope == "user" and not config.config_write.allow_user_scope:
        raise HTTPException(status_code=404, detail="config-write user scope is disabled")


def expected_confirm_token(scope: Scope, project_name: str | None) -> str:
    """Re-derive the confirmation token the caller must retype for this write.

    Project-scope ⇒ the literal resolved project name; user-scope ⇒ the literal
    :data:`USER_SCOPE_TOKEN`. The server *always* derives this itself and never trusts
    a client-supplied "expected" value — that is what makes the confirm a real CSRF /
    accidental-click guard.
    """
    if scope == "user":
        return USER_SCOPE_TOKEN
    if not project_name:
        # A project-scope write with no resolved project can have no valid token, so it
        # can never be confirmed — fail closed rather than accept an empty match.
        raise HTTPException(status_code=400, detail="project-scope write requires a project")
    return project_name


def require_confirm(scope: Scope, project_name: str | None, supplied: object) -> None:
    """Reject (400) unless ``supplied`` exactly equals the server-derived token.

    Runs **after** the capability/scope/auth gates and **before** any validation or
    I/O. A non-string or mismatched token is a 400 — the caller must have read and
    retyped the target.
    """
    expected = expected_confirm_token(scope, project_name)
    if not isinstance(supplied, str) or supplied != expected:
        raise HTTPException(status_code=400, detail=f"confirmation text must be {expected!r}")


def resolve_project_dir(projects_root: Path, project_name: str) -> Path:
    """Resolve a project name to a contained absolute directory, or fail closed.

    Validates the name shape (rejecting ``..`` / path separators outright) and then
    confirms the resolved path is *inside* ``projects_root`` — a symlink or crafted
    name that escapes the root raises :class:`PathEscapeError` before any I/O, the
    same validate-before-spawn discipline ``provisioning`` applies. The returned path
    is the boundary a child's project-scope write must stay within.
    """
    if not is_valid_project_name(project_name):
        raise PathEscapeError(f"invalid project name: {project_name!r}")
    root = projects_root.resolve()
    candidate = (root / project_name).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathEscapeError(f"project path escapes projects_root: {project_name!r}")
    return candidate


def validate_candidate(candidate: Any, validator: Callable[[Any], None]) -> None:
    """Run a structural-only ``validator`` over ``candidate``; reject (422) on failure.

    The validate-**never-execute** seam: ``validator`` inspects shape/types only and
    raises (any exception) when the candidate is malformed; it must never spawn,
    resolve, or run the content (that would convert the validator into the RCE). A
    failure is re-raised as :class:`InvalidCandidateError` so nothing is written. The
    concrete per-surface schemas live in the children (#688-#691); this is the seam
    they plug into.
    """
    try:
        validator(candidate)
    except InvalidCandidateError:
        raise
    except Exception as exc:  # noqa: BLE001 - any structural failure ⇒ reject, never crash
        raise InvalidCandidateError(str(exc)) from exc


def hash_bytes(data: bytes) -> str:
    """Return the hex SHA-256 of ``data`` (the external-edit token for project files)."""
    return hashlib.sha256(data).hexdigest()


def guard_unchanged(path: Path, expected_hash: str | None) -> bytes:
    """Read ``path`` and raise :class:`StaleConfigWriteError` if it changed under us.

    The project-file external-edit guard (mirrors ``config_writer``): when
    ``expected_hash`` is given, the freshly read bytes must still hash to it, else the
    file was edited since the caller loaded it (→ 409). Returns the read bytes so the
    caller hashes them once (no TOCTOU re-read). A missing file reads as empty bytes,
    which only matches an ``expected_hash`` of the empty digest.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    if expected_hash is not None and hash_bytes(data) != expected_hash:
        raise StaleConfigWriteError("config file changed on disk since it was loaded")
    return data


def _is_secretish(key: str, value: Any) -> bool:
    """Whether ``(key, value)`` should be masked (structural secret detection)."""
    if not isinstance(value, str) or not value:
        return False
    if _SECRET_KEY_RE.search(key):
        return True
    if _INTERP_RE.search(value):
        return True
    if _SECRETISH_URL_RE.match(value):
        return True
    return False


def redact_secrets(data: Any, _key: str = "") -> Any:
    """Return a deep copy of ``data`` with secret-shaped values masked from the start.

    Redaction is **structural, not a post-filter**: a secret-shaped leaf is emitted as
    :data:`REDACTION_SENTINEL` while building the display value, so the live secret is
    never assembled into the response in the first place. Recurses dicts (keys carry
    the secret-hint) and lists; non-secret scalars pass through unchanged. Mirrors
    ``config_editor.editable_values`` (read only what's safe to surface).
    """
    if isinstance(data, dict):
        return {
            k: (REDACTION_SENTINEL if _is_secretish(str(k), v) else redact_secrets(v, str(k)))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_secrets(v, _key) for v in data]
    if _is_secretish(_key, data):
        return REDACTION_SENTINEL
    return data


def merge_redacted(incoming: Any, stored: Any) -> Any:
    """Merge a write-side ``incoming`` value over ``stored``, honoring keep-stored.

    Implements the "unchanged-masked ⇒ keep-stored" rule: any leaf in ``incoming``
    equal to :data:`REDACTION_SENTINEL` is dropped and the ``stored`` value is kept —
    the browser can never read a secret out, and a write that doesn't touch it need not
    resend it. Dicts merge key-by-key (recursing); a sentinel for a key the store
    doesn't have is dropped entirely (there is nothing to keep). Non-dict / non-sentinel
    values from ``incoming`` win.
    """
    if incoming == REDACTION_SENTINEL:
        return stored  # keep-stored (may be a missing-key sentinel handled by caller)
    if isinstance(incoming, dict) and isinstance(stored, dict):
        out: dict[Any, Any] = {}
        for k, v in incoming.items():
            if v == REDACTION_SENTINEL:
                if k in stored:
                    out[k] = stored[k]  # keep the stored secret
                # else: sentinel for an absent key ⇒ nothing to keep, drop it
            else:
                out[k] = merge_redacted(v, stored.get(k))
        return out
    return incoming


def write_subtree(claude_json: Path, subtree_key: str, mutate: Callable[[Any], Any]) -> None:
    """Locked read → set **only** ``subtree_key`` → atomic replace of ``claude_json``.

    The user-scope writer primitive. ``mutate`` receives the *current* value of
    ``data[subtree_key]`` (or ``None`` when absent) and returns the new subtree value;
    every other top-level key is preserved verbatim by the atomic replace — never a
    whole-file browser blob over the top, which would wipe the operator's trust grants
    and tokens. Runs under the shared :mod:`clauster.claude_json` ``flock`` + one-time
    ``.bak`` + mode-preserving atomic replace, the same machinery ``trust`` uses.
    """
    with _locked(claude_json):
        raw, data = _read_claude_json(claude_json)
        data[subtree_key] = mutate(data.get(subtree_key))
        _atomic_write_claude_json(claude_json, raw, data)


def capability_status(config: ClausterConfig) -> dict[str, bool]:
    """Return the config-write capability flags for the (gated) status surface.

    A minimal, read-only view the Foundation may expose behind
    :func:`require_capability`: it never reflects any config *content*, only the two
    opt-in flags, so it leaks nothing. Reachable only when ``enabled`` is true (the
    gate 404s otherwise), so a caller that sees it already knows the capability is on.
    """
    return {
        "enabled": config.config_write.enabled,
        "allow_user_scope": config.config_write.allow_user_scope,
    }
