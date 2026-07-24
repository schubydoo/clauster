"""Skills config-write surface (#691) over the #347 Foundation + #766 file/dir writer.

Sibling of the hooks (#690) and CLAUDE.md (#768) surfaces, but the *directory* twin
of both: a skill is ``<root>/<name>/SKILL.md`` plus optional supporting files
(``scripts/*.sh``, reference docs, templates) — the shape :mod:`clauster.
config_file_writer` (#766) was built for. This module owns the skill-specific
structural validation and the atomic create/replace/delete calls into that
primitive; it does not re-implement containment, locking, or atomicity itself.

**Two independent settings, one directory:**

* The **directory** (``SKILL.md`` + supporting files) lives at **user**
  (``~/.claude/skills/<name>/``) or **project** (``<project>/.claude/skills/<name>/``)
  scope only — Claude Code has no "local" skills directory, so this surface does not
  invent one (see the surface matrix, ``scratch/config-management-expansion-design-
  2026-06-29.md`` §3).
* **Visibility** (enable/disable) is a *settings* concern: Claude Code
  v2.1.129+'s ``skillOverrides`` key in ``settings.json`` (verified against the
  shipped docs — https://code.claude.com/docs/en/skills, https://code.claude.com/
  docs/en/settings), mapping a skill name to one of :data:`SKILL_OVERRIDE_VALUES`
  (``"on"``, ``"name-only"``, ``"user-invocable-only"``, ``"off"``). Because it is an
  ordinary settings key it gets all three scopes (user/project/**local**), exactly
  like :mod:`clauster.config_write_hooks`' ``hooks`` key — this module's overrides
  functions are that module's pattern renamed. The separate, coarser
  ``disable-model-invocation: true`` SKILL.md frontmatter lever is a per-skill-file
  author choice, not something this settings-based surface writes.

**Plugin skills are read-only here — by construction, not by a special case.**
A plugin's skill physically lives inside the plugin's own install tree
(``~/.claude/plugins/cache/...`` — see https://code.claude.com/docs/en/plugins-
reference), never under ``~/.claude/skills/`` or ``<project>/.claude/skills/``. Three
independent guards make "read-only" a structural guarantee rather than a lookup this
module has to get right:

1. **Root confinement.** Every operation resolves through
   :func:`~clauster.config_file_writer.resolve_contained_path` against exactly the
   user/project skills root — a plugin's install directory is categorically outside
   both roots, so it is unreachable through this surface at all, the same reasoning
   :mod:`clauster.config_write_hooks` uses for plugin ``hooks/hooks.json`` (a file
   this surface never opens).
2. **Symlink defense.** :func:`~clauster.config_file_writer.resolve_contained_path`
   resolves symlinks before the containment check, so a skill-name entry crafted as a
   symlink into the plugin cache (to *look like* a normal skill while shadowing
   plugin content) is rejected before any I/O — this also closes the "symlink escape"
   containment case for uploaded member paths.
3. **Content-marker guard (defense in depth, mirrors #770).** ``${CLAUDE_PLUGIN_ROOT}``
   only resolves inside a plugin's own bundle; any uploaded ``SKILL.md`` or script body
   referencing it did not originate as project/user content, so the whole write is
   rejected (422) — see :data:`_PLUGIN_ROOT_MARKER`.

**Scripts are opaque, behind an EXTRA confirm.** A skill's non-``SKILL.md`` files
(``scripts/*.sh``, etc.) are validated for *shape only* (string, size cap, no
plugin-root marker) — **never** parsed, resolved, shell-checked, or executed; the
validate-never-execute invariant applies here exactly as it does to a hook
``command``. Because the browser is the one place an *executable* skill script can
be authored end-to-end (unlike a hook command, which at least requires an operator to
already trust the settings.json surface), an upload that includes anything besides
``SKILL.md`` must additionally echo back the literal :data:`SCRIPT_CONFIRM_TOKEN` —
a second, distinct type-the-phrase gate on top of the Foundation's type-the-name
confirm, required only when script bodies are actually present. Uploading `just`
``SKILL.md`` (no scripts) needs only the ordinary Foundation confirm.

**Read redaction (closes the #813 INFO-1 gap).** Free-text skill content — an
operator-authored ``SKILL.md`` or, especially, a script — is far more likely to carry
an inline credential than ``CLAUDE.md`` prose (the surface :mod:`clauster.claude_md`
deliberately does NOT redact, since it is pure memory/prose never containing
executable material). Skills are executable-adjacent and often copy-pasted from
elsewhere, so every read path here runs its content through
:func:`~clauster.config_write.redact_secret_lines` — the line-oriented redaction the
#766 primitive shipped for exactly this case — before returning it:
:func:`read_skill_file` for a file body, and :func:`list_skills` for the surfaced
``description`` / ``frontmatter_error`` metadata. This is a deliberate, DIFFERENT
choice than CLAUDE.md's, not an oversight.

**Stale-hash guard scope (a documented simplification).** The guard compares against
the hash of ``SKILL.md`` content only, not a whole-tree hash — mirroring
:mod:`clauster.claude_md`'s single-file precedent. A concurrent edit that changes
only a sibling script file (leaving ``SKILL.md`` untouched) is not caught by this
guard; documented here rather than silently assumed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from . import config_file_writer as fw
from . import config_write as cw

#: The skill's required entrypoint file (Claude Code's own convention).
SKILL_FILENAME = "SKILL.md"

#: The directory holding all skills at a given scope's ``.claude`` base.
SKILLS_DIRNAME = "skills"

#: Skill directory *name* shape: alnum-first (rejects a leading ``-``, which some
#: shells/CLIs would otherwise misparse as a flag), then alnum/underscore/hyphen only
#: — no ``.`` at all, which structurally forbids ``.`` and ``..`` as a whole name and
#: matches ``discovery.PROJECT_NAME_RE``'s conservative shape. Path separators are
#: never in the allowed character class either.
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: Size caps — generous but bounded (DoS/disk-fill guard, not a documented Claude Code
#: limit). ``SKILL.md`` mirrors the CLAUDE.md cap for consistency; a supporting file
#: gets a larger allowance since scripts run longer than the frontmatter+instructions.
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)

#: See the module docstring's "Plugin skills are read-only" section.
_PLUGIN_ROOT_MARKER = "CLAUDE_PLUGIN_ROOT"

#: The literal phrase the caller must echo back to write a skill whose upload
#: includes anything beyond ``SKILL.md`` (a script or other supporting file). Public,
#: not a secret — the same "type it back" idiom as
#: :data:`~clauster.config_write.USER_SCOPE_TOKEN`.
SCRIPT_CONFIRM_TOKEN = "I HAVE REVIEWED THESE SCRIPTS"  # noqa: S105 - public confirm literal

#: The settings key holding the skill-visibility override map.
SKILL_OVERRIDES_KEY = "skillOverrides"

#: The only values Claude Code accepts for a ``skillOverrides`` entry (verified
#: against https://code.claude.com/docs/en/skills and .../settings, v2.1.129+).
SKILL_OVERRIDE_VALUES = frozenset({"on", "name-only", "user-invocable-only", "off"})


class ScriptConfirmRequiredError(cw.ConfigWriteError):
    """A skill upload includes non-``SKILL.md`` files without :data:`SCRIPT_CONFIRM_TOKEN`."""


def is_valid_skill_name(name: str) -> bool:
    """Return whether ``name`` is a safe skill directory name.

    Structural pre-check ahead of (never a substitute for)
    :func:`~clauster.config_file_writer.resolve_contained_path` — rejects ``..``,
    path separators, a leading ``-``, and any ``.`` outright, at the name level,
    before a single path is ever resolved.
    """
    return bool(SKILL_NAME_RE.fullmatch(name))


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse ``SKILL.md``'s leading ``---``-delimited YAML frontmatter.

    Returns ``(frontmatter, body)``. STRUCTURE ONLY — the YAML is loaded with
    ``safe_load`` and never evaluated/executed; a missing/malformed frontmatter block
    raises :class:`~clauster.config_write.InvalidCandidateError`.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} must start with a '---' YAML frontmatter block"
        )
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} frontmatter is not valid YAML: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise cw.InvalidCandidateError(f"{SKILL_FILENAME} frontmatter must be a YAML mapping")
    return data, match.group(2)


def validate_frontmatter(candidate: Any) -> None:
    """Reject ``candidate`` unless it is a structurally valid SKILL.md frontmatter dict.

    ``description`` is the one required field (Claude Code always needs it to decide
    when to invoke the skill). Every recognized key is optional and type-checked only.
    An UNRECOGNIZED key is passed through untouched rather than rejected: Claude Code
    tolerates forward-compatible SKILL.md frontmatter (real skills carry keys such as
    ``effort`` / ``license`` / ``metadata``), so a hardcoded allowlist produced false
    "unknown key" errors on valid skills (#958/DF-3). STRUCTURE ONLY — nothing here is
    ever resolved or executed, including ``allowed-tools``.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError(f"{SKILL_FILENAME} frontmatter must be an object")
    description = candidate.get("description")
    if not isinstance(description, str) or not description.strip():
        raise cw.InvalidCandidateError(f"{SKILL_FILENAME} frontmatter 'description' is required")
    if "name" in candidate:
        name = candidate["name"]
        if not isinstance(name, str) or not is_valid_skill_name(name):
            raise cw.InvalidCandidateError(f"{SKILL_FILENAME} frontmatter 'name' is invalid")
    for bool_key in ("disable-model-invocation", "user-invocable"):
        if bool_key in candidate and not isinstance(candidate[bool_key], bool):
            raise cw.InvalidCandidateError(
                f"{SKILL_FILENAME} frontmatter {bool_key!r} must be a bool"
            )
    for str_key in ("allowed-tools", "argument-hint"):
        if str_key in candidate and not isinstance(candidate[str_key], str):
            raise cw.InvalidCandidateError(
                f"{SKILL_FILENAME} frontmatter {str_key!r} must be a string"
            )


def _check_no_plugin_marker(content: str, where: str) -> None:
    """Fail closed (mirrors #770) on content referencing the plugin-root interpolation.

    ``${CLAUDE_PLUGIN_ROOT}`` only resolves inside a plugin's own bundle; its presence
    marks the content as plugin-owned (copied from, or intending to shadow, a plugin's
    skill), never legitimate project/user content.
    """
    if _PLUGIN_ROOT_MARKER in content:
        raise cw.InvalidCandidateError(
            f"{where} references a plugin-owned path ({_PLUGIN_ROOT_MARKER}); "
            "plugin skills are read-only here — manage them via the plugin"
        )


def validate_skill_md_content(candidate: Any) -> None:
    """Structural validator for a ``SKILL.md`` body: size cap + valid frontmatter.

    STRUCTURE ONLY — the body text (everything after the frontmatter) is never
    parsed, resolved, or executed; only the frontmatter's shape is checked.
    """
    if not isinstance(candidate, str):
        raise cw.InvalidCandidateError(f"{SKILL_FILENAME} content must be a string")
    size = len(candidate.encode("utf-8"))
    if size > MAX_SKILL_MD_BYTES:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} is {size} bytes, over the {MAX_SKILL_MD_BYTES} byte cap"
        )
    _check_no_plugin_marker(candidate, SKILL_FILENAME)
    frontmatter, _body = parse_frontmatter(candidate)
    validate_frontmatter(frontmatter)


