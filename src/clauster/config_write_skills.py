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
  functions are that module's pattern renamed. A key is any skill name the CLI can show
  (a frontmatter ``name`` may differ from the directory name, and bundled skills such as
  ``doctor`` count too), not only a directory-shaped name. The separate, coarser
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

**Frontmatter: what the CLI reads (TASK-449.6).** The key set follows the frontmatter
reference at https://code.claude.com/docs/en/skills#frontmatter-reference. The accepted
VALUES are everything the installed CLI's skill loader (2.1.318, ``oPe`` and its helpers)
reads, which is wider than the documented types in places: a boolean field also takes
``yes``/``no``/``on``/``off``/``1``/``0`` in any case, and ``allowed-tools`` also takes a
list. A value the CLI rejects or ignores is refused (422), and the write never strips it.
Every field is optional, as in the docs, and a ``SKILL.md`` with no frontmatter at all is a
skill whose whole file is its content. Each check sees the value twice: once as PyYAML
(YAML 1.1) loads it and once as the CLI's YAML 1.2 loader would
(:func:`~clauster.config_write.yaml12_view`). A field the two read differently is refused,
so the editor never accepts a value on a reading Claude Code does not share.

**Convert on load.** An earlier release passed unknown frontmatter keys through, so a file
on disk can hold a value Claude Code ignores exactly as if the field were absent (an
``effort: banana``, a ``shell: zsh``). :func:`read_skill_file_for_edit` returns such a file
with those fields removed and a ``converted`` note, so the next save repairs it. Only a
single-value field (or a non-mapping ``metadata``) is removed. A field whose ignored value
is NOT the same as leaving it out (an invalid ``hooks`` block, which also cancels
``allowed-tools``; a ``disallowed-tools`` that hides ``disallowedTools``; an explicitly
tagged value) is never converted: the save is refused instead.

**Stale-hash guard scope (a documented simplification).** The guard compares against
the hash of ``SKILL.md`` content only, not a whole-tree hash — mirroring
:mod:`clauster.claude_md`'s single-file precedent. A concurrent edit that changes
only a sibling script file (leaving ``SKILL.md`` untouched) is not caught by this
guard; documented here rather than silently assumed.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from . import config_file_writer as fw
from . import config_write as cw
from .config_write_hooks import validate_hooks

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
#: ⚠️ ``MAX_SKILL_MD_BYTES`` is COUPLED to
#: :data:`clauster.config_write.MAX_EXPANDED_CHARS` — raising it without raising that would
#: make the alias-expansion guard reject an alias-FREE header. Read the comment there before
#: changing it; ``test_byte_caps_stay_under_the_expansion_cap`` is the gate. The other two
#: caps are unaffected: a supporting file is never parsed as YAML.
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

# THE SAME OBJECT config_write_subagents uses — an alias, not a copy, so the two
# parsers on this write tier cannot drift apart again (#1352). Change it in config_write.
_FRONTMATTER_RE = cw.FRONTMATTER_RE

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
#: against https://code.claude.com/docs/en/skills and .../settings, v2.1.129+, and against
#: the 2.1.318 settings schema, a record of skill name to exactly these four strings).
SKILL_OVERRIDE_VALUES = frozenset({"on", "name-only", "user-invocable-only", "off"})

#: The longest skill name the CLI keeps, in UTF-16 code units (its ``ZO``). A longer
#: frontmatter ``name`` is ignored, and a longer ``skillOverrides`` key matches no skill.
MAX_CLI_NAME_UNITS = 256


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
    raises :class:`~clauster.config_write.InvalidCandidateError`. An empty header reads
    as an empty mapping, as the CLI reads it. A file with no frontmatter at all is valid
    for Claude Code; :func:`validate_skill_md_content` handles that case before it gets
    here, so this function keeps its fenced-only contract for the fuzz harness.

    The fence and the YAML load are both shared with
    :func:`clauster.config_write_subagents.parse_frontmatter` (see
    :data:`~clauster.config_write.FRONTMATTER_RE` and
    :func:`~clauster.config_write.load_frontmatter_yaml`) — the two parsers guard the
    same code-executing write tier and must not drift.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} must start with a '---' YAML frontmatter block"
        )
    # `line_offset=1`: the header slice starts after the opening `---` fence, so a raw mark
    # names the line one above the fault in the FILE the operator is looking at.
    data = cw.load_frontmatter_yaml(
        cw.frontmatter_header(content, match),
        what=f"{SKILL_FILENAME} frontmatter",
        line_offset=1,
    )
    if data is None:
        data = {}  # an empty header: the CLI reads no fields, and so does this surface
    if not isinstance(data, dict):
        raise cw.InvalidCandidateError(f"{SKILL_FILENAME} frontmatter must be a YAML mapping")
    # The shared fence has no trailing capture group; the body is whatever follows the
    # match, exactly as config_write_subagents.parse_frontmatter takes it.
    return data, content[match.end() :]


# ---------------------------------------------------------------------------
# How the CLI reads each frontmatter field (2.1.318, `oPe` and its helpers)
# ---------------------------------------------------------------------------

#: The characters JavaScript's ``trim`` and ``\s`` match (WhiteSpace and LineTerminator).
#: Python's ``str.strip`` differs: it also strips ``\x1c``-``\x1f`` and ``\x85``, and it
#: keeps the BOM, ``\ufeff``.
_JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_JS_WHITESPACE_RUN_RE = re.compile(f"[{_JS_WHITESPACE}]+")
#: JavaScript ``parseInt(s, 10)``: optional leading whitespace, an optional sign, then the
#: longest run of ASCII digits; anything after the digits is ignored.
_JS_PARSE_INT_RE = re.compile(f"[{_JS_WHITESPACE}]*([+-]?[0-9]+)")

#: The strings the CLI's boolean reader (``bye``) takes as true or false, after it
#: lowercases and trims the value. The docs list these too.
_CLI_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_CLI_FALSE_WORDS = frozenset({"0", "false", "no", "off"})

#: The effort levels and the one alias the CLI's effort reader (``dq``) accepts.
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_ALIASES = {"med": "medium"}

#: The shells ``shell`` accepts (the CLI's ``Va``), compared trimmed and lowercased.
_SHELLS = ("bash", "powershell")

