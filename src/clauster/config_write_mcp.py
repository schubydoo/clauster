"""MCP-server config-write surface (#688) over the #347 Foundation seam.

This child adds exactly two things on top of :mod:`clauster.config_write` (the
fail-closed Foundation — gate, type-the-name confirm, validate-never-execute,
stale-hash guard, structural redaction, subtree writer): a **pure structural
validator** for an MCP server entry, and a **router** that runs the Foundation
pipeline in order for a write. It owns no gate, no writer, and no redaction logic
of its own — it *consumes* the Foundation.

The validator is **structural only**. It inspects shape and types and rejects
anything malformed; it **never resolves, spawns, or "test-runs" the ``command``**
(or any value). Running the candidate to "verify" it would make the validator
itself the RCE the whole trust tier exists to prevent — so it does not.

Three surfaces, one entry shape (mirroring Claude Code's own ``.mcp.json``):

* **project** scope ⇒ ``<project>/.mcp.json`` (top-level ``mcpServers`` object),
  guarded by the stale-hash external-edit check + path containment.
* **local** scope ⇒ ``~/.claude.json`` ``projects[<abs-project-path>].mcpServers`` —
  you, this project only, private to the operator's account (mirrors Claude Code's own
  ``claude mcp add --scope local``, and the existing per-project trust flag at
  ``projects[<abs-project-path>].hasTrustDialogAccepted``, see :mod:`clauster.trust`).
  Written through the locked nested-subtree-merge writer
  (:func:`~clauster.config_write.write_nested_subtree`); no separate file, so no
  gitignore concern (unlike the local-scope permissions/hooks files, this never lives
  inside the project directory).
* **user** scope ⇒ ``~/.claude.json`` ``mcpServers`` subtree, written through the
  locked subtree-merge writer (gated additionally on ``allow_user_scope``).

A server entry is either a **stdio** server (``command`` + optional
``args``/``env``) or a **remote** server (``type``/``transport`` of ``sse``/``http``
+ ``url`` + optional ``headers``). Unknown keys or wrong types ⇒ 422, nothing
written. Secret-shaped ``env`` values and token-bearing ``url``/``headers`` flow
through the Foundation's :func:`~clauster.config_write.redact_secrets` /
:func:`~clauster.config_write.merge_redacted` so the browser never reads a stored
secret out and a returned ``"********"`` sentinel keeps the stored value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config_write as cw

#: The top-level key holding the server map in both ``.mcp.json`` and ``mcpServers``.
MCP_SERVERS_KEY = "mcpServers"

#: The top-level ``~/.claude.json`` key holding per-project state, keyed by the
#: resolved absolute project path (the same shape :mod:`clauster.trust` uses for
#: ``hasTrustDialogAccepted``).
PROJECTS_KEY = "projects"

#: The two recognized remote transports (stdio servers carry no transport).
_REMOTE_TRANSPORTS = frozenset({"sse", "http"})

#: Allowed keys for a stdio server entry (``command`` required; rest optional).
_STDIO_KEYS = frozenset({"command", "args", "env", "type", "transport"})

#: Allowed keys for a remote server entry (``url`` required; transport required).
_REMOTE_KEYS = frozenset({"url", "headers", "env", "type", "transport"})


def _validate_str_dict(value: Any, label: str) -> None:
    """Reject ``value`` unless it is a ``dict`` of ``str`` keys to ``str`` values."""
    if not isinstance(value, dict):
        raise cw.InvalidCandidateError(f"{label} must be an object of string values")
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise cw.InvalidCandidateError(f"{label} must map string keys to string values")


def _entry_transport(entry: dict[str, Any]) -> str | None:
    """Return the remote transport declared by ``entry`` (``type``/``transport``), or None.

    ``type`` and ``transport`` are accepted as synonyms (Claude Code has used both).
    A non-string, or the two disagreeing, is a structural error. A stdio entry
    declares neither (or the literal ``"stdio"``) ⇒ returns ``None``.
    """
    declared = {entry[k] for k in ("type", "transport") if k in entry}
    if not declared:
        return None
    if not all(isinstance(v, str) for v in declared):
        raise cw.InvalidCandidateError("server 'type'/'transport' must be a string")
    if len(declared) > 1:
        raise cw.InvalidCandidateError("server 'type' and 'transport' disagree")
    (value,) = declared
    if value == "stdio":
        return None
    if value not in _REMOTE_TRANSPORTS:
        raise cw.InvalidCandidateError(f"unknown server transport {value!r} (want stdio/sse/http)")
    return value


def _validate_server_entry(name: Any, entry: Any) -> None:
    """Structurally validate one ``name -> entry`` MCP server, or raise (→ 422).

    Stdio entries require a non-empty string ``command`` (``args`` a list of
    strings, ``env`` a string→string map); remote entries require a non-empty
    string ``url`` (``headers`` a string→string map). Unknown keys are rejected.
    **Nothing here resolves, spawns, or runs the command/url** — shape only.
    """
    if not isinstance(name, str) or not name:
        raise cw.InvalidCandidateError("server name must be a non-empty string")
    if not isinstance(entry, dict):
        raise cw.InvalidCandidateError(f"server {name!r} must be an object")

    transport = _entry_transport(entry)
    is_remote = transport is not None
    allowed = _REMOTE_KEYS if is_remote else _STDIO_KEYS
    unknown = set(entry) - allowed
    if unknown:
        raise cw.InvalidCandidateError(f"server {name!r} has unknown keys: {sorted(unknown)}")

    if "env" in entry:
        _validate_str_dict(entry["env"], f"server {name!r} 'env'")

    if is_remote:
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise cw.InvalidCandidateError(
                f"remote server {name!r} requires a non-empty string 'url'"
            )
        if "headers" in entry:
            _validate_str_dict(entry["headers"], f"server {name!r} 'headers'")
        return

    # stdio
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise cw.InvalidCandidateError(
            f"stdio server {name!r} requires a non-empty string 'command'"
        )
    args = entry.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise cw.InvalidCandidateError(f"server {name!r} 'args' must be a list of strings")


def validate_mcp_servers(candidate: Any) -> None:
    """Structural validator for the whole ``mcpServers`` map (the Foundation hook).

    ``candidate`` is the desired ``mcpServers`` object: a ``dict`` mapping each
    server name to its entry. Every entry is validated structurally; a single bad
    entry rejects the whole write (→ 422 via :func:`config_write.validate_candidate`),
    so a partial/garbled map never lands. **Never executes any value.**
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("mcpServers must be an object")
    for name, entry in candidate.items():
        _validate_server_entry(name, entry)