def validate_script_body(candidate: Any, relative: str) -> None:
    """Structural validator for a non-``SKILL.md`` skill file: an OPAQUE text blob.

    ``candidate`` must be a string under :data:`MAX_FILE_BYTES`. **Never** parsed,
    shell-checked, or executed — validating (and storing) a script is fine; running it
    even to "check" it would be the RCE this gate exists to prevent. The one content
    check applied is the plugin-marker guard, identical in spirit to
    :func:`validate_skill_md_content`'s.
    """
    if not isinstance(candidate, str):
        raise cw.InvalidCandidateError(f"{relative!r} content must be a string")
    size = len(candidate.encode("utf-8"))
    if size > MAX_FILE_BYTES:
        raise cw.InvalidCandidateError(
            f"{relative!r} is {size} bytes, over the {MAX_FILE_BYTES} byte cap"
        )
    _check_no_plugin_marker(candidate, relative)


def _skills_root(base: Path) -> Path:
    """Return the skills directory under a scope's ``.claude`` base."""
    return base / SKILLS_DIRNAME


def _is_contained_regular_file(path: Path, root_resolved: Path) -> bool:
    """Whether ``path`` is a regular file that really lives inside ``root_resolved``.

    Symlink-safe member gate for :func:`list_skills`' file enumeration: rejects a
    symlink outright (a symlink-to-file passes ``is_file()`` but must never be listed —
    it could point at, and later leak, an outside file) AND rejects any path whose
    resolved real location escapes ``root_resolved`` (catching a regular file sitting
    under a symlinked intermediate directory). ``root_resolved`` is the *already
    resolved* skill directory, so the containment check is a pure parent comparison.
    A vanished/broken entry (``OSError`` from ``resolve``) fails closed as not-listable.
    """
    if path.is_symlink() or not path.is_file():
        return False
    try:
        real = path.resolve()
    except OSError:
        return False
    return real == root_resolved or root_resolved in real.parents