#: The hook events the CLI knows (its ``bc``, 2.1.318).
_HOOK_EVENTS = frozenset(
    {
        "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch", "Notification",
        "UserPromptSubmit", "UserPromptExpansion", "SessionStart", "SessionEnd", "Stop",
        "StopFailure", "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
        "PreModelSwitch", "PostModelSwitch", "PermissionRequest", "PermissionDenied", "Setup",
        "TeammateIdle", "TaskCreated", "TaskCompleted", "Elicitation", "ElicitationResult",
        "ConfigChange", "WorktreeCreate", "WorktreeRemove", "InstructionsLoaded",
        "CwdChanged", "FileChanged", "DirectoryAdded", "MessageDisplay",
    }
)  # fmt: skip
#: The two events whose hooks guard a tool call (its ``XX``). Hooks for them anywhere but
#: under ``hooks`` make the CLI load the skill with no hooks AND no ``allowed-tools``.
_GUARD_EVENTS = frozenset({"PreToolUse", "PermissionRequest"})
#: Top-level keys the CLI's scan for misplaced hooks skips (its ``Don``).
_UNSCANNED_KEYS = frozenset({"metadata", "mcpServers", "lspServers"})

#: Code points the CLI refuses in a shown name although Python files them under a letter
#: or symbol category: Hangul fillers and the Braille blank (default-ignorable, or blank
#: to the eye) and the few characters its blank-like list names.
_NAME_REFUSED_CODEPOINTS = frozenset(
    {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0xFFFC, 0x1D159, 0x13441, 0x13442, 0x16FE4}
)
#: Hangul jamo the CLI accepts only in a context it checks script by script. Refused here.
_NAME_REFUSED_RANGES = ((0x1160, 0x11FF), (0xD7B0, 0xD7FF))


#: Below this, every integer is exact as a JavaScript number (``Number.MAX_SAFE_INTEGER``).
_JS_SAFE_INTEGER = 2**53


class _IgnoredFieldError(cw.InvalidCandidateError):
    """A recognized field holds a value Claude Code ignores exactly as if it were absent.

    On write this is an ordinary 422. On load it marks a field that
    :func:`read_skill_file_for_edit` can remove without changing what Claude Code does.
    """

    def __init__(self, key: str, message: str) -> None:
        """Keep the field name so a conversion on load knows which key to remove."""
        super().__init__(message)
        self.key = key


