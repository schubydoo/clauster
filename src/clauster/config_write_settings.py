"""Generic settings.json editor surface (#772) over the #347 Foundation seam.

Sibling of the permission-rules (#689) and hooks (#690) surfaces: it covers the
``settings.json`` keys **not** owned by a dedicated config-write surface --
chiefly ``env`` and ``model``, plus any other "documented misc" top-level key
Claude Code defines (see the settings docs cited below) -- through the same
fail-closed Foundation pipeline (gate -> confirm -> validate -> contain ->
stale-hash -> atomic write). It owns no gate, no writer, and no redaction
primitive of its own -- it *consumes* :mod:`clauster.config_write`.

**The carve (#772 scope, locked 2026-06-29).** :data:`OWNED_KEYS` --
``permissions``, ``hooks``, ``enabledPlugins``, ``disabledPlugins``,
``enabledMcpjsonServers``, ``disabledMcpjsonServers`` -- are excluded from this
surface: each already has (or will have) its own dedicated config-write surface,
and writing them here would race/duplicate that surface's own validation. A
write candidate naming one of them is rejected **whole** (422), never silently
stripped. Every *other* top-level key -- ``env``, ``model``, and the wide (and
still-growing) vocabulary of "documented misc" keys Claude Code ships
(``apiKeyHelper``, ``cleanupPeriodDays``, ``includeCoAuthoredBy``,
``statusLine``, ``outputStyle``, ``attribution``, ... see
https://docs.anthropic.com/en/docs/claude-code/settings) -- is handled
generically here. Unlike the permissions/hooks validators (a narrow, enumerated
shape because they are literally code Claude executes), this surface
deliberately does **not** maintain an exhaustive allowlist of recognized misc
keys: Claude Code's settings vocabulary grows every release, and clauster never
resolves, spawns, or interprets any of these values itself (only the spawned
``claude`` process ever reads them) -- an unenumerated key is simply passed
through as opaque JSON. The one exception is ``env``, which gets its own shape
check (must be a flat ``str -> str`` map) because it is also this surface's one
true secret-bearing shape (see the redaction decision below).

**Redaction decision (#772, applying the #822 lesson) -- flagged for maintainer
review.** :func:`~clauster.config_write.redact_secrets` only masks a value whose
*key* looks credential-shaped (``token``/``secret``/``password``/``api[-_]key``/
``auth``/``credential``/``bearer``), or whose value looks like a ``${...}``
interpolation or a credential-bearing URL -- it does **not** detect a real
secret stored under a benign-looking key such as ``DEPLOY_KEY`` (no ``token``/
``secret``/... substring, and the regex requires an ``api`` prefix before
``key`` -- a bare ``key`` never matches). ``env`` is *exactly* where operators
keep such secrets, and the #822 incident is the concrete example of this exact
gap. This surface resolves the tension between "the operator must be able to
edit a non-secret env var" and "never assemble a live secret into an HTTP
response" as follows:

* **Every** ``env`` value is masked on read, unconditionally -- not just the
  key/value-shaped subset :func:`~clauster.config_write.redact_secrets` would
  catch. The read view shows ``{"MY_VAR": "********"}`` for every entry,
  secret-looking or not (see :func:`_redact_misc`).
* Every *other* top-level key runs through the normal
  :func:`~clauster.config_write.redact_secrets` key/value heuristic (``model``
  is not secret-shaped and passes through in the clear; something coincidentally
  matching the heuristic, e.g. an ``apiKeyHelper`` shell-command path, is masked
  -- over-masking, the same deliberately-conservative direction the rest of the
  Foundation takes).
* **Write is unaffected** -- the operator can still send a real (unmasked) value
  for any ``env`` var or other key; only a value that comes back as the literal
  sentinel is treated as keep-stored
  (:func:`~clauster.config_write.merge_redacted`). This means the editor
  supports "add/replace this var" for every var, but reading an existing var's
  value back requires resending it verbatim (the sentinel) to keep it -- the
  operator cannot use this surface to *reveal* what is currently stored. That is
  the deliberate trade-off: full editability, zero read-side disclosure, for the
  one subtree that is a real secret store.

This is a **default, not a claimed-final answer** -- the design doc (#772
scoping, 2026-06-29 §8) explicitly flagged the env-secret tension as needing a
maintainer steer; a masked-with-explicit-reveal or a write-only-for-
suspected-secrets model are both reasonable alternatives if the blanket mask
above is too coarse in practice.

**Scope-merge provenance (the novel part of #772).**
:func:`compute_effective_settings` computes, for the union of (non-owned)
top-level keys across the three scopes, the *effective* value and *which scope
supplied it* -- following Claude Code's own precedence order (verified against
https://docs.anthropic.com/en/docs/claude-code/settings, "How scopes interact",
checked 2026-07-02): **Managed > command-line arguments > Local > Project >
User**. Managed settings and CLI-arg overrides are out of clauster's scope (see
the config-management design doc's scope model -- clauster manages only
User/Project/Local), so among the three scopes clauster manages, the order is
**local > project > user**. Precedence is computed **per top-level key** (the
docs' own "merged at the individual setting level, not the entire file"
default): a scope that does not define a key at all falls through to the next
scope; a scope that *does* define the key supplies the **entire** value -- this
surface does not deep-merge, e.g. a project-scope ``env`` object is not
key-by-key merged with a user-scope ``env`` object; the highest-precedence scope
that defines ``env`` at all supplies it whole. The docs call out specific
*documented* exceptions where arrays merge/concatenate across scopes instead of
one scope winning outright (``permissions.allow``/``deny``, sandbox filesystem
allow/deny paths, ``allowedHttpHookUrls``) -- none of those keys are in this
surface's carve (``permissions`` is `OWNED_KEYS`-excluded entirely), so the
simpler per-key-whole-value rule applies to everything this surface manages.

Three read/write surfaces, one entry shape (mirroring Claude Code's own
``settings.json``):

* **project** scope -> ``<project>/.claude/settings.json``
* **local** scope -> ``<project>/.claude/settings.local.json`` (gitignored on
  create, #766)
* **user** scope -> ``~/.claude/settings.json``, gated additionally on
  ``allow_user_scope``

Only the non-owned top-level keys are ever written; ``permissions``/``hooks``/
the plugin- and MCP-enable keys, and every other sibling key in the file, are
preserved verbatim by the locked atomic replace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config_write as cw
from .claude_json import locked_replace_json_file

#: Top-level ``settings.json`` keys owned by a DEDICATED config-write surface.
#: This surface refuses to read or touch them: they are excluded from the read
#: view (a caller wanting them must use their own surface) and a write
#: candidate that names any of them is rejected **whole** (422) rather than
#: silently accepted or silently stripped -- fail closed, mirroring the hooks
#: surface's plugin-owned-hook guard (:mod:`clauster.config_write_hooks`).
OWNED_KEYS = frozenset(
    {
        "permissions",
        "hooks",
        "enabledPlugins",
        "disabledPlugins",
        "enabledMcpjsonServers",
        "disabledMcpjsonServers",
    }
)

#: The one key this surface treats as an unconditional secret container: every
#: value under it is masked on read regardless of the key/value heuristic (see
#: the module docstring's redaction decision).
ENV_KEY = "env"

#: Scope precedence among the three scopes clauster manages, highest first
#: (local overrides project overrides user) -- see module docstring.
_SCOPE_PRECEDENCE: tuple[cw.Scope, ...] = ("local", "project", "user")


class SettingsCarveError(cw.InvalidCandidateError):
    """A candidate named a key owned by a dedicated config-write surface."""


def _validate_env(value: Any) -> None:
    """Reject ``value`` unless it is a flat ``str -> str`` mapping.

    The only shape check this surface performs beyond "not an owned key" --
    ``env`` is the one key this surface treats specially (see module
    docstring), so its shape is worth enforcing even though nothing here ever
    resolves or executes a variable's value.
    """
    if not isinstance(value, dict):
        raise cw.InvalidCandidateError(f"{ENV_KEY!r} must be an object")
    for key, val in value.items():
        if not isinstance(key, str) or not key:
            raise cw.InvalidCandidateError(f"{ENV_KEY!r} keys must be non-empty strings")
        if not isinstance(val, str):
            raise cw.InvalidCandidateError(f"{ENV_KEY!r} value for {key!r} must be a string")


def validate_misc_settings(candidate: Any) -> None:
    """Structural validator for the generic settings surface (the Foundation hook).

    ``candidate`` is a dict of top-level ``settings.json`` keys, excluding
    every :data:`OWNED_KEYS` member -- a candidate naming one is rejected whole
    (those keys are managed by their own dedicated surface, never here).
    ``env``, when present, must be a flat ``str -> str`` map. Every other key
    is accepted as opaque, never-interpreted JSON -- see the module docstring
    for why this surface does not maintain an exhaustive misc-key allowlist.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("settings must be an object")
    owned = OWNED_KEYS & set(candidate)
    if owned:
        raise SettingsCarveError(
            f"settings has keys owned by a dedicated surface: {sorted(owned)}"
        )
    if ENV_KEY in candidate:
        _validate_env(candidate[ENV_KEY])