def _skill_md_hash(skills_root: Path, name: str) -> str:
    """Return the hash of the current on-disk ``SKILL.md`` bytes (empty digest if absent)."""
    try:
        target = fw.resolve_contained_path(skills_root, f"{name}/{SKILL_FILENAME}")
    except fw.PathEscapeError as exc:
        raise cw.PathEscapeError(str(exc)) from exc
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        data = b""
    return cw.hash_bytes(data)


def list_skills(base: Path) -> list[dict[str, Any]]:
    """Return a listing of every valid-named skill directory under ``base``'s skills root.

    Each entry: ``{"name", "has_skill_md", "files", "description"?, "disable_model_
    invocation"?, "frontmatter_error"?}``. A skill whose ``SKILL.md`` fails structural
    validation is still listed (so a hand-edited/corrupt skill is visible, not hidden)
    with ``frontmatter_error`` set instead of raising — a single bad skill must never
    break the whole listing.

    **Redaction (consistency with the file-body read view).** Both the surfaced
    ``description`` and any ``frontmatter_error`` fragment run through
    :func:`~clauster.config_write.redact_secret_lines` before entering the listing —
    a secret pasted into a ``description:`` line, or echoed back inside a YAML
    parse-error message, is masked exactly as it would be on the
    :func:`read_skill_file` body view, so this surface's redaction invariant holds on
    every read path, not just the file-body one.
    """
    skills_root = _skills_root(base)
    if not skills_root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(skills_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.is_symlink() or not is_valid_skill_name(entry.name):
            continue
        skill_md = entry / SKILL_FILENAME
        # A *symlinked* SKILL.md is refused, never followed — consistent with skipping
        # symlinked skill DIRS above. ``is_file()`` follows symlinks (True for a
        # symlink-to-file), so a symlinked SKILL.md pointing out of the tree would
        # otherwise have its target content read; requiring a non-symlink regular file
        # closes that. A symlinked entrypoint reads as "no SKILL.md" (nothing surfaced).
        has_skill_md = skill_md.is_file() and not skill_md.is_symlink()
        item: dict[str, Any] = {"name": entry.name, "has_skill_md": has_skill_md}
        if has_skill_md:
            try:
                text = skill_md.read_bytes().decode("utf-8")
                frontmatter, _body = parse_frontmatter(text)
                validate_frontmatter(frontmatter)
            except cw.InvalidCandidateError as exc:
                # The error string can quote a fragment of the offending frontmatter
                # (a bad YAML line), so redact it the same way the body view is.
                item["frontmatter_error"] = cw.redact_secret_lines(str(exc))
            except UnicodeDecodeError:
                item["frontmatter_error"] = f"{SKILL_FILENAME} is not valid UTF-8"
            else:
                description = frontmatter.get("description")
                # description is a validated non-empty str here, but guard anyway so a
                # future validator change can't feed a non-str into redact_secret_lines.
                item["description"] = (
                    cw.redact_secret_lines(description)
                    if isinstance(description, str)
                    else description
                )
                item["disable_model_invocation"] = bool(
                    frontmatter.get("disable-model-invocation", False)
                )
        # Member enumeration is symlink-SAFE: a symlinked member (or one under a
        # symlinked subdir) is skipped, so a symlink pointing at an outside file is
        # never listed — and therefore never offered up for a later read that would leak
        # its target's content. Keys are normalized to forward slashes (``as_posix``) so
        # the API returns ``scripts/x.sh`` on every OS — these are logical member keys,
        # not host filesystem paths.
        entry_resolved = entry.resolve()
        item["files"] = sorted(
            p.relative_to(entry).as_posix()
            for p in entry.rglob("*")
            if p != skill_md and _is_contained_regular_file(p, entry_resolved)
        )
        out.append(item)
    return out


def read_skill_file(
    base: Path, name: str, relative: str = SKILL_FILENAME
) -> tuple[str, str, bool]:
    """Return ``(redacted_content, hash, exists)`` for one file inside a skill directory.

    Path-contained twice over: the skill ``name`` and the ``relative`` member path
    both go through :func:`~clauster.config_file_writer.resolve_contained_path`, so
    neither a crafted name nor a crafted member path (``../secrets``) can escape the
    skills root.

    The file is read **exactly once** — the returned (redacted) content and the hash
    are both derived from that single ``read_bytes`` (the hash over the RAW,
    unredacted bytes; the content through
    :func:`~clauster.config_write.redact_secret_lines`). Reading twice — once for the
    hash, once via ``read_file`` for the content — would open a TOCTOU window where the
    bytes the hash describes and the bytes returned could differ; this closes it. The
    hash matching the raw bytes is exactly what a subsequent write's stale-hash guard
    compares against. See the module docstring for the read-redaction decision.
    """
    if not is_valid_skill_name(name):
        raise cw.PathEscapeError(f"invalid skill name: {name!r}")
    skills_root = _skills_root(base)
    try:
        target = fw.resolve_contained_path(skills_root, f"{name}/{relative}")
    except fw.PathEscapeError as exc:
        raise cw.PathEscapeError(str(exc)) from exc
    try:
        raw = target.read_bytes()
        exists = True
    except FileNotFoundError:
        raw = b""
        exists = False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise cw.InvalidCandidateError(f"{relative} is not valid UTF-8") from exc
    # Redact and hash from the SAME bytes (single read) — no second read to drift from.
    content = cw.redact_secret_lines(text)
    return content, cw.hash_bytes(raw), exists


def write_skill(
    base: Path,
    name: str,
    files: dict[str, Any],
    *,
    expected_hash: str | None,
    confirm_scripts: str | None = None,
) -> None:
    """Validate + atomically create/replace the skill directory ``base/skills/<name>``.

    ``files`` maps a relative member path to its (text) content and MUST include
    ``SKILL.md``. Any OTHER key present requires ``confirm_scripts`` to equal
    :data:`SCRIPT_CONFIRM_TOKEN` exactly (:class:`ScriptConfirmRequiredError`
    otherwise, before any I/O) — see the module docstring. Every file is validated
    for shape only and NEVER executed: :func:`validate_skill_md_content` for
    ``SKILL.md``, :func:`validate_script_body` for everything else. Member paths are
    containment-checked one by one inside the atomic build (each is resolved through
    :func:`~clauster.config_file_writer.resolve_contained_path` against the staging
    dir before it is written) — a ``../`` or absolute member path aborts the whole
    write before any promote. The build writes members directly rather than via
    :func:`~clauster.config_file_writer.write_file` because the staging dir is private
    and uncontested until ``replace_tree`` promotes it under one cross-process lock, so
    per-member locking would be pure redundant overhead — see the inline comment in
    ``_build``.
    """
    if not is_valid_skill_name(name):
        raise cw.PathEscapeError(f"invalid skill name: {name!r}")
    if not isinstance(files, dict) or not files:
        raise cw.InvalidCandidateError("files must be a non-empty object")
    if SKILL_FILENAME not in files:
        raise cw.InvalidCandidateError(f"files must include {SKILL_FILENAME!r}")

    extra_files = [k for k in files if k != SKILL_FILENAME]
    if extra_files and confirm_scripts != SCRIPT_CONFIRM_TOKEN:
        raise ScriptConfirmRequiredError(
            "uploading files alongside SKILL.md requires confirm_scripts to equal "
            f"{SCRIPT_CONFIRM_TOKEN!r} — script bodies are opaque and must be "
            "explicitly acknowledged before they are ever written"
        )

    total = 0
    for relative, content in files.items():
        if not isinstance(content, str):
            raise cw.InvalidCandidateError(f"{relative!r} content must be a string")
        total += len(content.encode("utf-8"))
    if total > MAX_TOTAL_BYTES:
        raise cw.InvalidCandidateError(
            f"skill directory is {total} bytes, over the {MAX_TOTAL_BYTES} byte cap"
        )

    validate_skill_md_content(files[SKILL_FILENAME])
    for relative in extra_files:
        validate_script_body(files[relative], relative)

    skills_root = _skills_root(base)
    current_hash = _skill_md_hash(skills_root, name)
    existed = current_hash != cw.hash_bytes(b"")
    if expected_hash is None:
        if existed:
            raise cw.StaleConfigWriteError(f"skill {name!r} already exists; a hash is required")
    elif current_hash != expected_hash:
        raise cw.StaleConfigWriteError(f"skill {name!r} changed on disk since it was loaded")

    def _build(staging: Path) -> None:
        # Contained, direct writes -- NOT fw.write_file(), which additionally takes its
        # own cross-process flock per target. The staging dir is a fresh, private,
        # uncontested tree that no other writer can see until replace_tree promotes it
        # under a single cross-process lock, so taking a per-member lock here would be
        # pure redundant overhead (and would open/close a state-dir lock file per file
        # for nothing). replace_tree's own lock is the only contention point.
        for relative, content in files.items():
            target = fw.resolve_contained_path(staging, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            # write_BYTES, not write_text: text mode would translate "\n" to the
            # platform newline (CRLF on Windows), but the read path (read_skill_file /
            # list_skills) is byte-exact, so a text-mode write would break the
            # round-trip off-POSIX. Members are stored verbatim, mirroring the #766
            # primitive's own "wb" write.
            target.write_bytes(content.encode("utf-8"))

    try:
        fw.replace_tree(skills_root, name, _build)
    except fw.PathEscapeError as exc:
        raise cw.PathEscapeError(str(exc)) from exc


def delete_skill(base: Path, name: str) -> bool:
    """Delete the skill directory ``base/skills/<name>``; return whether it existed."""
    if not is_valid_skill_name(name):
        raise cw.PathEscapeError(f"invalid skill name: {name!r}")
    skills_root = _skills_root(base)
    try:
        return fw.delete_path(skills_root, name)
    except fw.PathEscapeError as exc:
        raise cw.PathEscapeError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Scope wrappers (mirror clauster.claude_md's project/user pair-of-wrappers shape)
# ---------------------------------------------------------------------------


def list_project_skills(project_dir: Path) -> list[dict[str, Any]]:
    """Return the listing for the project-scope ``<project>/.claude/skills/``."""
    return list_skills(project_dir / ".claude")


def list_user_skills(claude_json: Path) -> list[dict[str, Any]]:
    """Return the listing for the user-scope ``~/.claude/skills/``."""
    return list_skills(claude_json.parent / ".claude")


def read_project_skill_file(
    project_dir: Path, name: str, relative: str = SKILL_FILENAME
) -> tuple[str, str, bool]:
    """Read one file from a project-scope skill directory."""
    return read_skill_file(project_dir / ".claude", name, relative)


def read_user_skill_file(
    claude_json: Path, name: str, relative: str = SKILL_FILENAME
) -> tuple[str, str, bool]:
    """Read one file from a user-scope skill directory."""
    return read_skill_file(claude_json.parent / ".claude", name, relative)


def write_project_skill(
    project_dir: Path,
    name: str,
    files: dict[str, Any],
    *,
    expected_hash: str | None,
    confirm_scripts: str | None = None,
) -> None:
    """Validate + write a project-scope skill directory."""
    write_skill(
        project_dir / ".claude",
        name,
        files,
        expected_hash=expected_hash,
        confirm_scripts=confirm_scripts,
    )


def write_user_skill(
    claude_json: Path,
    name: str,
    files: dict[str, Any],
    *,
    expected_hash: str | None,
    confirm_scripts: str | None = None,
) -> None:
    """Validate + write a user-scope skill directory."""
    write_skill(
        claude_json.parent / ".claude",
        name,
        files,
        expected_hash=expected_hash,
        confirm_scripts=confirm_scripts,
    )


def delete_project_skill(project_dir: Path, name: str) -> bool:
    """Delete a project-scope skill directory."""
    return delete_skill(project_dir / ".claude", name)


def delete_user_skill(claude_json: Path, name: str) -> bool:
    """Delete a user-scope skill directory."""
    return delete_skill(claude_json.parent / ".claude", name)


# ---------------------------------------------------------------------------
# Enable/disable — the ``skillOverrides`` settings key (user/project/local scope,
# exactly mirroring clauster.config_write_hooks' three-scope ``hooks`` pattern).
# ---------------------------------------------------------------------------


def validate_skill_overrides(candidate: Any) -> None:
    """Structural validator for the whole ``skillOverrides`` object.

    ``candidate`` must be a ``dict`` mapping a valid skill name to one of
    :data:`SKILL_OVERRIDE_VALUES`. An invalid name or an unrecognized value rejects
    the whole write (→ 422), so a partial/garbled override map never lands.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("skillOverrides must be an object")
    for name, value in candidate.items():
        if not isinstance(name, str) or not is_valid_skill_name(name):
            raise cw.InvalidCandidateError(f"skillOverrides has an invalid skill name: {name!r}")
        if value not in SKILL_OVERRIDE_VALUES:
            raise cw.InvalidCandidateError(
                f"skillOverrides[{name!r}] must be one of {sorted(SKILL_OVERRIDE_VALUES)} "
                f"(got {value!r})"
            )


def _read_overrides(path: Path) -> tuple[dict[str, str], str]:
    """Return ``(overrides, content_hash)`` for a settings file at ``path``."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = cw.load_settings_json_obj(raw)
    overrides = data.get(SKILL_OVERRIDES_KEY)
    overrides = overrides if isinstance(overrides, dict) else {}
    return overrides, cw.hash_bytes(raw)


def read_project_skill_overrides(project_dir: Path) -> tuple[dict[str, str], str]:
    """Return ``(overrides, hash)`` for a project's ``.claude/settings.json``."""
    return _read_overrides(cw.project_settings_path(project_dir))


def write_project_skill_overrides(
    project_dir: Path, incoming: dict[str, str], expected_hash: str | None
) -> None:
    """Validate + write the project ``.claude/settings.json`` ``skillOverrides`` block."""
    cw.validate_candidate(incoming, validate_skill_overrides)
    path = cw.project_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cw.write_settings_subtree(path, SKILL_OVERRIDES_KEY, incoming, expected_hash)


def read_user_skill_overrides(settings_json: Path) -> tuple[dict[str, str], str]:
    """Return ``(overrides, hash)`` for the user-scope ``~/.claude/settings.json``."""
    return _read_overrides(settings_json)


def write_user_skill_overrides(
    settings_json: Path, incoming: dict[str, str], expected_hash: str | None
) -> None:
    """Validate + write the user-scope ``~/.claude/settings.json`` ``skillOverrides`` block."""
    cw.validate_candidate(incoming, validate_skill_overrides)
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    cw.write_settings_subtree(settings_json, SKILL_OVERRIDES_KEY, incoming, expected_hash)


def read_project_local_skill_overrides(project_dir: Path) -> tuple[dict[str, str], str]:
    """Return ``(overrides, hash)`` for a project's ``settings.local.json``."""
    return _read_overrides(cw.project_local_settings_path(project_dir))


def write_project_local_skill_overrides(
    project_dir: Path, incoming: dict[str, str], expected_hash: str | None
) -> None:
    """Validate + write the local-scope ``.claude/settings.local.json`` ``skillOverrides`` block.

    A successful write runs :func:`~clauster.config_write.ensure_gitignored` so a
    newly created ``settings.local.json`` is never accidentally committed (#766).
    """
    cw.validate_candidate(incoming, validate_skill_overrides)
    path = cw.project_local_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cw.write_settings_subtree(path, SKILL_OVERRIDES_KEY, incoming, expected_hash)
    cw.ensure_gitignored(project_dir, ".claude/settings.local.json", ignore_backup_sibling=True)