def _js_number_text(value: float) -> str:
    """Return JavaScript's ``String()`` of a number that is not a small whole number."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    sign = "-" if value < 0 else ""
    # repr() gives the shortest round-tripping digits, as JavaScript does; only the layout
    # differs (Number::toString, ECMA-262 6.1.6.1.20).
    digits_tuple = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(str(d) for d in digits_tuple.digits)
    # The caller passes only a finite value that is not zero, so the exponent is an int.
    point = len(digits) + int(digits_tuple.exponent)  # the decimal point follows this many
    if len(digits) <= point <= 21:
        # A whole number past 2**53: JavaScript keeps only the shortest round-trip digits.
        return sign + digits + "0" * (point - len(digits))
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
    power = point - 1
    return f"{sign}{mantissa}e{'+' if power > 0 else '-'}{abs(power)}"


def _js_string(value: Any) -> str:
    """Return JavaScript's ``String(value)`` for a value YAML can produce.

    Raises ``TypeError`` for a value JavaScript never sees from the CLI's YAML loader (a
    PyYAML date, ``bytes``, a set or an explicitly tagged value), so the caller can refuse
    the field instead of guessing what Claude Code reads.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        try:
            as_float = float(value)
        except OverflowError:
            return "Infinity" if value > 0 else "-Infinity"
        return str(value) if abs(value) < _JS_SAFE_INTEGER else _js_number_text(as_float)
    if isinstance(value, float):
        if value.is_integer() and abs(value) < _JS_SAFE_INTEGER:
            return str(int(value))
        return _js_number_text(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Array.prototype.toString: join with ",", null and undefined become "".
        return ",".join("" if item is None else _js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    raise TypeError(type(value).__name__)


def _js_trim(text: str) -> str:
    """Return ``text`` with JavaScript's ``trim()`` applied."""
    return text.strip(_JS_WHITESPACE)


def _utf16_length(text: str) -> int:
    """Return JavaScript's ``.length`` of ``text``: UTF-16 code units, not code points."""
    return len(text.encode("utf-16-le")) // 2


def cli_shows_name(name: str) -> bool:
    """Return whether Claude Code shows and matches ``name`` as a skill name.

    A port, slightly narrower, of the CLI's name rule (``GJ``) and of the docs'
    "Characters a skill name can't hold": at most 256 UTF-16 code units, no control,
    format, private-use, surrogate or unassigned character, no blank other than one plain
    space between words, and no ``<`` or ``>``. The CLI decides some combining marks and
    jamo script by script; those are refused here outright, which only ever refuses more.
    """
    if not name or _utf16_length(name) > MAX_CLI_NAME_UNITS:
        return False
    if name != name.strip(" ") or "  " in name:
        return False
    if "<" in name or ">" in name or "&lt;" in name or "&gt;" in name:
        return False
    for char in name:
        point = ord(char)
        category = unicodedata.category(char)
        if category[0] in "CM" or category in ("Zl", "Zp"):
            return False
        if category == "Zs" and char != " ":
            return False
        if point in _NAME_REFUSED_CODEPOINTS:
            return False
        if any(low <= point <= high for low, high in _NAME_REFUSED_RANGES):
            return False
    return True


def _cli_bool(value: Any) -> bool | None:
    """Return the CLI's ``bye`` reading of ``value``: True, False, or None when it ignores it."""
    if isinstance(value, bool):
        return value
    if not isinstance(value, (str, int, float)):
        return None
    word = _js_trim(_js_string(value).lower())
    if word in _CLI_TRUE_WORDS:
        return True
    if word in _CLI_FALSE_WORDS:
        return False
    return None


def _cli_tool_entries(items: list[str]) -> list[str]:
    """Split tool-list strings into entries the way the 2.1.318 CLI does (its ``fu``).

    A comma or a space ends an entry unless it is inside parentheses, so
    ``Agent(worker, researcher), Read`` is two entries. Nothing is resolved against a tool
    registry. The same port as the subagents surface's (PR 1804); share it once both land.
    """
    entries: list[str] = []
    for item in items:
        current, in_parens = "", False
        for char in item:
            if char == "(":
                in_parens = True
                current += char
            elif char == ")":
                in_parens = False
                current += char
            elif char in ", " and not in_parens:
                if _js_trim(current):
                    entries.append(_js_trim(current))
                current = ""
            elif current or _js_trim(char):
                current += char
        if _js_trim(current):
            entries.append(_js_trim(current))
    return entries


def _js_parse_int(text: str) -> int | None:
    """Return what JavaScript ``parseInt(text, 10)`` returns, or None for ``NaN``."""
    match = _JS_PARSE_INT_RE.match(text)
    return int(match.group(1)) if match else None


def _cli_effort(value: Any) -> str | int | None:
    """Return the effort the CLI reads from ``value`` (its ``dq``), or None when it ignores it.

    A number must be a whole number, and any whole number counts. Anything else is
    stringified and lowercased (not trimmed), mapped through the alias table, and matched
    against the level names. Failing that, ``parseInt`` decides, so ``"3 tokens"`` is ``3``.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        text = _js_string(value)  # a list joins, so `[high]` reads as high
    except TypeError:
        return None
    lowered = text.lower()
    level = _EFFORT_ALIASES.get(lowered, lowered)
    if level in _EFFORT_LEVELS:
        return level
    return _js_parse_int(lowered)


def _label(key: str) -> str:
    """Return the message prefix that names one frontmatter field."""
    return f"{SKILL_FILENAME} frontmatter {key!r}"


def _read_text_field(key: str, value: Any) -> str | None:
    """Read a field the CLI turns into text with ``String()``: a scalar or a list of them.

    ``argument-hint: [issue-number]`` is a YAML list, and the CLI shows it as
    ``issue-number``. A mapping, alone or inside a list, becomes ``[object Object]``, which
    is refused. A null is "not set".
    """
    if value is None:
        return None
    try:
        if isinstance(value, dict) or (
            isinstance(value, list) and any(isinstance(item, dict) for item in value)
        ):
            raise TypeError("dict")
        return _js_string(value)
    except TypeError:
        raise cw.InvalidCandidateError(
            f"{_label(key)} must be text, a number or true/false (or a list of them)"
        ) from None


def _read_description(value: Any) -> str | None:
    """Read ``description``: text, a number or a boolean, never blank (the CLI's ``ace``)."""
    if value is None:
        return None
    if isinstance(value, str):
        text = _js_trim(value)
        if not text:
            raise _IgnoredFieldError(
                "description",
                f"{_label('description')} is blank. Claude Code ignores it and uses the "
                "first line of the content instead. Write a description or remove the line",
            )
        return text
    if isinstance(value, (bool, int, float)):
        return _js_string(value)
    raise _IgnoredFieldError(
        "description",
        f"{_label('description')} must be text. Claude Code ignores a list or a mapping",
    )


def _read_name(value: Any) -> str | None:
    """Read ``name``: shown as the command name when Claude Code can show it."""
    if value is None:
        return None
    try:
        if isinstance(value, (dict, list)):
            raise TypeError("collection")
        text = _js_string(value)
    except TypeError:
        raise cw.InvalidCandidateError(f"{_label('name')} must be text") from None
    if text == "" or _utf16_length(text) > MAX_CLI_NAME_UNITS:
        raise _IgnoredFieldError(
            "name",
            f"{_label('name')} must be 1 to {MAX_CLI_NAME_UNITS} characters. Claude Code "
            "ignores it otherwise and uses the directory name",
        )
    if not cli_shows_name(text):
        raise cw.InvalidCandidateError(
            f"{_label('name')} holds a character Claude Code refuses in a name: a hidden, "
            "direction-changing or blank-like character other than single spaces between "
            "words, or < or >"
        )
    return text


def _bool_reader(key: str, *, absent: bool | None) -> Callable[[Any], bool | None]:
    """Return a reader for a boolean field that an unreadable value leaves at ``absent``.

    Used for the fields where the CLI treats a null or an unreadable value exactly as a
    missing field, so such a value can be removed on load. ``user-invocable`` differs and
    has its own reader.
    """

    def _read(value: Any) -> bool | None:
        """Read the field, refusing a value the CLI's boolean reader does not take."""
        if value is None:
            return absent
        reading = _cli_bool(value)
        if reading is None:
            raise _IgnoredFieldError(
                key,
                f"{_label(key)} must be true or false (yes/no, on/off and 1/0 also work, "
                "in any case). Claude Code ignores any other value",
            )
        return reading

    return _read


def _read_user_invocable(value: Any) -> bool:
    """Read ``user-invocable``: absent is true, and the CLI reads an unreadable value as false.

    ``user-invocable:`` with no value (YAML null) is read as false, so it hides the skill
    from the ``/`` menu. That is accepted, as the CLI reads it. An unreadable value is
    refused: the CLI would also read it as false, which a typo should not do silently.
    """
    if value is None:
        return False
    reading = _cli_bool(value)
    if reading is None:
        raise cw.InvalidCandidateError(
            f"{_label('user-invocable')} must be true or false (yes/no, on/off and 1/0 also "
            "work, in any case). Claude Code reads anything else as false and hides the skill"
        )
    return reading


def _read_tool_list(key: str, value: Any) -> list[str]:
    """Read a tool list: a space- or comma-separated string, or a list of strings."""
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        return _cli_tool_entries([value])
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise cw.InvalidCandidateError(
                f"{_label(key)} must list tool names as strings. Claude Code drops any other entry"
            )
        return _cli_tool_entries(value)
    raise _IgnoredFieldError(
        key,
        f"{_label(key)} must be a space- or comma-separated string or a list. Claude Code "
        "ignores any other value",
    )


def _read_arguments(value: Any) -> list[str]:
    """Read ``arguments``: names for ``$name`` placeholders (the CLI's ``wke``)."""
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        names = [word for word in _JS_WHITESPACE_RUN_RE.split(value) if word]
    elif isinstance(value, list):
        names = value
    else:
        raise _IgnoredFieldError(
            "arguments",
            f"{_label('arguments')} must be a space-separated string or a list. Claude Code "
            "ignores any other value",
        )
    for name in names:
        if not isinstance(name, str) or not _js_trim(name) or re.fullmatch(r"[0-9]+", name):
            raise cw.InvalidCandidateError(
                f"{_label('arguments')} must hold names. Claude Code drops an entry that is "
                "not text, is blank, or is only digits"
            )
    return list(names)


def _read_effort(value: Any) -> str | int | None:
    """Read ``effort``: a level, the alias ``med``, or an integer (the CLI's ``dq``)."""
    if value is None:
        return None
    reading = _cli_effort(value)
    if reading is None:
        raise _IgnoredFieldError(
            "effort",
            f"{_label('effort')} must be one of {list(_EFFORT_LEVELS)} (any case, or 'med') "
            "or an integer. Claude Code ignores any other value",
        )
    return reading


def _read_model(value: Any) -> str | None:
    """Read ``model``: any non-blank string; ``inherit`` in any case keeps the session model."""
    if value is None:
        return None
    if not isinstance(value, str) or not _js_trim(value):
        raise _IgnoredFieldError(
            "model",
            f"{_label('model')} must be a model alias, a model ID or 'inherit'. Claude Code "
            "ignores a blank value or one that is not text",
        )
    text = _js_trim(value)
    return "inherit" if text.lower() == "inherit" else text


def _read_context(value: Any) -> str | None:
    """Read ``context``: only ``fork``, spelled exactly."""
    if value is None:
        return None
    if value != "fork":
        raise _IgnoredFieldError(
            "context",
            f"{_label('context')} must be 'fork'. Claude Code ignores any other value",
        )
    return "fork"


def _read_shell(value: Any) -> str | None:
    """Read ``shell``: ``bash`` or ``powershell``, in any case (the CLI's ``tdo``)."""
    if value is None:
        return None
    try:
        word = _js_trim(_js_string(value)).lower()
    except TypeError:
        word = ""
    if word not in _SHELLS:
        raise _IgnoredFieldError(
            "shell",
            f"{_label('shell')} must be one of {list(_SHELLS)}. Claude Code ignores any other "
            "value and uses bash",
        )
    return word


def _read_metadata(value: Any) -> bool | None:
    """Read ``metadata``: any mapping, kept for your own tools. The CLI acts on none of it."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _IgnoredFieldError(
            "metadata",
            f"{_label('metadata')} must be a mapping. Claude Code drops any other value",
        )
    return True


def _path_patterns(value: Any) -> list[str]:
    """Split ``paths`` the way the CLI does (its ``Ka``): top-level commas, nested lists."""
    if isinstance(value, list):
        return [pattern for item in value for pattern in _path_patterns(item)]
    if not isinstance(value, str):
        raise cw.InvalidCandidateError(
            f"{_label('paths')} must hold glob patterns as strings. Claude Code drops any "
            "other entry"
        )
    patterns, current, depth = [], "", 0
    for char in value:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            patterns.append(_js_trim(current))
            current = ""
            continue
        current += char
    patterns.append(_js_trim(current))
    return [pattern for pattern in patterns if pattern]


def _read_paths(value: Any) -> list[str]:
    """Read ``paths``: a comma-separated string or a list of glob patterns."""
    if value is None:
        return []
    if not isinstance(value, (str, list)):
        raise _IgnoredFieldError(
            "paths",
            f"{_label('paths')} must be a comma-separated string or a list. Claude Code "
            "ignores any other value",
        )
    return _path_patterns(value)


def _read_hooks(value: Any) -> Any:
    """Read ``hooks``: checked by the hooks surface's validator, stored, never executed.

    The same check as subagent frontmatter hooks and the Hooks tab
    (:func:`~clauster.config_write_hooks.validate_hooks`, TASK-482): every documented event,
    hook type and field, and nothing else. It is stricter than the CLI in one direction
    only: the CLI drops a bad entry under most events and keeps the rest, and this refuses
    the save instead. The entries are also compared between the YAML 1.1 and YAML 1.2
    readings. A ``hooks`` that is false, empty or 0 is ignored by the CLI exactly as if it
    were absent, so a load may remove it.
    """
    if value is None:
        return None
    if value is False or value == "" or (isinstance(value, (int, float)) and value == 0):
        # Falsy in JavaScript, so the CLI reads no hooks, exactly as if the key were absent.
        raise _IgnoredFieldError(
            "hooks", f"{_label('hooks')} must be a mapping of hook events to matcher lists"
        )
    try:
        validate_hooks(value)
    except cw.InvalidCandidateError as exc:
        raise cw.InvalidCandidateError(f"{_label('hooks')}: {exc}") from exc
    return _tagged(value)


def _read_dream_marker(key: str, value: Any) -> str | None:
    """Read ``created_by``/``improved_by``: CLI bookkeeping, read only as ``dream-proposal``."""
    if value is None:
        return None
    if value != "dream-proposal":
        raise _IgnoredFieldError(
            key,
            f"{_label(key)} is a Claude Code bookkeeping field that is read only as "
            "'dream-proposal'. Claude Code ignores any other value",
        )
    return value


def _names_guard_event(value: Any) -> bool:
    """Whether ``value`` is a mapping with a non-empty PreToolUse/PermissionRequest (``Ja``)."""
    return isinstance(value, dict) and any(
        key in _GUARD_EVENTS and item is not None and item != [] for key, item in value.items()
    )


def _looks_like_matcher(value: Any) -> bool:
    """Whether ``value`` is shaped like a hook matcher group (the CLI's ``gCe``)."""
    if not isinstance(value, dict):
        return False
    if "matcher" not in value and any(key in _HOOK_EVENTS for key in value):
        return False
    hooks = value.get("hooks")
    if isinstance(hooks, list):
        return len(hooks) > 0
    return isinstance(hooks, dict) and ("matcher" in value or isinstance(hooks.get("type"), str))


def _holds_guard_hooks(
    value: Any,
    depth: int = 3,
    *,
    matchers: bool = True,
    unscanned: frozenset[str] = frozenset(),
    in_hooks_list: bool = False,
) -> bool:
    """Whether ``value`` holds hook definitions within ``depth`` levels (the CLI's ``D2e``)."""
    if _names_guard_event(value) or (matchers and _looks_like_matcher(value)):
        return True
    if depth == 0 or not isinstance(value, (dict, list)):
        return False
    if isinstance(value, list):
        return any(
            _holds_guard_hooks(
                item,
                depth - 1,
                matchers=matchers,
                unscanned=unscanned,
                in_hooks_list=in_hooks_list,
            )
            for item in value
        )
    if in_hooks_list and isinstance(value.get("type"), str):
        return False
    return any(
        key not in unscanned
        and _holds_guard_hooks(
            item,
            depth - 1,
            matchers=matchers and key not in _HOOK_EVENTS,
            unscanned=unscanned,
            in_hooks_list=key == "hooks" and isinstance(item, list),
        )
        for key, item in value.items()
    )


def _refuse_misplaced_hooks(candidate: dict[Any, Any]) -> None:
    """Refuse hook definitions the CLI finds outside ``hooks`` (its ``Hse(e, Don)``).

    A top-level ``PreToolUse``/``PermissionRequest``, or a matcher group nested up to three
    levels into any other field except ``metadata``, ``mcpServers`` and ``lspServers``,
    makes Claude Code load the skill with no hooks and no ``allowed-tools``.
    """
    misplaced = _names_guard_event(candidate) or any(
        key != "hooks"
        and key not in _UNSCANNED_KEYS
        and _holds_guard_hooks(
            item, 3, unscanned=_UNSCANNED_KEYS, matchers=key not in _HOOK_EVENTS
        )
        for key, item in candidate.items()
    )
    if misplaced:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} frontmatter holds hook definitions outside 'hooks' (a "
            "PreToolUse or PermissionRequest key, or a matcher group, in another field). Put "
            "them under 'hooks'. Claude Code otherwise loads the skill with no hooks and no "
            "'allowed-tools'"
        )


def _tagged(value: Any) -> Any:
    """Return ``value`` in a form where two readings compare equal only if JavaScript agrees.

    Booleans stay apart from 1 and 0 (Python treats ``True == 1``), whole floats equal
    their ints (JavaScript has one number type), mapping keys become JavaScript property
    names, and a value JavaScript never sees (a date, ``bytes``, an explicitly tagged
    value) gets a unique tag, so it never compares equal to anything.
    """
    if isinstance(value, bool) or value is None:
        return ("bool-or-null", value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return ("number", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_tagged(item) for item in value))
    if isinstance(value, dict):
        try:
            return ("map", tuple(sorted((_js_string(k), _tagged(v)) for k, v in value.items())))
        except TypeError:
            return ("unknown", id(value))
    return ("unknown", id(value))


#: Each recognized frontmatter key → the reader that returns what Claude Code reads from
#: it, or raises. Table-driven, like the subagents surface, so the recognized set reads as
#: one list. Sources: https://code.claude.com/docs/en/skills#frontmatter-reference, and the
#: 2.1.318 loader for ``version``, ``fallback``, ``disallowedTools``, ``created_by`` and
#: ``improved_by``, which it reads but the docs do not list. ``license`` and
#: ``compatibility`` are documented as accepted but unused, so they pass through like any
#: unknown key; so does ``when-to-use`` (the loader reads only ``when_to_use``).
_FIELD_READERS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("description", _read_description),
    ("name", _read_name),
    ("when_to_use", lambda v: _read_text_field("when_to_use", v)),
    ("argument-hint", lambda v: _read_text_field("argument-hint", v)),
    ("arguments", _read_arguments),
    ("disable-model-invocation", _bool_reader("disable-model-invocation", absent=False)),
    ("user-invocable", _read_user_invocable),
    ("allowed-tools", lambda v: _read_tool_list("allowed-tools", v)),
    ("disallowed-tools", lambda v: _read_tool_list("disallowed-tools", v)),
    ("disallowedTools", lambda v: _read_tool_list("disallowedTools", v)),
    ("model", _read_model),
    ("effort", _read_effort),
    ("context", _read_context),
    ("agent", lambda v: _read_text_field("agent", v)),
    ("background", _bool_reader("background", absent=None)),
    ("fallback", _bool_reader("fallback", absent=None)),
    ("hooks", _read_hooks),
    ("paths", _read_paths),
    ("shell", _read_shell),
    ("metadata", _read_metadata),
    ("version", lambda v: _read_text_field("version", v)),
    ("created_by", lambda v: _read_dream_marker("created_by", v)),
    ("improved_by", lambda v: _read_dream_marker("improved_by", v)),
)


def validate_frontmatter(candidate: Any) -> dict[str, Any]:
    """Return what Claude Code reads from a SKILL.md frontmatter mapping, or raise.

    Every field is optional. Each recognized key (:data:`_FIELD_READERS`) is read the way
    the 2.1.318 CLI reads it, and a value the CLI rejects or ignores raises
    :class:`~clauster.config_write.InvalidCandidateError` (a 422 on write). An
    UNRECOGNIZED key is passed through untouched: Claude Code ignores a field it does not
    know, without an error (#958/DF-3). A YAML null is "not set" for every field except
    ``user-invocable``, where the CLI reads it as false. STRUCTURE ONLY — nothing here is
    ever resolved or executed, including ``allowed-tools`` and ``hooks``.

    The returned mapping holds one reading per recognized key present, in a comparable
    form; :func:`validate_skill_md_content` compares the YAML 1.1 and 1.2 readings with it.
    """
    readings = _field_readings(candidate)
    for reading in readings.values():
        if isinstance(reading, cw.InvalidCandidateError):
            raise reading
    return readings


def _field_readings(candidate: Any) -> dict[str, Any]:
    """Return one reading per recognized key, or the error that key raises, in table order."""
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError(f"{SKILL_FILENAME} frontmatter must be an object")
    _refuse_misplaced_hooks(candidate)
    readings: dict[str, Any] = {}
    try:
        for key, read in _FIELD_READERS:
            if key not in candidate:
                continue
            value = candidate[key]
            if isinstance(value, cw.Yaml12Unknown):
                # The CLI may read an explicitly tagged value: never treat it as ignorable.
                readings[key] = cw.InvalidCandidateError(
                    f"{_label(key)} has an explicit YAML tag that Claude Code may read "
                    "differently. Remove the tag"
                )
                continue
            try:
                readings[key] = _tagged(read(value))
            except cw.InvalidCandidateError as exc:
                readings[key] = exc
    except RecursionError as exc:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} frontmatter is nested too deeply to check"
        ) from exc
    if candidate.get("disallowed-tools") is not None and "disallowedTools" in candidate:
        readings["disallowedTools"] = _IgnoredFieldError(
            "disallowedTools",
            f"{_label('disallowedTools')} is ignored while 'disallowed-tools' is set. Keep one",
        )
    denylist = readings.get("disallowed-tools")
    if isinstance(denylist, _IgnoredFieldError) and "disallowedTools" in candidate:
        # The CLI reads `disallowed-tools ?? disallowedTools`: an ignored but non-null value
        # still hides `disallowedTools`, so removing it would change the denylist.
        readings["disallowed-tools"] = cw.InvalidCandidateError(str(denylist))
    return readings


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