def read_project_servers(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(redacted_servers, content_hash)`` for a project's ``.mcp.json``.

    The hash is over the *current file bytes* (empty digest when absent) — the caller
    echoes it back on write so the stale-hash guard can reject a stale write (409).
    Secret-shaped values are masked by the Foundation's structural redaction before
    they ever leave this function, so the browser never reads a stored secret. A
    missing file reads as an empty server map.
    """
    path = project_dir / ".mcp.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    servers = data.get(MCP_SERVERS_KEY)
    servers = servers if isinstance(servers, dict) else {}
    return cw.redact_secrets(servers), cw.hash_bytes(raw)


def read_user_servers(claude_json: Path) -> dict[str, Any]:
    """Return the redacted user-scope ``mcpServers`` map from ``claude_json``.

    User scope has no stale-hash token (the locked subtree-merge writer reads the
    file under its own ``flock`` at write time); read is for display only, with the
    Foundation's structural redaction applied so no stored secret is surfaced.
    """
    try:
        raw = claude_json.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    servers = data.get(MCP_SERVERS_KEY)
    servers = servers if isinstance(servers, dict) else {}
    return cw.redact_secrets(servers)


def write_project_servers(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the project ``.mcp.json`` ``mcpServers`` map, fail-closed.

    Pipeline (each step aborts before the write): validate the candidate structurally
    (→ 422) → stale-hash external-edit guard over the current bytes (→ 409) →
    :func:`config_write.merge_redacted` so a returned ``"********"`` keeps the stored
    secret → atomic replace of the rendered file. The caller must have already run the
    capability gate, the type-the-name confirm, and path containment.

    The whole read-merge-write runs inside the Foundation's
    :func:`~clauster.config_write.write_settings_subtree` transaction — the same
    hardened ``flock`` + one-time ``.bak`` + unique-``mkstemp`` + mode-preserving +
    atomic-``os.replace`` machinery — so two concurrent project writers can't lose an
    update and the file's permission bits are preserved. The stale-hash check runs
    against the bytes read *under the lock* (no TOCTOU window).

    The candidate is **never executed**; only its shape is checked.
    """
    cw.validate_candidate(incoming, validate_mcp_servers)
    path = project_dir / ".mcp.json"
    cw.write_settings_subtree(
        path, MCP_SERVERS_KEY, incoming, expected_hash, merge=cw.merge_redacted
    )


def write_user_servers(claude_json: Path, incoming: dict[str, Any]) -> None:
    """Validate + write the user-scope ``mcpServers`` subtree, fail-closed.

    Validates structurally (→ 422), then runs the Foundation's locked subtree-merge
    writer: it reads the *current* ``mcpServers`` under the ``flock``, merges via
    :func:`config_write.merge_redacted` (so a ``"********"`` keeps the stored secret),
    and atomically replaces only that subtree — every other key in ``~/.claude.json``
    (trust grants, tokens) is preserved. Never executes the candidate.
    """
    cw.validate_candidate(incoming, validate_mcp_servers)

    def _mutate(current: Any) -> dict[str, Any]:
        stored = current if isinstance(current, dict) else {}
        return cw.merge_redacted(incoming, stored)

    cw.write_subtree(claude_json, MCP_SERVERS_KEY, _mutate)


def read_project_local_servers(claude_json: Path, project_dir: Path) -> dict[str, Any]:
    """Return the redacted local-scope ``mcpServers`` map for ``project_dir``.

    Reads ``~/.claude.json`` ``projects[<abs-project-path>].mcpServers`` — private to
    the operator's account for this one project, never a shared file. Read is for
    display only (no stale-hash token, same as :func:`read_user_servers`: the locked
    nested-subtree writer reads the file under its own ``flock`` at write time), with
    the Foundation's structural redaction applied so no stored secret is surfaced. A
    missing project entry (or a missing file) reads as an empty server map.
    """
    servers = cw.read_nested_subtree(claude_json, PROJECTS_KEY, str(project_dir), MCP_SERVERS_KEY)
    servers = servers if isinstance(servers, dict) else {}
    return cw.redact_secrets(servers)


def write_project_local_servers(
    claude_json: Path, project_dir: Path, incoming: dict[str, Any]
) -> None:
    """Validate + write the local-scope ``mcpServers`` map for ``project_dir``, fail-closed.

    Third (local) scope, sibling of the project/user writers above: validates
    structurally (→ 422), then runs the Foundation's locked *nested*-subtree-merge
    writer (:func:`~clauster.config_write.write_nested_subtree`) — it reads the
    *current* ``projects[<abs-project-path>].mcpServers`` under the ``flock``, merges
    via :func:`~clauster.config_write.merge_redacted` (so a ``"********"`` keeps the
    stored secret), and atomically replaces only that one nested leaf — every other
    project's entry, every other subtree of *this* project's entry, and every other
    top-level key in ``~/.claude.json`` (trust grants, tokens) is preserved. Never
    executes the candidate. ``project_dir`` must already be the *resolved* absolute
    path (the caller's path-containment step), so the ``projects`` key it writes under
    matches the one :mod:`clauster.trust` reads/writes for the same project.
    """
    cw.validate_candidate(incoming, validate_mcp_servers)

    def _mutate(current: Any) -> dict[str, Any]:
        stored = current if isinstance(current, dict) else {}
        return cw.merge_redacted(incoming, stored)

    cw.write_nested_subtree(claude_json, PROJECTS_KEY, str(project_dir), MCP_SERVERS_KEY, _mutate)