def _misc_view(data: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of ``data`` this surface manages (non-owned keys)."""
    return {k: v for k, v in data.items() if k not in OWNED_KEYS}


def _redact_misc(misc: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted display copy of ``misc`` (the read-side masking rule).

    Runs the normal structural :func:`~clauster.config_write.redact_secrets`
    heuristic over every key, then unconditionally masks every ``env`` value on
    top -- the #822-lesson override described in the module docstring.
    """
    redacted = cw.redact_secrets(misc)
    env = misc.get(ENV_KEY)
    if isinstance(env, dict):
        redacted[ENV_KEY] = dict.fromkeys(env, cw.REDACTION_SENTINEL)
    return redacted


def _read_misc(path: Path) -> tuple[dict[str, Any], str]:
    """Return ``(redacted_misc, content_hash)`` for a settings file at ``path``.

    The hash is over the *current file bytes* (empty digest when absent) -- the
    caller echoes it back on write so the stale-hash guard can reject a stale
    write (409). A missing file reads as an empty settings view.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    return _redact_misc(_misc_view(data)), cw.hash_bytes(raw)


def _locked_write_misc(path: Path, incoming: dict[str, Any], expected_hash: str | None) -> None:
    """Locked stale-hash-guarded write of the misc partition of ``path``.

    The caller must have already run :func:`validate_misc_settings` (via
    :func:`~clauster.config_write.validate_candidate`) -- this only performs
    the guard + merge + atomic replace, mirroring
    :func:`~clauster.config_write.write_settings_subtree`'s contract but over
    *several* top-level keys instead of one named subtree.

    ``incoming`` is merged against the **current on-disk** misc partition via
    :func:`~clauster.config_write.merge_redacted`, so a resent
    :data:`~clauster.config_write.REDACTION_SENTINEL` (an env var, or any other
    coincidentally secret-shaped key, the operator didn't touch) keeps the
    real stored value rather than overwriting it with the literal sentinel
    string. Every key in :data:`OWNED_KEYS` is preserved byte-for-byte
    untouched; the entire non-owned partition is replaced by the merged result
    (a full-replacement subtree write, not a key-by-key patch of unspecified
    keys the caller never mentioned).
    """

    def _mutate(current_bytes: bytes) -> dict[str, Any]:
        if expected_hash is None:
            if current_bytes:
                raise cw.StaleConfigWriteError(f"{path.name} already exists; a hash is required")
        elif cw.hash_bytes(current_bytes) != expected_hash:
            raise cw.StaleConfigWriteError("config file changed on disk since it was loaded")
        current = cw.load_settings_json_obj(current_bytes)
        stored_misc = _misc_view(current)
        merged_misc = cw.merge_redacted(incoming, stored_misc)
        # Replace the whole non-owned partition; every owned key stays exactly
        # as it was (untouched key, untouched value, untouched position).
        for key in list(current):
            if key not in OWNED_KEYS:
                del current[key]
        current.update(merged_misc)
        return current

    locked_replace_json_file(path, _mutate, render=cw.render_json)


def read_project_settings(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(settings, content_hash)`` for a project's ``.claude/settings.json``."""
    return _read_misc(cw.project_settings_path(project_dir))


def write_project_settings(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the project ``.claude/settings.json`` misc partition.

    Ensures the ``<project>/.claude`` parent exists (so a first write to a
    project that has no ``.claude`` dir yet does not fail the atomic writer's
    ``mkstemp`` in a missing directory), then runs the fail-closed Foundation
    pipeline. The candidate is validated *before* the directory is created, so
    a bad shape (422) leaves the filesystem untouched.
    """
    cw.validate_candidate(incoming, validate_misc_settings)
    path = cw.project_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _locked_write_misc(path, incoming, expected_hash)


def read_user_settings(settings_json: Path) -> tuple[dict[str, Any], str]:
    """Return ``(settings, content_hash)`` for the user-scope ``~/.claude/settings.json``."""
    return _read_misc(settings_json)


def write_user_settings(
    settings_json: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the user-scope ``~/.claude/settings.json`` misc partition.

    Ensures the ``~/.claude`` parent exists, then runs the fail-closed
    Foundation pipeline. The candidate is validated *before* the directory is
    created, so a bad shape (422) writes nothing.
    """
    cw.validate_candidate(incoming, validate_misc_settings)
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    _locked_write_misc(settings_json, incoming, expected_hash)


def read_project_local_settings(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(settings, content_hash)`` for a project's ``settings.local.json``."""
    return _read_misc(cw.project_local_settings_path(project_dir))


def write_project_local_settings(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the local-scope ``.claude/settings.local.json`` misc partition.

    Third (local) scope, sibling of the project/user writers above: same
    fail-closed Foundation pipeline and stale-hash guard, targeting a *third*
    file that is you, this project only. A successful write additionally runs
    :func:`~clauster.config_write.ensure_gitignored` so a newly created
    ``settings.local.json`` is never accidentally committed (#766) --
    idempotent, so a write to an already-gitignored file is a no-op there.
    """
    cw.validate_candidate(incoming, validate_misc_settings)
    path = cw.project_local_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _locked_write_misc(path, incoming, expected_hash)
    cw.ensure_gitignored(project_dir, ".claude/settings.local.json")


def _compute_effective_settings(
    *,
    user_misc: dict[str, Any] | None,
    project_misc: dict[str, Any] | None,
    local_misc: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return, per top-level key, the effective value and which scope supplied it.

    **Module-private on purpose (contract-safety, Greptile #823 P2).** This
    function is a pure dict-merge that copies its inputs' values verbatim into
    the returned ``{"value": ..., "source": ...}`` dicts -- it does **no
    redaction of its own**. Every caller MUST pass the **already-redacted** misc
    view for each scope, i.e. the output of :func:`read_user_settings` /
    :func:`read_project_settings` / :func:`read_project_local_settings`, and
    **never raw ``settings.json`` disk content**: a caller that skipped the read
    layer and handed in raw data would silently surface live ``env`` secrets in
    the effective view (env-masking lives in the read functions, not here). The
    leading underscore keeps this function reachable only from
    :mod:`clauster.app`'s route handler (the one correct call site) so a future
    caller can't wire it to raw data by importing a public symbol.

    Each of ``user_misc``/``project_misc``/``local_misc`` is that already-redacted
    misc view, so this function never sees or handles an unmasked secret.
    ``None`` means that scope was not read at all (e.g. the caller omitted the
    user layer because ``allow_user_scope`` is off) -- distinct from an empty
    dict (the scope *was* read and genuinely defines nothing), so an unread
    scope never silently participates in the merge as if it were authoritatively
    empty.

    Precedence follows Claude Code's own scope order among the three scopes
    clauster manages -- **local > project > user** (see module docstring for
    the full chain and the docs citation) -- computed **per top-level key**:
    the highest-precedence scope that defines a key supplies the *entire*
    value for that key (no deep merge inside an object-valued key like
    ``env``). Returns ``{key: {"value": ..., "source": "local"|"project"|"user"}}``
    for the union of keys across every scope that was read.
    """
    by_scope: dict[cw.Scope, dict[str, Any] | None] = {
        "local": local_misc,
        "project": project_misc,
        "user": user_misc,
    }
    layers: list[tuple[cw.Scope, dict[str, Any]]] = []
    for scope in _SCOPE_PRECEDENCE:
        misc = by_scope[scope]
        if misc is not None:
            layers.append((scope, misc))

    # ``layers`` is in precedence order (local, project, user). Walk it
    # reversed so the highest-precedence scope is applied *last* and wins on
    # a shared key -- a plain last-write-wins merge, with no inner loop that
    # would need a break (and no "loop exhausts without finding the key"
    # branch for codecov to flag: every scope's own keys are always found in
    # that scope's own dict).
    effective: dict[str, dict[str, Any]] = {}
    for scope, misc in reversed(layers):
        for key, value in misc.items():
            effective[key] = {"value": value, "source": scope}
    return {key: effective[key] for key in sorted(effective)}