#: A first line that opens frontmatter. A file that starts with one but has no closing
#: ``---`` line is refused: Claude Code would read the intended header as content.
_OPENING_FENCE_RE = re.compile(r"\A---[ \t]*\r?(?:\n|\Z)")
#: Two fence lines with nothing between them: an empty frontmatter, which the CLI reads as
#: no fields. The shared fence needs a line between the two, so it does not match this.
_EMPTY_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n---[ \t]*(?:\r?\n|\Z)")
#: When the CLI reads frontmatter at all: its ``ok`` regex, ``/^---\s*\n([\s\S]*?)---/``,
#: where JavaScript's ``\s`` also takes a no-break space, a form feed or a BOM.
_CLI_FENCE_RE = re.compile(f"---[{_JS_WHITESPACE}]*\n[\\s\\S]*?---")


def _split_skill_md(content: str) -> tuple[dict[str, Any], str | None, str]:
    """Return ``(frontmatter, header, text)`` the way Claude Code splits a SKILL.md.

    ``text`` is ``content`` without a leading BOM, which the CLI drops before it looks
    for the fence. ``header`` is the raw YAML between the fences, or None when the file
    has no frontmatter: Claude Code then reads the whole file as the skill's content.
    """
    text = content.removeprefix("\ufeff")
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        if _EMPTY_FRONTMATTER_RE.match(text):
            return {}, "", text
        if _CLI_FENCE_RE.match(text):
            # Claude Code reads a header here and this surface does not: refuse, rather than
            # accept the file as having no frontmatter and check none of its fields.
            raise cw.InvalidCandidateError(
                f"{SKILL_FILENAME} has a frontmatter fence that Claude Code reads but this "
                "editor cannot read the same way, for example a no-break space after '---' "
                "or a closing '---' in the middle of a line. Put each '---' alone on its line"
            )
        if _OPENING_FENCE_RE.match(text):
            raise cw.InvalidCandidateError(
                f"{SKILL_FILENAME} opens a '---' frontmatter block but never closes it. "
                "Add a closing '---' line"
            )
        return {}, None, text
    frontmatter, _body = parse_frontmatter(text)
    return frontmatter, cw.frontmatter_header(text, match), text


