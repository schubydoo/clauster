"""Generic settings.json editor surface (#772) over the #347 Foundation seam.

Sibling of the permission-rules (#689) and hooks (#690) surfaces: it covers the
``settings.json`` keys **not** owned by a dedicated config-write surface --
chiefly ``env`` and ``model``, plus any other "documented misc" top-level key
Claude Code defines (see the settings docs cited below) -- through the same
fail-closed Foundation pipeline (gate -> confirm -> validate -> contain ->
stale-hash -> atomic write). It owns no gate, no writer, and no redaction
primitive of its own -- it *consumes* :mod:`clauster.config_write`.

**The carve (#772 scope, locked 2026-06-29; narrowed for TASK-449.2).** :data:`OWNED_KEYS`
-- ``permissions``, ``autoMode`` (added for TASK-449.1), ``hooks``, ``enabledPlugins`` --
are excluded from this surface: each has a dedicated config-write surface that writes
it, and writing them here would race/duplicate that surface's own validation. A key
that another tab only reads (the settings-file MCP approval lists) or that nothing reads
(``disabledPlugins``) is not carved, so this tab can show and remove it. A write
candidate naming a carved key is rejected **whole** (422), never silently stripped;
the error names the tab that owns each key. Every *other*
top-level key -- ``env``, ``model``, and the wide (and still-growing) vocabulary
of "documented misc" keys Claude Code ships (``apiKeyHelper``,
``cleanupPeriodDays``, ``includeCoAuthoredBy``, ``statusLine``, ``outputStyle``,
``attribution``, ... see https://code.claude.com/docs/en/settings-reference) -- is handled
generically here. Unlike the permissions/hooks validators (a narrow, enumerated
shape because they are literally code Claude executes), this surface
deliberately does **not** maintain an exhaustive allowlist of recognized misc
keys: Claude Code's settings vocabulary grows every release, and clauster never
resolves, spawns, or interprets any of these values itself (only the spawned
``claude`` process ever reads them) -- an unenumerated key is simply passed
through as opaque JSON. The one exception is ``env``, which gets its own shape
check (must be a flat ``str -> str`` map) because it is also this surface's one
true secret-bearing shape (see the redaction decision below).

**Keys Claude Code reads only at some scopes (TASK-449.2).** The settings reference
gives each key a scope; some are read only from user settings, only from user and local
settings, only from managed settings, or only from ``~/.claude.json``. A write that names
such a key (:data:`SCOPE_LIMITED_KEYS`) at a scope where Claude Code ignores it is
refused whole (422, :func:`reject_scope_ignored`), with a message naming the scope that
works, and the merged view leaves it out of that scope's layer. This is a scope rule,
not a key allowlist: every other key still passes through as opaque JSON.

**Redaction decision (#772, applying the #822 lesson) -- flagged for maintainer
review.** :func:`~clauster.config_write.redact_secrets` only masks a value whose
*key* -- or an **ancestor** key, since a credential-shaped key marks its whole
subtree -- looks credential-shaped (``token``/``secret``/``password``/
``api[-_]key``/``auth``/``credential``/``bearer``), or whose value looks like a
``${...}`` interpolation or a credential-bearing URL -- it does **not** detect a real
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
* Every credential helper (:data:`SECRET_HELPER_KEYS`: ``apiKeyHelper``,
  ``awsAuthRefresh``, ``awsCredentialExport``, ``gcpAuthRefresh``,
  ``otelHeadersHelper``, ``proxyAuthHelper``) is masked whole, unconditionally:
  the command can embed the credential it prints.
* Every *other* top-level key runs through the normal
  :func:`~clauster.config_write.redact_secrets` key/value heuristic (``model``
  is not secret-shaped and passes through in the clear; something coincidentally
  matching the heuristic is masked -- over-masking, the same deliberately-conservative
  direction the rest of the Foundation takes).
* **Write is unaffected** -- the operator can still send a real (unmasked) value
  for any ``env`` var or other key; only a value that comes back as the literal
  sentinel is treated as keep-stored
  (:func:`~clauster.config_write.merge_redacted`). This means the editor
  supports "add/replace this var" for every var, but reading an existing var's
  value back requires resending it verbatim (the sentinel) to keep it -- the
  operator cannot use this surface to *reveal* what is currently stored. That is
  the deliberate trade-off: full editability, zero read-side disclosure, for the
  one subtree that is a real secret store. A mask that no longer lines up with a stored
  value (a renamed ``env`` variable, a helper sent as a mask to a file without one) is
  refused whole (422) rather than dropped (:func:`_reject_orphan_sentinels`).

This is a **default, not a claimed-final answer** -- the design doc (#772
scoping, 2026-06-29 §8) explicitly flagged the env-secret tension as needing a
maintainer steer; a masked-with-explicit-reveal or a write-only-for-
suspected-secrets model are both reasonable alternatives if the blanket mask
above is too coarse in practice.

**Scope-merge provenance (the novel part of #772).**
:func:`_compute_effective_settings` (module-private on purpose) computes, for
the union of (non-owned) top-level keys across the three scopes, the *effective*
value and *which scopes supplied it* -- following Claude Code's own precedence order
(https://code.claude.com/docs/en/settings, "Settings precedence", re-checked 2026-11-04):
**Managed > command line (``--settings``) > Local > Project > User**. Managed settings
and CLI-arg overrides are out of clauster's scope (clauster manages only
User/Project/Local), so among the three scopes clauster manages, the order is
**local > project > user**. The merge itself follows Claude Code 2.1.318's merge
customizer and the same docs page ("Lists merge instead of overriding"): objects such
as ``env`` merge key by key, lists from several scopes combine, and a scalar from a
higher scope replaces a lower one (:func:`_merge_setting` lists the exceptions). Until
TASK-449.2 this view took each top-level key whole from one scope, which no longer
matched either source.

Three read/write surfaces, one entry shape (mirroring Claude Code's own
``settings.json``):

* **project** scope -> ``<project>/.claude/settings.json``
* **local** scope -> ``<project>/.claude/settings.local.json`` (gitignored on
  create, #766)
* **user** scope -> ``~/.claude/settings.json``, gated additionally on
  ``allow_user_scope``

Only the non-owned top-level keys are ever written; ``permissions``/``autoMode``/
``hooks``/``enabledPlugins`` keep their values through the locked atomic replace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config_write as cw
from . import config_write_plugins as cwp
from .claude_json import locked_replace_json_file

#: Top-level ``settings.json`` keys owned by a DEDICATED config-write surface.
#: This surface refuses to read or touch them: they are excluded from the read
#: view (a caller wanting them must use their own surface) and a write
#: candidate that names any of them is rejected **whole** (422) rather than
#: silently accepted or silently stripped -- fail closed, mirroring the hooks
#: surface's plugin-owned-hook guard (:mod:`clauster.config_write_hooks`).
#:
#: Each key maps to the dashboard tab that owns it, so a refused Raw JSON write can say
#: where to edit the key instead of a bare "owned by a dedicated surface" (TASK-449.1).
#: ``autoMode`` (the auto mode classifier rules) is a top-level key that the
#: Permissions tab manages beside ``permissions``
#: (:mod:`clauster.config_write_permissions`).
#:
#: Only a key that another tab's editor WRITES is carved (TASK-449.2). ``disabledPlugins`` is not:
#: the Plugins tab never writes it, and Claude Code 2.1.318 does not read it (the name is
#: absent from its bundle). ``enabledMcpjsonServers``/``disabledMcpjsonServers`` are not
#: either: the MCP tab writes approvals to ``~/.claude.json`` and shows a settings-file
#: approval read-only, so the Settings tab is where a settings-file list is edited.
#: ``skillOverrides`` stays here until the Skills tab has an editor for it (TASK-249).
OWNED_KEY_TABS: dict[str, str] = {
    "permissions": "Permissions",
    "autoMode": "Permissions",
    "hooks": "Hooks",
    "enabledPlugins": "Plugins",
}

OWNED_KEYS = frozenset(OWNED_KEY_TABS)

#: Top-level keys whose whole value is a credential helper: a command that prints an API
#: key, cloud credentials or OpenTelemetry headers (settings reference, "Authentication
#: and providers"; ``proxyAuthHelper`` is CLI-parsed in 2.1.318 but undocumented). The
#: command text can itself carry the credential, so each value is masked whole on read,
#: whatever its key name matches (``otelHeadersHelper`` matches no credential-shaped
#: pattern), and a resent mask keeps the stored value.
SECRET_HELPER_KEYS = frozenset(
    {
        "apiKeyHelper",
        "awsAuthRefresh",
        "awsCredentialExport",
        "gcpAuthRefresh",
        "otelHeadersHelper",
        "proxyAuthHelper",
    }
)

#: Where Claude Code reads a scope-limited key: kind -> (the scopes clauster writes that
#: Claude Code reads the key from, the phrase for those sources, how to fix the write).
_READ_FROM: dict[str, tuple[frozenset[str], str, str]] = {
    "user": (
        frozenset({"user"}),
        "user settings",
        "switch to User scope, or edit ~/.claude/settings.json on the host",
    ),
    "user+local": (
        frozenset({"user", "local"}),
        "user and local settings",
        "switch to User or Local scope",
    ),
    "managed": (
        frozenset(),
        "managed settings",
        "your organization deploys managed settings, which clauster does not write; remove it",
    ),
    "global": (
        frozenset(),
        "~/.claude.json (global config)",
        "Claude Code's /config writes it there; remove it from this file",
    ),
}

#: Keys (dotted for a nested key) that Claude Code reads only from some settings files,
#: from the Scope column of https://code.claude.com/docs/en/settings-reference (checked
#: 2026-11-04 against Claude Code 2.1.318), plus three undocumented keys whose own schema
#: text in the 2.1.318 bundle names their scope. A write that names one at a scope where it is
#: ignored is refused whole (422) with a message naming the scope that works, as the
#: Permissions tab does for ``autoMode`` (PR 1801). ``autoMode`` itself is carved above.
#: A value-level caveat (for example a ``mask`` entry in ``sandbox.credentials.envVars``)
#: is not enforced here: the key is read at that scope, only some of its values are not.
SCOPE_LIMITED_KEYS: dict[str, str] = {
    **dict.fromkeys(
        (
            "appendPlugins",
            "askUserQuestionTimeout",
            "autoContinueAtUsageLimit",
            "axScreenReader",
            "bashEditDiffEnabled",
            "desktopSessionCleanupPeriodDays",
            "dialogExpiry",
            "feedbackDrafts",
            "footerLinksRegexes",
            "forceLoginGatewayUrl",
            "modelPicker",
            "pluginConfigs",
            "prependPlugins",
            "processWrapper",
            # CLI-parsed, undocumented: its 2.1.318 schema text says a project, local or
            # --settings value is ignored.
            "remoteTools.allowUnattendedServing",
            "sandbox.allowAppleEvents",
            "sandbox.allowNestedUserNamespaces",
            "sandbox.credentials.allowPlaintextInject",
            "sandbox.credentials.awsPairs",
            "sandbox.credentials.sigv4",
            "sandbox.filesystem.disabled",
            "sandbox.network.strictAllowlist",
            "sandbox.network.tlsTerminate",
            "sandbox.ripgrep",
            "skipAutoPermissionPrompt",
            "spellcheck",
            "sshConfigs",
            "sshHostCommands",
            "vimInsertModeRemaps",
            "worktree.location",
        ),
        "user",
    ),
    **dict.fromkeys(
        (
            "skipDangerousModePermissionPrompt",
            "syncClaudeAiPlugins",
            "syncClaudeAiSkills",
            "useAutoModeDuringPlan",
        ),
        "user+local",
    ),
    **dict.fromkeys(
        (
            "allowAllClaudeAiMcps",
            "allowClaudeInChromeWithManagedMcp",
            "allowManagedHooksOnly",
            "allowManagedMcpServersOnly",
            "allowManagedPermissionRulesOnly",
            "allowedChannelPlugins",
            # CLI-parsed, undocumented: the 2.1.318 schema text says "Alias for
            # strictKnownMarketplaces (managed settings only)".
            "allowedMarketplaces",
            "allowedProviders",
            # CLI-parsed, undocumented: "Only honored from managed settings".
            "attributionProjectPolicy",
            "availableModelsMatch",
            "blockedMarketplaces",
            "browserExternalPageTools",
            "channelsEnabled",
            "claudeMd",
            "deniedModels",
            "disableBrowserExternalNavigation",
            "disableCommandPluginSources",
            "disableDesktopLocalSessions",
            "disableMobileSimulatorTools",
            "disableSideloadFlags",
            "forceRemoteSettingsRefresh",
            "gatewayInternalNetworks",
            "managedMcpServers",
            "managedSourcesBehavior",
            "modelPricing",
            "parentSettingsBehavior",
            "pluginSuggestionMarketplaces",
            "pluginTrustMessage",
            "policyHelper",
            "requiredMaximumVersion",
            "requiredMinimumVersion",
            "sandbox.bwrapPath",
            "sandbox.filesystem.allowManagedReadPathsOnly",
            "sandbox.network.allowManagedDomainsOnly",
            "sandbox.socatPath",
            "sshHostAllowlist",
            "strictKnownMarketplaces",
            "strictPluginOnlyCustomization",
            "wslInheritsWindowsSettings",
        ),
        "managed",
    ),
    **dict.fromkeys(
        (
            "autoConnectIde",
            "autoInstallIdeExtension",
            "claudeInChromeDefaultEnabled",
            "copyFullResponse",
            "copyOnSelect",
            "defaultToAgentsView",
            "diffTool",
            "externalEditorContext",
            "leftArrowOpensAgents",
            "permissionExplainerEnabled",
            "prStatusFooterEnabled",
            "teammateDefaultModel",
        ),
        "global",
    ),
}

#: The file each scope writes, for a refusal message.
_SCOPE_LABEL: dict[str, str] = {
    "user": "user settings (~/.claude/settings.json)",
    "project": "project settings (.claude/settings.json)",
    "local": "local settings (.claude/settings.local.json)",
}

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
        where = "; ".join(f"{key} on the {OWNED_KEY_TABS[key]} tab" for key in sorted(owned))
        raise SettingsCarveError(
            f"settings has keys owned by a dedicated surface: {sorted(owned)}. "
            f"Remove them here and edit {where}"
        )
    if ENV_KEY in candidate:
        _validate_env(candidate[ENV_KEY])


def _has_path(data: Any, dotted: str) -> bool:
    """Whether ``data`` holds the dotted key path ``dotted`` (each step a dict key)."""
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return False
        data = data[part]
    return True


def _ignored_paths(data: dict[str, Any], scope: cw.Scope) -> list[str]:
    """Return the :data:`SCOPE_LIMITED_KEYS` paths in ``data`` ignored at ``scope``."""
    return sorted(
        path
        for path, kind in SCOPE_LIMITED_KEYS.items()
        if scope not in _READ_FROM[kind][0] and _has_path(data, path)
    )


def reject_scope_ignored(candidate: dict[str, Any], scope: cw.Scope) -> None:
    """Refuse (422) a write naming a key Claude Code does not read at ``scope``.

    Saving such a key would show a setting that never takes effect, so the write fails
    closed, whole, with a message that names where Claude Code does read the key. A file
    that already holds one must have it removed (or moved) before this tab saves it again.
    """
    paths = _ignored_paths(candidate, scope)
    if paths:
        raise cw.InvalidCandidateError(
            " ".join(
                f"Claude Code reads {path} only from {_READ_FROM[SCOPE_LIMITED_KEYS[path]][1]} "
                f"and ignores it in {_SCOPE_LABEL[scope]}; "
                f"{_READ_FROM[SCOPE_LIMITED_KEYS[path]][2]}."
                for path in paths
            )
        )


def _without_paths(data: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """Return a copy of ``data`` without each dotted path (copying only the dicts on a path).

    Each parent object left empty by the removal is dropped too, so the merged view never
    names a scope for a key whose every part that scope sets is ignored there.
    """
    out = dict(data)
    for dotted in paths:
        *parents, leaf = dotted.split(".")
        chain = [out]
        for part in parents:
            chain[-1][part] = dict(chain[-1][part])
            chain.append(chain[-1][part])
        del chain[-1][leaf]
        for depth in range(len(parents), 0, -1):
            if chain[depth]:
                break
            del chain[depth - 1][parents[depth - 1]]
    return out


def _misc_view(data: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of ``data`` this surface manages (non-owned keys)."""
    return {k: v for k, v in data.items() if k not in OWNED_KEYS}