def _js_keys(value: dict[Any, Any]) -> dict[str, Any]:
    """Return a YAML 1.2 mapping with its top-level keys as JavaScript property names."""
    try:
        return {_js_string(key): item for key, item in value.items()}
    except TypeError:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} frontmatter has a key Claude Code cannot read"
        ) from None


def _check_skill_md(content: str) -> dict[str, Any]:
    """Return the CLI's readings of ``content``'s frontmatter, or raise; never writes.

    The readings come from the YAML 1.2 view, which is what Claude Code parses. The PyYAML
    (YAML 1.1) mapping must give the same readings, so every other part of this surface,
    which works on the PyYAML mapping, agrees with the CLI.
    """
    frontmatter, header, _text = _split_skill_md(content)
    if header is None:
        return validate_frontmatter({})
    view = cw.yaml12_view(header)
    readings = validate_frontmatter({} if view is None else _js_keys(view))
    pyyaml_readings = _field_readings(frontmatter)
    differing = sorted(
        key
        for key in readings.keys() | pyyaml_readings.keys()
        if not _same_reading(readings.get(key), pyyaml_readings.get(key))
    )
    if differing:
        raise cw.InvalidCandidateError(
            f"{_label(differing[0])} reads differently in YAML 1.1 and in the YAML 1.2 that "
            "Claude Code reads, as yes, on, 010, 0b1, 1e3 or a date do. Quote the value, or "
            "write true, false or a plain number"
        )
    return readings


def _same_reading(a: Any, b: Any) -> bool:
    """Whether two readings of one field agree; an error never agrees with anything."""
    if isinstance(a, Exception) or isinstance(b, Exception):
        return False
    return bool(a == b)


def validate_skill_md_content(candidate: Any) -> None:
    """Structural validator for a ``SKILL.md`` body: size cap + valid frontmatter.

    STRUCTURE ONLY — the body text (everything after the frontmatter) is never
    parsed, resolved, or executed; only the frontmatter's shape is checked. A file with
    no frontmatter is valid (Claude Code reads it all as content).
    """
    if not isinstance(candidate, str):
        raise cw.InvalidCandidateError(f"{SKILL_FILENAME} content must be a string")
    size = len(candidate.encode("utf-8"))
    if size > MAX_SKILL_MD_BYTES:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} is {size} bytes, over the {MAX_SKILL_MD_BYTES} byte cap"
        )
    _check_no_plugin_marker(candidate, SKILL_FILENAME)
    _check_skill_md(candidate)


# ---------------------------------------------------------------------------
# Convert on load: remove a field whose value Claude Code ignores as if absent
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")


def _ignorable_keys(text: str) -> list[str] | None:
    """Return the fields a load may remove from ``text``, or None when it may remove none.

    One reading pass over the YAML 1.2 view, which is what Claude Code parses. Every error
    must be an ignored field (:class:`_IgnoredFieldError`), and each such field must hold a
    scalar: a list or mapping may hold data that the CLI reads elsewhere (its scan for
    misplaced hooks looks into every field but ``metadata``, ``mcpServers`` and
    ``lspServers``), so removing one is never assumed equivalent. ``metadata`` is the one
    collection it may remove, because that scan skips it.
    """
    try:
        _frontmatter, header, _ = _split_skill_md(text)
        view = cw.yaml12_view(header) if header else None
        mapping = _js_keys(view) if isinstance(view, dict) else {}
        readings = _field_readings(mapping)
    except cw.InvalidCandidateError:
        return None
    errors = {k: r for k, r in readings.items() if isinstance(r, cw.InvalidCandidateError)}
    if not errors or not all(isinstance(r, _IgnoredFieldError) for r in errors.values()):
        return None
    for key in errors:
        if isinstance(mapping.get(key), (dict, list)) and key != "metadata":
            return None
    return sorted(errors)