def _redact_misc(misc: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted display copy of ``misc`` (the read-side masking rule).

    Runs the normal structural :func:`~clauster.config_write.redact_secrets`
    heuristic over every key, then unconditionally masks every ``env`` value on
    top -- the #822-lesson override described in the module docstring -- masks each
    :data:`SECRET_HELPER_KEYS` value whole, and
    redacts ``extraKnownMarketplaces`` (and its ``additionalMarketplaces`` alias)
    with :func:`clauster.config_write_plugins.redact_named_map`.
    """
    redacted = cw.redact_secrets(misc)
    env = misc.get(ENV_KEY)
    if isinstance(env, dict):
        redacted[ENV_KEY] = dict.fromkeys(env, cw.REDACTION_SENTINEL)
    # A credential helper's command can hold the credential itself, so it is masked whole,
    # whatever its value's shape. A resent sentinel keeps the stored value.
    for key in SECRET_HELPER_KEYS & misc.keys():
        redacted[key] = cw.REDACTION_SENTINEL
    # A marketplace declaration's `headers`/`headersHelper` hold credentials under plain
    # names (`Cookie`), so it gets the Plugins surface's masking, not only the key-name
    # heuristic. A resent sentinel keeps the stored value through `merge_redacted`.
    for key in (cwp.EXTRA_MARKETPLACES_KEY, cwp.EXTRA_MARKETPLACES_ALIAS_KEY):
        declared = misc.get(key)
        if isinstance(declared, dict):
            redacted[key] = cwp.redact_named_map(declared)
        elif declared is not None:
            # Malformed (not the documented map), but it can still carry credentials.
            redacted[key] = cwp.redact_plugin_display(declared)
    return redacted


_MISSING = object()


def _has_sentinel(value: Any) -> bool:
    """Whether ``value`` holds a :data:`~clauster.config_write.REDACTION_SENTINEL` at any depth."""
    if isinstance(value, dict):
        return any(_has_sentinel(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_sentinel(v) for v in value)
    return value == cw.REDACTION_SENTINEL


def _unchanged_but_masks(incoming: Any, stored: Any) -> bool:
    """Whether ``incoming`` equals ``stored`` everywhere except where it holds a sentinel."""
    if incoming == cw.REDACTION_SENTINEL:
        return True
    if isinstance(incoming, dict):
        return (
            isinstance(stored, dict)
            and incoming.keys() == stored.keys()
            and all(_unchanged_but_masks(v, stored[k]) for k, v in incoming.items())
        )
    if isinstance(incoming, list):
        return (
            isinstance(stored, list)
            and len(incoming) == len(stored)
            and all(_unchanged_but_masks(v, s) for v, s in zip(incoming, stored, strict=True))
        )
    return bool(incoming == stored)


def _orphan_sentinel_path(incoming: Any, stored: Any, path: str) -> str | None:
    """Return the path of the first sentinel in ``incoming`` that cannot be matched safely.

    :func:`~clauster.config_write.merge_redacted` silently DROPS a sentinel with no
    stored value behind it, and restores a list by POSITION. So renaming a masked
    marketplace entry or header on the Settings tab would save it with its credential
    gone, and deleting or reordering an element of a list (a ``settings`` source's
    ``plugins``) would move one entry's credential into another and report success.
    A dict is matched by key. A list that holds a sentinel must keep its length, and
    each element that holds one must come back unchanged apart from its sentinels.
    """
    if isinstance(incoming, dict):
        items: Any = incoming.items()
    elif isinstance(incoming, list):
        if _has_sentinel(incoming):
            if not isinstance(stored, list) or len(stored) != len(incoming):
                return path
            for i, v in enumerate(incoming):
                if _has_sentinel(v) and not _unchanged_but_masks(v, stored[i]):
                    return f"{path}.{i}"
        items = enumerate(incoming)
    else:
        return None
    for k, v in items:
        if isinstance(incoming, dict):
            here = stored.get(k, _MISSING) if isinstance(stored, dict) else _MISSING
        else:
            here = stored[k] if isinstance(stored, list) and k < len(stored) else _MISSING
        where = f"{path}.{k}"
        if v == cw.REDACTION_SENTINEL and here is _MISSING:
            return where
        found = _orphan_sentinel_path(v, None if here is _MISSING else here, where)
        if found:
            return found
    return None


def _unmasked_slot_path(incoming: Any, shown: Any, path: str) -> str | None:
    """Return the path of the first sentinel in ``incoming`` where the read view showed none.

    ``shown`` is the redacted view of the stored value. A sentinel is only a "keep the
    stored value" marker where the read put one, so a mask moved onto another slot (a
    list reordered so the mask lands on a clear entry) is caught here: the positional
    restore would otherwise copy that clear entry and drop the masked credential.

    Masks all look the same, so in a list that still holds one, every element the read
    showed masked must still hold a mask in the same position. Otherwise deleting one
    masked entry and appending another value would keep the deleted credential and drop
    the kept one. To change one such element, retype every masked entry of the list. That
    case raises :class:`~clauster.config_write.InvalidCandidateError` here, with a message
    that names the list, since no single slot is at fault.
    """
    if incoming == cw.REDACTION_SENTINEL:
        return None if shown == cw.REDACTION_SENTINEL else path
    if isinstance(incoming, dict):
        pairs: Any = (
            (k, v, shown.get(k, _MISSING) if isinstance(shown, dict) else _MISSING)
            for k, v in incoming.items()
        )
    elif isinstance(incoming, list):
        # The orphan check ran first, so ``incoming`` has the stored (and shown) length.
        if isinstance(shown, list) and _has_sentinel(incoming):
            if any(
                _has_sentinel(s) and not _has_sentinel(v)
                for s, v in zip(shown, incoming, strict=False)
            ):
                raise cw.InvalidCandidateError(
                    f"{path} holds masked entries that all look the same, so after you "
                    f"change one, type the real value of every masked entry in {path}"
                )
        pairs = (
            (i, v, shown[i] if isinstance(shown, list) and i < len(shown) else _MISSING)
            for i, v in enumerate(incoming)
        )
    else:
        return None
    for k, v, sub in pairs:
        found = _unmasked_slot_path(v, sub, f"{path}.{k}")
        if found:
            return found
    return None


def _reject_orphan_sentinels(incoming: dict[str, Any], stored: dict[str, Any]) -> None:
    """Refuse (422) a write whose masked values no longer match a stored value.

    Covers every key of the partition, not only the marketplace maps (TASK-449.2): a
    renamed ``env`` variable or a credential helper sent as a mask to a file that has none
    would otherwise be dropped by :func:`~clauster.config_write.merge_redacted` and the
    save would report success. Each mask must also sit where the read view of the stored
    file shows one (:func:`_unmasked_slot_path`).
    """
    shown = _redact_misc(stored)
    for key in sorted(incoming):
        if incoming[key] == cw.REDACTION_SENTINEL and key not in stored:
            where: str | None = key
        else:
            where = _orphan_sentinel_path(
                incoming[key], stored.get(key), key
            ) or _unmasked_slot_path(incoming[key], shown.get(key, _MISSING), key)
        if where:
            raise cw.InvalidCandidateError(
                f"{where} holds a masked value that no longer matches its stored value (an "
                "entry or key was renamed, moved or reordered, or a list changed length); "
                "enter the real value again"
            )
    for key in (cwp.EXTRA_MARKETPLACES_KEY, cwp.EXTRA_MARKETPLACES_ALIAS_KEY):
        if key in incoming:
            _reject_masks_sent_to_a_new_url(incoming[key], stored.get(key), key)


def _urls(value: Any, path: tuple[Any, ...] = ()) -> dict[tuple[Any, ...], Any]:
    """Return every ``url`` value under ``value``, at any depth, keyed by its path."""
    found: dict[tuple[Any, ...], Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "url":
                found[(*path, k)] = v
            found.update(_urls(v, (*path, k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found.update(_urls(v, (*path, i)))
    return found


def _reject_masks_sent_to_a_new_url(incoming: Any, stored: Any, key: str) -> None:
    """Refuse (422) a marketplace entry that keeps masked values while any of its urls changes.

    A masked ``headers`` value or ``headersHelper`` is restored from the stored entry by
    key. If a ``url`` in that entry (its ``source.url``, or a nested plugin's) now differs
    from the stored one, restoring would send the stored credential to a new host. So every
    masked value in such an entry is refused whole, and the operator retypes the
    credential. The comparison is the EXACT string: any parsed "same destination" rule
    is a second URL parser that can disagree with the one Claude Code fetches with (a
    backslash before ``@`` names a different host to Python's ``urlsplit`` than to a
    WHATWG parser), and a disagreement there would leak the credential. A url that is
    itself masked was resent unchanged, so it still matches.
    """
    if not isinstance(incoming, dict):
        return
    stored_map = stored if isinstance(stored, dict) else {}
    for name, entry in incoming.items():
        if not _has_sentinel(entry):
            continue
        new_urls = _urls(entry)
        old_urls = _urls(stored_map.get(name))
        moved = any(
            url != cw.REDACTION_SENTINEL and old_urls.get(where, _MISSING) != url
            for where, url in new_urls.items()
        ) or any(where not in new_urls for where in old_urls)
        if moved:
            raise cw.InvalidCandidateError(
                f"{key}.{name}: a marketplace url changed, so its masked credentials cannot "
                "be kept; retype the credential for the new url"
            )


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
    string. Every key in :data:`OWNED_KEYS` is carried through with its value
    unchanged (the whole object is re-rendered, so byte layout and key order are
    not preserved); the entire non-owned partition is replaced by the merged result
    (a full-replacement subtree write, not a key-by-key patch of unspecified
    keys the caller never mentioned).
    """

    def _mutate(current_bytes: bytes) -> dict[str, Any]:
        """Check the expected hash, then swap in the merged non-owned partition."""
        if expected_hash is None:
            if current_bytes:
                raise cw.StaleConfigWriteError(f"{path.name} already exists; a hash is required")
        elif cw.hash_bytes(current_bytes) != expected_hash:
            raise cw.StaleConfigWriteError("config file changed on disk since it was loaded")
        current = cw.load_settings_json_obj(current_bytes)
        stored_misc = _misc_view(current)
        _reject_orphan_sentinels(incoming, stored_misc)
        merged_misc = cw.merge_redacted(incoming, stored_misc)
        # Replace the whole non-owned partition; every owned key keeps its value
        # (the file is re-rendered, so byte layout and key order are not preserved).
        for key in list(current):
            if key not in OWNED_KEYS:
                del current[key]
        current.update(merged_misc)
        return current

    locked_replace_json_file(path, _mutate, render=cw.render_json)


def read_project_settings(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(redacted_misc, content_hash)`` for a project's ``.claude/settings.json``.

    The misc partition only (every :data:`OWNED_KEYS` member is dropped), with every
    ``env`` value masked -- see :func:`_read_misc`.
    """
    return _read_misc(cw.project_settings_path(project_dir))


def write_project_settings(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the project ``.claude/settings.json`` misc partition.

    Ensures the ``<project>/.claude`` parent exists (so a first write to a
    project that has no ``.claude`` dir yet does not fail the atomic writer's
    ``mkstemp`` in a missing directory), then runs the fail-closed Foundation
    pipeline. The candidate is validated *before* the directory is created, so
    a bad shape (422) leaves the filesystem untouched, as does a key Claude Code ignores
    at project scope (:func:`reject_scope_ignored`). A masked value that no
    longer matches its stored value is refused inside the lock instead (also 422); the
    settings file is unchanged, though a new ``.claude`` directory may be left behind.
    """
    cw.validate_candidate(incoming, validate_misc_settings)
    reject_scope_ignored(incoming, "project")
    path = cw.project_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _locked_write_misc(path, incoming, expected_hash)


def read_user_settings(settings_json: Path) -> tuple[dict[str, Any], str]:
    """Return ``(redacted_misc, content_hash)`` for the user-scope ``~/.claude/settings.json``.

    The misc partition only (every :data:`OWNED_KEYS` member is dropped), with every
    ``env`` value masked -- see :func:`_read_misc`.
    """
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
    reject_scope_ignored(incoming, "user")
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    _locked_write_misc(settings_json, incoming, expected_hash)


def read_project_local_settings(project_dir: Path) -> tuple[dict[str, Any], str]:
    """Return ``(redacted_misc, content_hash)`` for a project's ``settings.local.json``.

    The misc partition only (every :data:`OWNED_KEYS` member is dropped), with every
    ``env`` value masked -- see :func:`_read_misc`.
    """
    return _read_misc(cw.project_local_settings_path(project_dir))


def write_project_local_settings(
    project_dir: Path, incoming: dict[str, Any], expected_hash: str | None
) -> None:
    """Validate + write the local-scope ``.claude/settings.local.json`` misc partition.

    Third (local) scope, sibling of the project/user writers above: same
    fail-closed Foundation pipeline and stale-hash guard, targeting a *third*
    file that is you, this project only. :func:`~clauster.config_write.ensure_gitignored`
    runs *before* the write (fail-closed: the write creates a secret-bearing ``.bak``),
    so ``settings.local.json`` and that backup are never accidentally committed (#766) --
    idempotent, so an already-gitignored file is a no-op there.
    """
    cw.validate_candidate(incoming, validate_misc_settings)
    reject_scope_ignored(incoming, "local")
    path = cw.project_local_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ignore BEFORE writing, never after: the write creates a `.bak` holding the PREVIOUS
    # env values, so an ensure_gitignored that failed afterwards would leave that
    # secret-bearing file trackable. Ignoring first is the fail-closed order — a write that
    # then fails leaves only a harmless extra .gitignore line (the call is idempotent).
    cw.ensure_gitignored(project_dir, ".claude/settings.local.json", ignore_backup_sibling=True)
    _locked_write_misc(path, incoming, expected_hash)


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
    the full chain and the docs citation) -- and the merge is Claude Code's own
    (:func:`_merge_setting`): objects merge key by key, lists combine, and a scalar
    from a higher scope replaces a lower one. A key Claude Code ignores at a scope
    (:data:`SCOPE_LIMITED_KEYS`) is left out of that scope's layer. Returns
    ``{key: {"value": ..., "source": <highest scope>, "sources": [<scopes>]}}`` for the
    union of keys across every scope that was read; ``sources`` lists, highest first,
    each scope whose value is part of the merged result.
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
            layer = _without_paths(misc, _ignored_paths(misc, scope))
            # Claude Code reads the alias as the canonical key in each file before it
            # merges: a null alias is dropped, and an alias is ignored where the file
            # also sets the canonical key to a non-null value.
            alias = layer.pop(cwp.EXTRA_MARKETPLACES_ALIAS_KEY, None)
            if alias is not None and layer.get(cwp.EXTRA_MARKETPLACES_KEY) is None:
                layer[cwp.EXTRA_MARKETPLACES_KEY] = alias
            layers.append((scope, layer))

    # ``layers`` is in precedence order (local, project, user). Walk it reversed so
    # each higher scope merges OVER the lower ones, as Claude Code applies them.
    effective: dict[str, dict[str, Any]] = {}
    for scope, misc in reversed(layers):
        for key, value in misc.items():
            below = effective.get(key)
            if below is None:
                effective[key] = {"value": value, "source": scope, "sources": [scope]}
                continue
            merged = _merge_setting(below["value"], value, key)
            sources = [scope] if merged == value else [scope, *below["sources"]]
            effective[key] = {"value": merged, "source": scope, "sources": sources}
    return {key: effective[key] for key in sorted(effective)}


#: Keys whose object value Claude Code merges one entry deep: a higher scope's entry
#: replaces the lower scope's entry of the same name whole. (Its customizer does the same
#: for ``managedMcpServers``, which only managed settings may set.)
_SHALLOW_MERGE_KEYS = frozenset({cwp.EXTRA_MARKETPLACES_KEY})


def _js_kind(value: Any) -> str:
    """Return the JavaScript type name of a parsed JSON scalar (a number is one kind)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _merge_setting(lower: Any, higher: Any, key: Any) -> Any:
    """Merge one settings value from a higher scope over a lower one, as Claude Code does.

    Mirrors the merge customizer in Claude Code 2.1.318 (and the settings docs, "Lists
    merge instead of overriding"): two lists combine, lower entries first, with repeated
    plain values kept once; ``fallbackModel`` is an ordered chain, so the higher list
    replaces it; ``extraKnownMarketplaces`` (each layer's alias already renamed to it)
    merges one entry deep; two objects merge key by key;
    anything else is replaced by the higher value. The rule
    applies at every depth, as the customizer receives each nested key. (The customizer
    also takes ``modelPicker`` whole, but only user settings may set it, so among the
    three scopes here it never merges.) A masked value is never collapsed into another,
    because two masks can stand for different stored values.
    """
    if isinstance(lower, list) and isinstance(higher, list):
        if key == "fallbackModel":
            return higher
        out: list[Any] = []
        for item in [*lower, *higher]:
            plain = not isinstance(item, (dict, list)) and item != cw.REDACTION_SENTINEL
            # Typed equality as in JavaScript: ``1 == True`` must not merge two entries,
            # while ``1`` and ``1.0`` are the same number.
            if not (plain and any(_js_kind(o) == _js_kind(item) and o == item for o in out)):
                out.append(item)
        return out
    if isinstance(lower, dict) and isinstance(higher, dict):
        if key in _SHALLOW_MERGE_KEYS:
            return {**lower, **higher}
        merged = dict(lower)
        for k, v in higher.items():
            merged[k] = _merge_setting(lower[k], v, k) if k in lower else v
        return merged
    return higher