def _without_top_level_keys(text: str, keys: list[str]) -> str | None:
    """Return ``text`` with the top-level frontmatter fields ``keys`` removed, or None.

    Textual, so every other byte keeps its spelling and quoting. Only plain block-style
    top-level pairs, each spelled once, with no ``<<`` merge at the top level, are removed:
    for each, its lines from the key's line to the line its value ends on. The result is
    used only when it parses to exactly the old mapping without ``keys``.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:  # pragma: no cover - only a fenced, non-empty header has a field error
        return None
    header = match.group(1)
    full = cw.frontmatter_header(text, match)
    if cw.cli_frontmatter_text(full) != full:
        return None  # the CLI reads a re-quoted copy: the lines on disk are not its lines
    root = yaml.compose(header, Loader=yaml.SafeLoader)
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        return None
    if any(key_node.tag == "tag:yaml.org,2002:merge" for key_node, _ in root.value):
        return None
    # Split where PyYAML counts a line (`\r\n`, `\r`, `\n`; the seam refused NEL, LS and
    # PS), so a mark's line number indexes this list. The pattern partitions the header.
    lines = _LINE_RE.findall(header)
    drop: set[int] = set()
    for key in keys:
        pairs = [
            (key_node, value_node)
            for key_node, value_node in root.value
            if isinstance(key_node, yaml.ScalarNode) and key_node.value == key
        ]
        if len(pairs) != 1 or pairs[0][0].start_mark.column != 0:
            return None
        key_node, value_node = pairs[0]
        end = value_node.end_mark
        drop.update(range(key_node.start_mark.line, end.line if end.column == 0 else end.line + 1))
    new_header = "".join(line for i, line in enumerate(lines) if i not in drop)
    if len(lines) - 1 in drop:
        new_header = re.sub(r"(?:\r\n|\r|\n)\Z", "", new_header)
    converted = text[: match.start(1)] + new_header + text[match.end(1) :]
    try:
        before, _ = parse_frontmatter(text)
        after, _ = parse_frontmatter(converted)
    except cw.InvalidCandidateError:
        return None
    for key in keys:
        before.pop(key, None)
    return converted if repr(after) == repr(before) else None


def _convert_ignored_fields(content: str) -> tuple[str, list[str]] | None:
    """Return ``(text, removed_keys)`` when removing ignored fields makes ``content`` valid.

    One pass: find every removable field (:func:`_ignorable_keys`), remove them together,
    and validate the result once, so a load costs a few parses however many fields go.
    """
    bom = "\ufeff" if content.startswith("\ufeff") else ""
    text = content.removeprefix("\ufeff")
    keys = _ignorable_keys(text)
    if keys is None:
        return None
    converted = _without_top_level_keys(text, keys)
    if converted is None:
        return None
    try:
        validate_skill_md_content(bom + converted)
    except cw.InvalidCandidateError:
        return None
    return bom + converted, keys


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
    invocation"?, "frontmatter_error"?, "files_error"?}``. A skill whose ``SKILL.md`` fails
    structural validation or is not UTF-8 is still listed (so a hand-edited/corrupt skill is
    visible, not hidden) with ``frontmatter_error`` set instead of raising.

    That tolerance is **per skill, and covers I/O as well as parse errors**: an ``OSError``
    from reading ``SKILL.md`` (EACCES/EIO/ELOOP, or a file pruned mid-listing) lands in
    ``frontmatter_error``, and one from enumerating members via ``resolve()``/``rglob()``
    lands in ``files_error`` with ``files`` empty. Neither fails the listing, so one bad
    skill can no longer hide every good one — the same per-entry containment
    :func:`~clauster.supervisor.list_background_jobs` uses. The errors are reported rather
    than swallowed: an empty ``files`` alone would make an unreadable skill dir look
    healthy.

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
        # `is_symlink()` FIRST (lstat, never follows); `is_dir()` follows, so testing it
        # first stat'd a symlink's out-of-tree target before we rejected the entry anyway.
        # Same hoist the subagents LIST path took — decision 7's sibling sweep.
        if entry.is_symlink() or not entry.is_dir() or not is_valid_skill_name(entry.name):
            continue
        skill_md = entry / SKILL_FILENAME
        # A *symlinked* SKILL.md is refused, never followed — consistent with skipping
        # symlinked skill DIRS above. ``is_file()`` follows symlinks (True for a
        # symlink-to-file), so a symlinked SKILL.md pointing out of the tree would
        # otherwise have its target content read; requiring a non-symlink regular file
        # closes that. A symlinked entrypoint reads as "no SKILL.md" (nothing surfaced).
        has_skill_md = not skill_md.is_symlink() and skill_md.is_file()
        item: dict[str, Any] = {"name": entry.name, "has_skill_md": has_skill_md}
        if has_skill_md:
            try:
                text = skill_md.read_bytes().decode("utf-8")
                readings = _check_skill_md(text)
            except cw.InvalidCandidateError as exc:
                # The error string can quote a fragment of the offending frontmatter
                # (a bad YAML line), so redact it the same way the body view is.
                item["frontmatter_error"] = cw.redact_secret_lines(str(exc))
            except UnicodeDecodeError:
                item["frontmatter_error"] = f"{SKILL_FILENAME} is not valid UTF-8"
            except OSError as exc:
                # Per ENTRY, never per listing: an unreadable SKILL.md (EACCES, ELOOP, or a
                # file pruned between iterdir and read) used to propagate and fail the WHOLE
                # skills listing, so one bad skill hid every good one. Matches
                # `supervisor.list_background_jobs`, which catches OSError per entry for
                # exactly this reason. The entry is still surfaced, carrying its error.
                reason = exc.strerror or exc
                item["frontmatter_error"] = f"{SKILL_FILENAME} could not be read: {reason}"
            else:
                # What Claude Code reads, not what PyYAML loaded: the description as the CLI
                # shows it (trimmed, a number or boolean as JavaScript prints it), and the
                # boolean as the CLI reads `yes`/`off`/`1`. A skill with no description has
                # none here; the CLI then uses the first line of the content.
                description = readings.get("description", (None, None))[1]
                # Guard anyway so a future reader change can't feed a non-str into
                # redact_secret_lines.
                item["description"] = (
                    cw.redact_secret_lines(description)
                    if isinstance(description, str)
                    else description
                )
                item["disable_model_invocation"] = bool(
                    readings.get("disable-model-invocation", (None, False))[1]
                )
        # Member enumeration is symlink-SAFE: a symlinked member (or one under a
        # symlinked subdir) is skipped, so a symlink pointing at an outside file is
        # never listed — and therefore never offered up for a later read that would leak
        # its target's content. Keys are normalized to forward slashes (``as_posix``) so
        # the API returns ``scripts/x.sh`` on every OS — these are logical member keys,
        # not host filesystem paths.
        try:
            entry_resolved = entry.resolve()
            item["files"] = sorted(
                p.relative_to(entry).as_posix()
                for p in entry.rglob("*")
                if p != skill_md and _is_contained_regular_file(p, entry_resolved)
            )
        except OSError as exc:
            # Same per-entry containment as the SKILL.md read above — an unreadable skill
            # dir must not fail the whole listing. Reported, never silently empty: bare
            # `files: []` is indistinguishable from a genuinely empty skill, which would
            # make an unreadable directory look healthy.
            item["files"] = []
            item["files_error"] = f"members could not be listed: {exc.strerror or exc}"
        out.append(item)
    return out


def read_skill_file(
    base: Path, name: str, relative: str = SKILL_FILENAME
) -> tuple[str, str, bool]:
    """Return ``(redacted_content, hash, exists)`` for one file inside a skill directory.

    The file exactly as it is on disk (redacted); :func:`read_skill_file_for_edit` is the
    editor's view, which may also convert it.
    """
    content, file_hash, exists, _note = _read_skill_file(base, name, relative, convert=False)
    return content, file_hash, exists


def read_skill_file_for_edit(
    base: Path, name: str, relative: str = SKILL_FILENAME
) -> tuple[str, str, bool, str | None]:
    """Return ``(redacted_content, hash, exists, converted)`` for one file in a skill directory.

    ``converted`` is None unless the file is a ``SKILL.md`` that this surface would refuse
    only because some fields hold a value Claude Code ignores exactly as if the field were
    absent. Then ``content`` is the file with those fields removed (every other byte as on
    disk), and ``converted`` is a note naming them, for the editor to show; saving the text
    writes the repaired file. ``hash`` is always the hash of the bytes ON DISK, so the
    stale-hash guard still compares against the file the operator loaded.
    """
    return _read_skill_file(base, name, relative, convert=True)


def _read_skill_file(
    base: Path, name: str, relative: str, *, convert: bool
) -> tuple[str, str, bool, str | None]:
    """Read one skill file once, for :func:`read_skill_file` and :func:`read_skill_file_for_edit`.

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
    note = None
    if convert and exists and relative == SKILL_FILENAME:
        conversion = _convert_ignored_fields(text)
        if conversion is not None:
            text, removed = conversion
            fields = ", ".join(repr(key) for key in removed)
            note = (
                f"Claude Code ignores the value of {fields}, exactly as if the field were not "
                "there, so the editor removed it from the text below. Save to write the "
                "repaired file."
            )
    # Redact AFTER converting: the conversion only removes lines, so the text it returns
    # holds nothing that was not in the file, and the redactor still sees all of it.
    content = cw.redact_secret_lines(text)
    return content, cw.hash_bytes(raw), exists, note


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
        """Write every skill member into the private staging tree, byte-exact."""
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


def read_project_skill_file_for_edit(
    project_dir: Path, name: str, relative: str = SKILL_FILENAME
) -> tuple[str, str, bool, str | None]:
    """Read one file from a project-scope skill directory for the editor (may convert)."""
    return read_skill_file_for_edit(project_dir / ".claude", name, relative)


def read_user_skill_file_for_edit(
    claude_json: Path, name: str, relative: str = SKILL_FILENAME
) -> tuple[str, str, bool, str | None]:
    """Read one file from a user-scope skill directory for the editor (may convert)."""
    return read_skill_file_for_edit(claude_json.parent / ".claude", name, relative)


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

    ``candidate`` must be a ``dict`` mapping a skill name to one of
    :data:`SKILL_OVERRIDE_VALUES`. An invalid name or an unrecognized value rejects
    the whole write (→ 422), so a partial/garbled override map never lands.

    The CLI's schema takes any string key and matches it, exactly, against skill names. A
    skill name is the directory name, a frontmatter ``name`` or a bundled skill's name, so
    a key is accepted when Claude Code can show it as a name (:func:`cli_shows_name`).
    ``__proto__`` is refused: the CLI drops that key when it loads settings.
    """
    if not isinstance(candidate, dict):
        raise cw.InvalidCandidateError("skillOverrides must be an object")
    for name, value in candidate.items():
        if not isinstance(name, str) or name == "__proto__" or not cli_shows_name(name):
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

    :func:`~clauster.config_write.ensure_gitignored` runs *before* the write (fail-closed:
    the write creates a secret-bearing ``.bak``), so ``settings.local.json`` and that
    backup are never accidentally committed (#766).
    """
    cw.validate_candidate(incoming, validate_skill_overrides)
    path = cw.project_local_settings_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ignore BEFORE writing, never after: the write creates a `.bak` holding the PREVIOUS
    # values, so an ensure_gitignored that failed afterwards would leave that secret-bearing
    # file trackable. Ignoring first is the fail-closed order (the call is idempotent).
    cw.ensure_gitignored(project_dir, ".claude/settings.local.json", ignore_backup_sibling=True)
    cw.write_settings_subtree(path, SKILL_OVERRIDES_KEY, incoming, expected_hash)
