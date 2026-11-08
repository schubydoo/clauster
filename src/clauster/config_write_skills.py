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
choice than CLAUDE.md's, not an oversight. A ``url`` value in the frontmatter of any member
is also given the URL view the MCP and Hooks tabs use, and each ``mcpServers`` and hook
value those tabs mask whole is masked (:func:`_member_view`, TASK-525, TASK-529).

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
guard; documented here rather than silently assumed. Such an edit is not lost, though: a
save keeps every member it does not name, and it re-checks the hash and copies the kept
members inside the same lock as the swap (:func:`write_skill`). A process that writes the
skill directory without that lock (a text editor, a shell) is outside it: a change it
makes to a kept member in the instant between the copy and the swap is lost.
"""

from __future__ import annotations

import base64
import bisect
import errno
import itertools
import math
import os
import re
import stat
import unicodedata
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from . import config_file_writer as fw
from . import config_write as cw
from .config_write_hooks import RECOGNIZED_EVENTS, mask_url, validate_hooks

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
#: The most entries (files and directories, at any depth) a save keeps from a stored skill.
#: The byte cap does not bound them: empty files and directories count no bytes, and the
#: save lists and copies each one under its lock (TASK-500).
MAX_STORED_ENTRIES = 4096

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

#: The hook events the CLI knows (its ``bc``, 2.1.318): the hooks surface's own set, so
#: the two cannot drift apart.
_HOOK_EVENTS = RECOGNIZED_EVENTS
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
    """Return what JavaScript ``parseInt(text, 10)`` returns, or None for ``NaN``/``Infinity``."""
    match = _JS_PARSE_INT_RE.match(text)
    return cw.js_integer(match.group(1)) if match else None


def _cli_effort(value: Any) -> str | int | None:
    """Return the effort the CLI reads from ``value`` (its ``dq``), or None when it ignores it.

    A number must be a whole number, and any whole number counts. Anything else is
    stringified and lowercased (not trimmed), mapped through the alias table, and matched
    against the level names. Failing that, ``parseInt`` decides, so ``"3 tokens"`` is ``3``.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        try:
            float(value)  # past the double range JavaScript holds Infinity: ignored
        except OverflowError:
            return None
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


def _holds_a_mapping(value: Any) -> bool:
    """Whether ``value`` is a mapping or holds one at any depth of nested lists.

    Iterative, and each list is walked once (an alias makes one list object appear many
    times), so neither depth nor sharing costs stack frames or repeated work. Greptile on
    PR 1810: ``[[{a: 1}]]`` passed a one-level check and then showed as ``[object Object]``.
    """
    seen: set[int] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            return True
        if isinstance(item, list) and id(item) not in seen:
            seen.add(id(item))
            stack.extend(item)
    return False


def _read_text_field(key: str, value: Any) -> str | None:
    """Read a field the CLI turns into text with ``String()``: a scalar or a list of them.

    ``argument-hint: [issue-number]`` is a YAML list, and the CLI shows it as
    ``issue-number``. A mapping, alone or inside a list, becomes ``[object Object]``, which
    is refused. A null is "not set".
    """
    if value is None:
        return None
    try:
        if _holds_a_mapping(value):
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
#: Group 1 is the header the CLI reads.
_CLI_FENCE_RE = re.compile(f"---[{_JS_WHITESPACE}]*\n([\\s\\S]*?)---")


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
    with cw.refuse_check_failures(f"{SKILL_FILENAME} frontmatter"):
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
        with cw.refuse_check_failures(f"{SKILL_FILENAME} frontmatter"):
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


def _stored_skill_dir(skills_root: Path, name: str) -> Path:
    """Return the resolved skill directory ``skills_root/name``; refuse one that is not (→ 422).

    Containment first (:func:`~clauster.config_file_writer.resolve_contained_path`). A
    missing directory is returned as it is: a create, or a read of nothing. A regular file
    (or any other entry) at ``skills/<name>`` is the operator's own state, not a server
    fault, so it is a refusal that names it, never a 500 (TASK-500).
    """
    try:
        skill_dir = fw.resolve_contained_path(skills_root, name)
    except fw.PathEscapeError as exc:
        raise cw.PathEscapeError(str(exc)) from exc
    try:
        info = os.lstat(skill_dir)
    except FileNotFoundError:
        return skill_dir
    except OSError as exc:
        raise cw.InvalidCandidateError(
            f"skill {name!r} could not be read: {exc.strerror or exc}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise cw.InvalidCandidateError(
            f"skill {name!r} is not a directory: skills/{name} is a file or another entry, so "
            "it holds no skill. Move it aside on the host, then try again"
        )
    return skill_dir


def _unreadable(relative: str, exc: OSError) -> cw.InvalidCandidateError:
    """Return the refusal for a skill member that the file system would not let us read."""
    return cw.InvalidCandidateError(f"{relative!r} could not be read: {exc.strerror or exc}")


def _read_member_bytes(path: Path, relative: str) -> bytes | None:
    """Return the bytes of the regular file at ``path``, or None when nothing is there.

    Never blocks and never follows a final symlink. ``lstat`` comes first, so a directory,
    a FIFO, a socket, a device or a symlink is refused (→ 422) before any open. The open
    uses :data:`_OPEN_FLAGS` (``O_NOFOLLOW`` and ``O_NONBLOCK``), and the open descriptor
    must be that same regular file (``fstat``). An entry that cannot be read is refused
    too, naming the cause: never a 500, and never a worker thread blocked on a FIFO
    (TASK-500, TASK-531). A path through a file (``NotADirectoryError``) holds nothing.
    """
    try:
        seen = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise _unreadable(relative, exc) from exc
    if stat.S_ISDIR(seen.st_mode):
        raise cw.InvalidCandidateError(f"{relative!r} is a directory in the skill, not a file")
    if not stat.S_ISREG(seen.st_mode):
        raise cw.InvalidCandidateError(
            f"{relative!r} is a symlink or not a regular file (a FIFO, a socket or a device), "
            "so it is not read. Replace it with a regular file on the host"
        )
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        raise _unreadable(relative, exc) from exc
    try:
        fh = os.fdopen(fd, "rb")
    except BaseException:  # pragma: no cover - fdopen of a fresh descriptor does not fail
        os.close(fd)  # os.fdopen did not take the descriptor, so close it here
        raise
    with fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or not _same_object(info, seen):
            raise cw.InvalidCandidateError(f"{relative!r} changed while it was read. Try again")
        try:
            return fh.read()
        except OSError as exc:  # pragma: no cover - EIO from the disk itself
            raise _unreadable(relative, exc) from exc


def _skill_md_bytes(skills_root: Path, name: str) -> bytes:
    """Return the current on-disk ``SKILL.md`` bytes (``b""`` if absent).

    Read as :func:`_read_member_bytes` reads, so a FIFO at ``SKILL.md`` cannot block the
    save's check outside the lock nor its re-check inside it, and a symlinked ``SKILL.md``
    (which the save's walk refuses anyway) is refused here first (TASK-500).
    """
    skill_dir = _stored_skill_dir(skills_root, name)
    data = _read_member_bytes(skill_dir / SKILL_FILENAME, SKILL_FILENAME)
    return b"" if data is None else data


def _skill_md_hash(skills_root: Path, name: str) -> str:
    """Return the hash of the current on-disk ``SKILL.md`` bytes (empty digest if absent)."""
    return cw.hash_bytes(_skill_md_bytes(skills_root, name))


#: The Windows reparse-tag bit that marks a link: a symlink, a junction, a WSL symlink.
#: ``st_reparse_tag`` exists only on Windows, so elsewhere the test below is ``0 & bit``.
_NAME_SURROGATE_BIT = 0x20000000

#: How a stored member is opened: never through a final symlink, never blocking on a FIFO.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)

#: How a file is made in staging: a new file only, never through a symlink at its path.
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)

#: Bits a kept file or directory never takes from the stored tree: setuid and setgid.
#: The sticky bit stays: on a world-writable directory it is what stops another user
#: from renaming over or deleting the owner's files, so clearing it would widen access.
_SET_ID_BITS = stat.S_ISUID | stat.S_ISGID


def _create_exclusive(path: Path, data: bytes) -> None:
    """Write ``data`` to a new file at ``path``; fail if anything is there, a symlink too.

    ``O_EXCL`` with ``O_CREAT`` fails on any existing entry and never follows a symlink,
    so a file or a symlink planted at ``path`` makes the write fail closed.
    """
    fd = os.open(path, _CREATE_FLAGS, 0o666)
    try:
        out = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)  # os.fdopen did not take the descriptor, so close it here
        raise
    with out:
        out.write(data)


def _kept_mode(path: Path, info: os.stat_result, mode: int) -> int:
    """Give ``path`` the stored group of ``info``, and return the mode it may then take.

    A new file or directory gets the process's group (Linux) or its parent's (macOS), and
    that group can hold every local account (``staff``). So the stored group is set back
    when the user may; when not, the group bits are cleared, never handed to another group.
    """
    if os.lstat(path).st_gid != info.st_gid:
        try:
            os.chown(path, -1, info.st_gid, follow_symlinks=False)
        except OSError:
            mode &= ~stat.S_IRWXG
    return mode


def _make_private_dirs(staging: Path, directory: Path, relative: str) -> list[Path]:
    """Create the missing directories from ``staging`` down to ``directory``, owner-only.

    Return the new ones, relative to ``staging``. A component that exists and is not a
    directory is refused, naming ``relative``, the request key being written.
    """
    created: list[Path] = []
    current = staging
    for part in directory.relative_to(staging).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError as exc:
            if not current.is_dir():
                raise cw.InvalidCandidateError(
                    f"{relative!r} is inside a path that the skill holds as a file"
                ) from exc
        else:
            created.append(current.relative_to(staging))
    return created


def _default_dir_mode(staging: Path) -> int:
    """Return the mode a new directory gets here (``0o777`` less the umask), read safely.

    The probe directory lives in the owner-only staging tree, so no one else sees it, and
    the umask is never changed (``os.umask`` is process-wide and not thread-safe).
    """
    probe = staging / ".mode-probe"
    probe.mkdir()
    try:
        return stat.S_IMODE(probe.stat().st_mode)
    finally:
        probe.rmdir()


def _same_object(a: os.stat_result, b: os.stat_result) -> bool:
    """Whether two stat results describe the same file-system object."""
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _changed(member: Path) -> cw.InvalidCandidateError:
    """Return the refusal for a stored member that changed while the save copied it."""
    return cw.InvalidCandidateError(
        f"skill member {member.as_posix()!r} changed while it was saved. Nothing was "
        "written. Save again"
    )


def _list_stored_dir(
    path: Path, expected: os.stat_result, member: Path, *, limit: int
) -> list[tuple[str, os.stat_result]]:
    """Return ``(name, lstat)`` for each entry of the stored directory ``path``.

    The listing is of the directory the walk saw (``expected``), never of a symlink
    swapped in since: on POSIX the directory is opened with ``O_NOFOLLOW``, its identity
    is checked on the open descriptor, and the entries are read from that descriptor. It
    stops after ``limit + 1`` entries, so a huge directory costs no more than the walk's
    entry cap: a result longer than ``limit`` means the directory holds too many.
    """
    if os.scandir in os.supports_fd:
        fd = os.open(path, _OPEN_FLAGS | getattr(os, "O_DIRECTORY", 0))
        try:
            if not _same_object(os.fstat(fd), expected):
                raise _changed(member)
            with os.scandir(fd) as entries:
                return [
                    (e.name, e.stat(follow_symlinks=False))
                    for e in itertools.islice(entries, limit + 1)
                ]
        finally:
            os.close(fd)
    # Windows has no descriptor listing: list by path, then make sure that the directory
    # is still the one the walk saw. A swap and a swap back inside that instant remain.
    with os.scandir(path) as entries:  # pragma: no cover - Windows only
        listed = [(e.name, os.lstat(e.path)) for e in itertools.islice(entries, limit + 1)]
    if not _same_object(os.lstat(path), expected):  # pragma: no cover - Windows only
        raise _changed(member)
    return listed  # pragma: no cover - Windows only


def _copy_stored_file(src: Path, dst: Path, expected: os.stat_result, member: Path) -> None:
    """Copy one stored file byte for byte, with its mode bits (POSIX) and times.

    The open descriptor must be the very file the listing showed (``expected``), at the
    size the listing showed, so a path component swapped for a symlink since the listing
    is refused, never followed, and a file that grows cannot push the tree over the cap.
    The setuid and setgid bits are cleared: the copy belongs to clauster's user, so they
    would make it run as that user.
    """
    try:
        fd = os.open(src, _OPEN_FLAGS)
    except OSError as exc:
        # The open fails when the entry is no longer the file that the listing showed: a
        # symlink (ELOOP from O_NOFOLLOW), or on Windows a directory (PermissionError).
        # That is the same change the identity check below refuses, so say so.
        try:
            now = os.lstat(src)
        except (FileNotFoundError, NotADirectoryError):
            raise _changed(member) from exc  # gone since the listing
        except OSError:
            raise exc from None  # unreadable is not changed: report the open's own error
        if not stat.S_ISREG(now.st_mode) or not _same_object(now, expected):
            raise _changed(member) from exc
        raise
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or not _same_object(info, expected)
            or info.st_size != expected.st_size
        ):
            raise _changed(member)
        fh = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)  # os.fdopen did not take the descriptor, so close it here
        raise
    with fh:
        data = fh.read(info.st_size + 1)
    if len(data) != info.st_size:  # pragma: no cover - a change between fstat and read
        raise _changed(member)
    _create_exclusive(dst, data)
    # A kept file keeps its own mode and group, group and world write included: that is
    # the operator's choice on their own file, and the copy never gets wider than it.
    if fw._is_posix():  # pragma: skip-on-win  # pragma: no branch - Windows skips it
        os.chmod(dst, _kept_mode(dst, info, stat.S_IMODE(info.st_mode) & ~_SET_ID_BITS))
    os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns))


def _keep_replaced_mode(
    target: Path, stored_path: Path, replaced_files: dict[tuple[int, int], os.stat_result]
) -> None:
    """Give the new text of a replaced member the mode of the stored file it replaces (POSIX).

    ``stored_path`` is the request key in the stored skill, looked up by the file system's
    own name rules, as the walk matched it. Its mode is taken only from an entry that the
    walk recorded as replaced (the same ``st_dev`` and ``st_ino``), so a new member keeps
    the default mode, and an executable script keeps its x bit when its text is saved
    (TASK-500), as :func:`~clauster.config_file_writer.write_file` keeps a mode on replace.
    As for a kept file (:func:`_copy_stored_file`), setuid and setgid are cleared, and the
    stored group is set back or the group bits are cleared.
    """
    try:
        now = os.lstat(stored_path)
    except OSError:
        return  # nothing is stored under the key: a new member
    stored = replaced_files.get((now.st_dev, now.st_ino))
    if stored is not None:
        mode = stat.S_IMODE(stored.st_mode) & ~_SET_ID_BITS
        os.chmod(target, _kept_mode(target, stored, mode))


def _fold(parts: tuple[str, ...]) -> tuple[str, ...]:
    """Return path parts folded loosely for case and Unicode normalization."""
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)


def _stored_spelling(skill_dir: Path, parts: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the stored names of the entry the file system maps ``parts`` to, or None.

    Component by component: a name stored exactly is that entry. Otherwise the file
    system's own name rules decide, never a guess by OS: the component is looked up
    (``RUN.sh`` reaches ``run.sh`` on APFS or NTFS), and the entry is the stored one only
    when all of these hold. The object the lookup reached is the only entry in the
    listing with its identity, so no hard link or ``st_ino`` of 0 can stand in for it. A
    file has no other hard link anywhere (``st_nlink`` 1). Its name folds to the key. A
    rule the fold does not model (an NTFS 8.3 short name) then never picks a name.
    Anything else is None: the save keeps and counts the stored file, and the named
    write refuses if the key then collides with that copy. A wrong None only refuses or
    over-counts. A wrong match would delete a stored file. ``os.listdir`` follows a
    stored symlink in a parent component, which does no harm: the walk refuses the link.
    """
    current = skill_dir
    out: list[str] = []
    for part in parts:
        try:
            names = os.listdir(current)
            if part in names:
                out.append(part)
                current = current / part
                continue
            target = os.lstat(current / part)
            same = [n for n in names if _same_object(os.lstat(current / n), target)]
        except OSError:
            return None
        if (
            len(same) != 1
            or target.st_ino == 0
            or (not stat.S_ISDIR(target.st_mode) and target.st_nlink != 1)
            or _fold((same[0],)) != _fold((part,))
        ):
            return None
        out.append(same[0])
        current = current / same[0]
    return tuple(out)


def _replaced_by(skill_dir: Path, files: dict[str, Any]) -> Callable[[Path], bool]:
    """Return a test of whether a stored file is one of the members the request names.

    A key names a member by its parts, as :func:`~clauster.config_file_writer.resolve_
    contained_path` reads it (``a//b`` names ``a/b``), or by the stored spelling that the
    file system maps it to (:func:`_stored_spelling`). A stored name is never matched by
    a fold alone: on a case-sensitive file system ``run.sh`` and ``RUN.sh`` (or an NFC
    and an NFD name) are two members, even as hard links of one file, and NTFS keeps
    ``ss.md`` and ``ß.md`` apart. Matching on a fold would drop a stored file.
    """
    named = {Path(relative).parts for relative in files}
    spelled = set(named)
    for parts in named:
        if Path(*parts).is_absolute() or any(p in ("", ".", "..") for p in parts):
            continue  # not a path the save can write: resolve_contained_path refuses it
        stored = _stored_spelling(skill_dir, parts)
        if stored is not None:
            spelled.add(stored)

    def _replaced(member: Path) -> bool:
        """Whether the stored file ``member`` is one the save replaces."""
        return member.parts in spelled

    return _replaced


def _keep_stored_members(
    skill_dir: Path,
    staging: Path,
    replaced: Callable[[Path], bool],
    *,
    name: str,
    sent: int,
) -> tuple[list[tuple[Path, os.stat_result]], dict[tuple[int, int], os.stat_result]]:
    """Copy into ``staging`` every stored member of the skill that the request does not name.

    A save keeps every member it does not name (TASK-496). The walk follows no symlink
    inside the skill directory (``skill_dir`` itself is already resolved). A symlink, a
    Windows junction, or a member that is neither a directory nor a regular file (a
    FIFO, a socket, a device) is refused (→ 422) and nothing is written: clauster
    cannot keep such a member byte for byte without following it, and dropping it would
    delete the operator's data. The byte cap covers the kept files plus the ``sent`` bytes,
    and the copy stops as soon as they pass it. A tree of more than
    :data:`MAX_STORED_ENTRIES` entries is refused the same way, as soon as the walk lists
    one too many (TASK-500).

    Return the kept directories, and the ``lstat`` of each stored file that the request
    replaces, by ``(st_dev, st_ino)``, so its new text can keep the stored mode.
    """
    try:
        root = os.lstat(skill_dir)
    except FileNotFoundError:
        return [], {}  # a create: nothing is stored yet
    dirs: list[tuple[Path, os.stat_result]] = [(Path(), root)]
    pending = [(Path(), root)]
    replaced_files: dict[tuple[int, int], os.stat_result] = {}
    kept = 0
    entries = 0
    while pending:
        rel, expected = pending.pop()
        listing = _list_stored_dir(
            skill_dir / rel, expected, rel, limit=MAX_STORED_ENTRIES - entries
        )
        entries += len(listing)
        if entries > MAX_STORED_ENTRIES:
            raise cw.InvalidCandidateError(
                f"skill {name!r} holds more than {MAX_STORED_ENTRIES} stored files and "
                "directories, so the save cannot keep them all. Nothing was written, and no "
                "stored file was dropped. Remove stored files on the host, then save again"
            )
        for entry_name, info in listing:
            member = rel / entry_name
            reparse = getattr(info, "st_reparse_tag", 0)
            link = stat.S_ISLNK(info.st_mode) or bool(reparse & _NAME_SURROGATE_BIT)
            if not link and stat.S_ISDIR(info.st_mode):
                (staging / member).mkdir(mode=0o700)  # its own mode comes at the promote
                dirs.append((member, info))
                pending.append((member, info))
            elif not link and stat.S_ISREG(info.st_mode):
                if replaced(member):
                    replaced_files[(info.st_dev, info.st_ino)] = info
                    continue  # the request replaces it
                kept += info.st_size
                if kept + sent > MAX_TOTAL_BYTES:
                    # No byte count in the message: `sent` counts the text with its masked
                    # values kept, and `kept` counts files the editor shows masked, so either
                    # number would tell the length of a hidden value in one answer. Whether
                    # the save is refused still answers yes or no, so repeated saves can find
                    # that length; that residual is documented, not hidden (invariant 4 is
                    # about the value, never its length).
                    raise cw.InvalidCandidateError(
                        f"skill {name!r} would be over the {MAX_TOTAL_BYTES} byte cap: the save "
                        "and the stored files it keeps are too large together. Nothing was "
                        "written, and no stored file was dropped to fit. Remove or shrink a "
                        "stored file, then save again"
                    )
                _copy_stored_file(skill_dir / member, staging / member, info, member)
            else:
                raise cw.InvalidCandidateError(
                    f"skill member {member.as_posix()!r} is a symlink or not a regular "
                    "file, so the save cannot keep it without following it. Nothing was "
                    "written. Replace it with a regular file, then save again"
                )
    return dirs, replaced_files


def _set_final_dir_modes(
    staging: Path,
    kept: list[tuple[Path, os.stat_result]],
    created: list[Path],
    default_mode: int,
) -> None:
    """Give every staging directory its final mode and times: the last step before promote.

    Until now every directory is owner-only (on POSIX), so no other user could change
    staging while the save wrote into it. A kept directory gets its stored mode and group,
    without setuid or setgid (the sticky bit stays), and with owner rwx, so that a later
    save can remove the old tree. A new directory gets ``default_mode``. The skill
    directory itself never gets group or world write.

    Deepest first and the root last. ``os.chmod`` and ``os.utime`` follow symlinks in the
    path, so every directory above the one being changed must still be owner-only: then
    no other user can swap a part of that path for a symlink and turn the call onto a
    file outside staging. Until the root opens, no one can reach a directory below it.
    """
    stored = dict(kept)
    modes = {
        rel: (stat.S_IMODE(info.st_mode) & ~_SET_ID_BITS) | stat.S_IRWXU for rel, info in kept
    }
    for rel in created:
        modes.setdefault(rel, default_mode)
    modes[Path()] = modes.get(Path(), default_mode) & ~(stat.S_IWGRP | stat.S_IWOTH)
    for rel in sorted(modes, key=lambda r: len(r.parts), reverse=True):
        if fw._is_posix():  # pragma: skip-on-win  # pragma: no branch - Windows skips it
            mode = modes[rel]
            if rel in stored:
                mode = _kept_mode(staging / rel, stored[rel], mode)
            os.chmod(staging / rel, mode)
        if rel in stored:
            os.utime(staging / rel, ns=(stored[rel].st_atime_ns, stored[rel].st_mtime_ns))


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
                # Never blocks: a SKILL.md swapped for a FIFO since the is_file() above is
                # refused before any open (TASK-500).
                data = _read_member_bytes(skill_md, SKILL_FILENAME)
                if data is None:
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory")
                text = data.decode("utf-8")
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


def _masked_whole(line: str) -> str:
    """Return ``line`` with its text replaced by the mask; a blank line stays as it is."""
    body = line.splitlines()[0] if line else ""
    return line if not body.strip() else cw.REDACTION_SENTINEL + line[len(body) :]


def _url_nodes(root: yaml.Node) -> list[yaml.ScalarNode]:
    """Return each scalar node that a ``url`` key holds anywhere under ``root``, once each.

    Iterative, with a visited set, so an alias or a merge key that reuses a node costs one
    visit and a cyclic node graph ends.
    """
    found: dict[int, yaml.ScalarNode] = {}
    seen: set[int] = set()
    stack: list[yaml.Node] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                stack += (key, value)
                if (
                    isinstance(key, yaml.ScalarNode)
                    and key.value == "url"
                    and isinstance(value, yaml.ScalarNode)
                ):
                    found[id(value)] = value
        elif isinstance(node, yaml.SequenceNode):
            stack += node.value
    return list(found.values())


#: The top-level fields whose values the editor masks by their place in the field, and,
#: for each, the keys anywhere under it whose values are masked whole. The MCP tab masks
#: every ``env`` and ``headers`` value and a ``headersHelper`` (``_redact_entry``), and the
#: Hooks tab every hook ``headers`` value (TASK-529). ``args`` is not here: an ``args`` item
#: is masked only when its loaded value is secret-shaped, as the MCP tab shows it
#: (:func:`_secret_shaped_nodes`, TASK-535).
_MASKED_UNDER = {
    "mcpServers": frozenset({"env", "headers", "headersHelper"}),
    "hooks": frozenset({"headers"}),
}

_MERGE_TAG = "tag:yaml.org,2002:merge"
_BINARY_TAG = "tag:yaml.org,2002:binary"


def _field_values(root: yaml.Node) -> list[tuple[yaml.Node, frozenset[str]]]:
    """Return the value of each top-level :data:`_MASKED_UNDER` field, with its masked keys.

    A field of the top-level mapping, or of a mapping that a top-level merge key brings in.
    Iterative, with a visited set, so a merge key that names one mapping twice costs one
    visit and a cyclic node graph ends.
    """
    fields: list[tuple[yaml.Node, frozenset[str]]] = []
    seen: set[int] = set()
    stack: list[yaml.Node] = [root]
    while stack:  # the top-level mapping, and every mapping a top-level merge key brings
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.SequenceNode):
            stack += node.value  # a merge key's list of mappings
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if key.tag == _MERGE_TAG:
                    stack.append(value)
                elif isinstance(key, yaml.ScalarNode) and key.value in _MASKED_UNDER:
                    fields.append((value, _MASKED_UNDER[key.value]))
    return fields


def _masked_value_nodes(root: yaml.Node) -> list[yaml.ScalarNode]:
    """Return each non-empty scalar the editor masks whole by its place in the header.

    These are the scalars under a :data:`_MASKED_UNDER` key (``headers``, ``env``,
    ``headersHelper``) anywhere in the value of a top-level ``mcpServers`` or ``hooks``
    field (:func:`_field_values`), also one that a top-level merge key brings in. The key
    of a mapping (a header name, an ``env`` variable name) is not masked, and an empty
    value hides nothing. Three iterative walks, each with a visited set, so an alias or a
    merge key that reuses a node costs one visit and a cyclic node graph ends.
    """
    fields = _field_values(root)
    seen: set[int] = set()
    holders: list[yaml.Node] = []
    visited: set[tuple[int, frozenset[str]]] = set()
    while fields:  # the values of the masked keys under each field
        node, keys = fields.pop()
        if (id(node), keys) in visited:
            continue
        visited.add((id(node), keys))
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                fields += ((key, keys), (value, keys))
                if isinstance(key, yaml.ScalarNode) and key.value in keys:
                    holders.append(value)
        elif isinstance(node, yaml.SequenceNode):
            fields += ((item, keys) for item in node.value)
    found: dict[int, yaml.ScalarNode] = {}
    seen.clear()
    while holders:  # every scalar value inside them
        node = holders.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.ScalarNode):
            if node.value:
                found[id(node)] = node
        elif isinstance(node, yaml.MappingNode):
            holders += (value for _key, value in node.value)
        else:  # a sequence, the only other node kind
            holders += node.value
    return list(found.values())


def _loaded_text(node: yaml.ScalarNode) -> str | None:
    """Return the text ``node`` loads as, for its shape check; None if it does not load.

    A ``!!binary`` value loads as the bytes its base64 text decodes to, as PyYAML decodes
    it (:func:`base64.decodebytes`), read as UTF-8 as the browser would show them. Any other
    scalar is read as its text.
    """
    if node.tag != _BINARY_TAG:
        return node.value
    try:
        return base64.decodebytes(node.value.encode("ascii")).decode("utf-8", "replace")
    except ValueError:  # a non-ASCII character, or a broken base64 text
        return None


def _secret_shaped_nodes(root: yaml.Node, source: str) -> list[tuple[yaml.ScalarNode, bool]]:
    r"""Return each scalar of the header ``source`` that is masked by its loaded value.

    The MCP and Hooks tabs mask a value through :func:`~clauster.config_write.redact_secrets`,
    which reads the loaded value: a ``${...}`` reference, a URL with a user name or a
    password, and any value under a key that looks secret (``clientSecret``) at any depth.
    The line masker reads the source text, so it misses such a value written with an escape
    (``"https://u:pw\u0040h"``) or as ``!!binary``, and a value under a key that looks
    secret only once its escape is read (TASK-535). Returned:

    * a value whose loaded text the line masker masks (:func:`_shaped`: a ``${...}``
      reference, a URL with a user name or a password, a ``key=value`` or ``key: value``
      with a secret key, and the other line shapes), where its source holds a backslash or
      it is ``!!binary``, anywhere in the header. The loaded text is read as lines of its
      own, so such a value can be masked where the same text written plainly in its place
      shows (``args: ["--api\x2dkey=x"]``). Any other value reads in its source as it
      loads, so the line masker masks it there, as before, except a ``${...}`` that is
      split across source lines (best effort, documented);
    * a value under a key that looks secret, under an ``mcpServers`` or ``hooks`` field
      (:func:`_field_values`), as on the tab, and elsewhere under such a key whose source
      holds a backslash. Elsewhere the line masker reads any other key;
    * a ``!!binary`` ``url`` value: the ``url`` view (:func:`mask_url`) cannot read its
      base64 text;
    * a ``!!binary`` value that does not load (fail closed).

    Each pair is a node and whether it shows as the mask (``True``: a key above it, a
    ``!!binary`` ``url``, or a loaded value on more than one line) or is masked in place by
    its shape (``False``). Any other
    ``url`` value is left to the ``url`` view, as on the MCP tab. One iterative walk with a
    visited set of (node, under a secret key, under ``url``, in a field), so each node
    costs eight visits at most and a cyclic node graph ends.
    """
    stack = [(value, False, False, True) for value, _keys in _field_values(root)]
    stack.append((root, False, False, False))
    seen: set[tuple[int, bool, bool, bool]] = set()
    found: dict[int, tuple[yaml.ScalarNode, bool]] = {}
    while stack:
        node, secret, under_url, keyed = stack.pop()
        if (id(node), secret, under_url, keyed) in seen:
            continue
        seen.add((id(node), secret, under_url, keyed))
        if isinstance(node, yaml.ScalarNode):
            whole = node.value != "" and (secret or (under_url and node.tag == _BINARY_TAG))
            text = _loaded_text(node)
            hidden = node.tag == _BINARY_TAG or "\\" in _source_of(node, source)
            shaped = hidden and not under_url and text is not None and _shaped(text)
            if whole or text is None or shaped:
                # Belt and braces: a loaded value on more than one line never reaches the
                # in-place view anyway (its source is not one line that reads as the value).
                whole = whole or text is None or "\n" in text or "\r" in text
                by_key = whole or (id(node) in found and found[id(node)][1])
                found[id(node)] = (node, by_key)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                name = key.value if isinstance(key, yaml.ScalarNode) else ""
                hint = cw._SECRET_KEY_RE.search(name) is not None and (
                    keyed or "\\" in _source_of(key, source)
                )
                stack.append((value, secret or hint, name == "url", keyed))
        else:  # a sequence, the only other node kind
            stack += ((item, secret, False, keyed) for item in node.value)
    return list(found.values())


def _shaped(text: str) -> bool:
    """Whether the line masker masks ``text``, read as lines or as one whole text.

    :func:`~clauster.config_write.redact_secret_lines` reads each line alone, so a loaded
    ``${...}`` that spans a line break is found only by its scan of the whole text (the
    ``${...}`` and URL scans, as :func:`~clauster.config_write.redact_secrets` reads a
    value). Both run, each in one pass.
    """
    if cw.redact_secret_lines(text) != text:
        return True
    sentinel = cw.REDACTION_SENTINEL
    return cw._mask_url_creds(cw._mask_interps(text, sentinel), f"{sentinel}@") != text


def _source_of(node: yaml.Node, source: str) -> str:
    """Return the text of ``source`` that ``node`` was composed from."""
    return source[node.start_mark.index : node.end_mark.index]


def _shape_view(value: str) -> str:
    """Return ``value`` masked in place as the line masker masks it, or the mask if it does not.

    A ``${...}`` reference shows as the mask, a URL keeps its host
    (``[REDACTED]@h.example.com``) and a ``--api-key=x`` keeps its key, as when the value is
    written without an escape. The in-place view is used only for a value on one line, so
    the line count stays.
    """
    shown = cw.redact_secret_lines(value)
    return shown if shown != value else cw.REDACTION_SENTINEL


#: Text that may spell a value the header view masks: a ``url`` key, an http(s) or ws(s)
#: scheme (WHATWG needs no ``//`` after it), a ``mcpServers`` or ``hooks`` field
#: (:data:`_MASKED_UNDER`), a backslash, which can escape any of them in a double-quoted
#: scalar, and a ``binary`` tag, whose base64 text can hold a secret-shaped value
#: (:func:`_secret_shaped_nodes`). Fixed words only, so a search costs one pass.
_MAY_HOLD_MASKED_RE = re.compile(r"url|https?:|wss?:|mcpServers|hooks|binary|\\", re.IGNORECASE)


def _may_hold_masked(text: str) -> bool:
    r"""Whether ``text`` may spell a value the header view masks.

    Each word of :data:`_MAY_HOLD_MASKED_RE`, and a tag with a ``%`` escape, which can spell
    ``binary`` (``!!%62inary``): one word with a ``%`` after its first ``!``. A header with
    none of them holds no such value: any secret-shaped value in it reads in its source as
    it loads, so the line masker masks it. Linear: one regex search of fixed words, then
    one split and one forward look at each word (a regex such as ``![^\s]*%`` scans the
    rest of a run of ``!`` again from each ``!``).
    """
    if _MAY_HOLD_MASKED_RE.search(text) is not None:
        return True
    return any("%" in word[word.index("!") :] for word in text.split() if "!" in word)


def _yaml12_may_refuse(source: str, root: yaml.Node | None) -> bool:
    """Whether YAML 1.2 may refuse ``source`` that PyYAML composed to ``root``.

    Claude Code then reads its re-quoting retry, where the lines after a quoted value that
    continues at column 0 can become real keys (:func:`~clauster.config_write.
    _yaml12_refuses`). A check that cannot finish counts as a refusal (fail closed).
    """
    try:
        return cw._yaml12_refuses(source, root) is not None
    except (*cw._CHECK_FAILURES, RecursionError):
        return True


def _mask_header_values(header: str, *, readable: bool) -> str:
    """Return a frontmatter ``header`` with each ``url`` and each server or hook secret masked.

    A ``url`` value is shown as :func:`mask_url` shows it. A value the MCP or Hooks tab masks
    whole (:func:`_masked_value_nodes`: an ``mcpServers`` ``headers``, ``env`` or
    ``headersHelper`` value, a hook ``headers`` value) shows as the sentinel (TASK-529);
    that wins when a value is both. A value that is masked by its loaded value
    (:func:`_secret_shaped_nodes`: a value written with an escape or as ``!!binary``
    anywhere whose loaded text the line masker masks, a value under a secret key in those
    fields, a ``!!binary`` ``url``) is masked as well: as the sentinel under a key that looks
    secret and for such a ``url``, else in place as the line masker masks it
    (:func:`_shape_view`, TASK-535). Each is found by its node in the composed YAML, so a
    value in a flow mapping, behind an alias, or under a merge key is found as well as a
    plain ``key: value`` line. Where the value's source text is one line and reads as the
    value (a plain scalar, or a quoted one with no escape), only that text is replaced: for
    a ``url``, the scheme, host, port, plain path and plain query keys stay visible
    (TASK-525), and a quote around a masked value stays. Any other spelling (a multi-line
    or block value, an escape, an anchor or tag before the value) has every line of the
    value masked whole.

    The node positions are trusted only where PyYAML reads the structure Claude Code reads.
    Every non-blank line is masked whole where it may not: a header that does not compose
    (Claude Code's YAML 1.2 can still read it, for example a tab before a comment), one
    that holds ``---`` (Claude Code ends the header there), one with a YAML 1.1-only line
    break, a fence only Claude Code reads (``readable`` false), and a header that may hold a
    masked value (:func:`_may_hold_masked`) and either composes only through Claude
    Code's re-quoting retry (the CLI may not need the retry and read it as written), holds
    a ``?`` in a flow collection that the two YAML versions split (``[?sk-x]`` is a key to
    PyYAML, so its text is no masked value, and an ``args`` item to the CLI,
    :func:`~clauster.config_write._flow_question_mark`), or holds syntax YAML 1.2 refuses
    (the CLI then reads the retry, :func:`_yaml12_may_refuse`). A save of ``SKILL.md``
    refuses all but the retry case, so masking them whole costs a retype at most. The line
    count never changes.

    Two checks on the text come first. A header longer than :data:`MAX_FILE_BYTES`, which
    no save can write, is masked whole without being scanned or parsed, so a large member
    on disk costs one pass to show. A header with no text that a masked value can be
    spelled with (:func:`_may_hold_masked`: a ``url``, an ``mcpServers`` or a ``hooks`` key
    in any case, a scheme, a backslash escape or a binary tag) holds no such value and is
    returned as it is.
    """
    lines = header.splitlines(keepends=True)
    if len(header.encode("utf-8")) > MAX_FILE_BYTES:
        return "".join(_masked_whole(line) for line in lines)
    if not _may_hold_masked(header):
        return header
    try:
        source = cw.cli_frontmatter_text(header)
        root = yaml.compose(source, Loader=yaml.SafeLoader)
        composed = True
    except (yaml.YAMLError, RecursionError):
        source, root, composed = header, None, False
    if (
        not readable
        or not composed
        or "---" in header
        or any(char in header for char in "\x85\u2028\u2029")
        or source != header
        or cw._flow_question_mark(source) is not None
        or _yaml12_may_refuse(source, root)
    ):
        return "".join(_masked_whole(line) for line in lines)
    if root is None:
        return header  # an empty or comment-only header holds no value
    starts = list(itertools.accumulate(map(len, source.splitlines(keepends=True)), initial=0))
    if len(starts) != len(lines) + 1:  # pragma: no cover - the retry keeps every line
        return "".join(_masked_whole(line) for line in lines)
    spans: dict[int, list[tuple[int, int, str]]] = {}
    whole: set[int] = set()
    masked = _masked_value_nodes(root)
    taken = {id(node) for node in masked}
    views = [(node, cw.REDACTION_SENTINEL) for node in masked]
    for node, by_key in _secret_shaped_nodes(root, source):
        if id(node) not in taken:
            taken.add(id(node))
            views.append((node, cw.REDACTION_SENTINEL if by_key else _shape_view(node.value)))
    for node in _url_nodes(root):
        if id(node) not in taken:
            views.append((node, mask_url(node.value)))
    for node, shown in views:
        if shown == node.value:
            continue
        begin, end = node.start_mark.index, node.end_mark.index
        first = bisect.bisect_right(starts, begin) - 1
        last = bisect.bisect_right(starts, max(end - 1, begin)) - 1
        quote = 0 if node.style is None else 1
        inner = (begin + quote, end - quote)
        if (
            source == header
            and first == last
            and node.style in (None, "'", '"')
            and header[inner[0] : inner[1]] == node.value
        ):
            local = (inner[0] - starts[first], inner[1] - starts[first], shown)
            spans.setdefault(first, []).append(local)
        else:
            whole.update(range(first, last + 1))
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in whole:
            out.append(_masked_whole(line))
            continue
        # One pass, left to right: the pieces between the spans, then each span's view.
        prev = 0
        for start, stop, shown in sorted(spans.get(index, ())):
            out += (line[prev:start], shown)
            prev = stop
        out.append(line[prev:])
    return "".join(out)


def _spells_skill_md(relative: str) -> bool:
    r"""Whether the member key ``relative`` spells ``SKILL.md``, in any case or path spelling.

    The resolver reads ``./SKILL.md``, ``SKILL.md/``, ``.//SKILL.md`` and ``SKILL.md/.`` (and
    ``.\SKILL.md`` on Windows) as ``SKILL.md``, because :class:`pathlib.Path` drops the
    ``.`` parts and the separators, and a case variant reaches the same file on macOS and
    Windows. Windows also opens ``SKILL.md`` for ``SKILL.md.``, ``SKILL.md `` and
    ``SKILL.md::$DATA``, so trailing dots and spaces and a ``:`` suffix are dropped first.
    Every rule applies on every OS, so this can only call more keys ``SKILL.md``, never
    fewer.
    """
    parts = Path(relative).parts
    if len(parts) != 1:
        return False
    stem = parts[0].split(":", 1)[0].rstrip(" .")
    return stem.casefold() == SKILL_FILENAME.casefold()


def _member_view(text: str) -> str:
    """Return the editor's read view of one skill member holding ``text``.

    Every member is masked line by line (:func:`~clauster.config_write.redact_secret_lines`),
    and each ``url`` value and each server or hook secret in a leading frontmatter block is
    masked first (:func:`_mask_header_values`), as the MCP and Hooks tabs mask them
    (TASK-525, TASK-529). Decided
    from the text alone, never from the member's name or file identity: a request can reach
    a ``SKILL.md`` as ``./SKILL.md``, through a link to any skill's ``SKILL.md`` or through a
    hard link, and a save breaks a hard link while it keeps the text. A member without a
    frontmatter fence gets only the line masks, as before. The write path binds a resent
    mask through this same function (:func:`_keep_masked_lines`), so the read and the
    restore never disagree.
    """
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    core = text[len(bom) :]
    match = _FRONTMATTER_RE.match(core)
    if match is not None:
        header, start = cw.frontmatter_header(core, match), match.start(1)
    else:
        # A fence that Claude Code reads and this surface does not (a save refuses it):
        # the header the CLI reads is masked whole.
        cli = _CLI_FENCE_RE.match(core)
        header, start = (cli.group(1), cli.start(1)) if cli else ("", 0)
    masked = _mask_header_values(header, readable=match is not None)
    text = bom + core[:start] + masked + core[start + len(header) :]
    return cw.redact_secret_lines(text)


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

    The file is read **exactly once**, by :func:`_read_member_bytes`, which refuses (→ 422)
    a member that is not a regular file or cannot be read, without blocking on a FIFO. The
    returned (redacted) content and the hash
    are both derived from that single read (the hash over the RAW,
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
    _stored_skill_dir(skills_root, name)  # a file at skills/<name> is refused (→ 422)
    # A directory, a FIFO, a device or an unreadable member is a named 422, and a FIFO is
    # never opened in a blocking way (TASK-531).
    data = _read_member_bytes(target, relative)
    exists = data is not None
    raw = b"" if data is None else data
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise cw.InvalidCandidateError(f"{relative} is not valid UTF-8") from exc
    note = None
    # The normalized key, not the request string: `./SKILL.md` converts as SKILL.md does.
    if convert and exists and Path(relative).parts == (SKILL_FILENAME,):
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
    content = _member_view(text)
    return content, cw.hash_bytes(raw), exists, note


def _not_a_stored_file(relative: str) -> cw.InvalidCandidateError:
    """Return the refusal for a masked member whose stored entry is not a plain file."""
    return cw.InvalidCandidateError(
        f"{relative!r} holds the mask {cw.REDACTION_SENTINEL}, but its stored entry is a "
        "symlink, not a regular file, too large, or unreadable, so no stored value can be "
        "kept. Type the real values"
    )


def _read_stored_member(skills_root: Path, name: str, relative: str) -> str:
    """Return the stored text of one skill member, or ``""`` when nothing is stored there.

    Read outside the save's lock, so it must never block or follow a link: the entry is
    checked with ``lstat`` first (a FIFO, a socket, a device or a symlink is refused before
    any open), then opened with :data:`_OPEN_FLAGS` (``O_NOFOLLOW`` and ``O_NONBLOCK``, as
    the save's own stored reads), and the open descriptor must be that same regular file.
    The read is bounded by :data:`MAX_FILE_BYTES`. Anything else is refused (→ 422). Bytes
    that are not UTF-8 read as ``""``: no mask can bind to them, so the restore then
    refuses the mask.

    A directory on the way to the member must not be a symlink either: its parent must
    resolve to exactly the skill directory joined with the member's own parent parts, so a
    ``sub -> ../other-skill`` link inside the skill cannot lend another file's text. A swap
    of such a directory between this check and the open remains (closing it needs a
    descriptor walk); the save's own walk refuses any symlink in the skill under its lock.
    """
    try:
        fw.resolve_contained_path(skills_root, f"{name}/{relative}")  # containment only
    except fw.PathEscapeError as exc:
        raise cw.PathEscapeError(str(exc)) from exc
    skill_dir = (skills_root / name).resolve()
    parent = Path(relative).parent
    # normcase: on Windows `resolve()` returns the on-disk case, and a case variant of a
    # stored directory is the same directory there, not a link.
    real_parent = os.path.normcase((skill_dir / parent).resolve())
    if real_parent != os.path.normcase(skill_dir.joinpath(*parent.parts)):
        raise _not_a_stored_file(relative)  # a symlinked directory inside the skill
    # The UNRESOLVED path: resolving it would follow a symlink at the member itself.
    path = skills_root / name / relative
    try:
        seen = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return ""  # nothing is stored there, so no mask can be bound to it
    except OSError as exc:
        raise _not_a_stored_file(relative) from exc
    if not stat.S_ISREG(seen.st_mode) or seen.st_size > MAX_FILE_BYTES:
        raise _not_a_stored_file(relative)
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        raise _not_a_stored_file(relative) from exc
    try:
        fh = os.fdopen(fd, "rb")
    except BaseException:  # pragma: no cover - fdopen of a fresh descriptor does not fail
        os.close(fd)  # os.fdopen did not take the descriptor, so close it here
        raise
    with fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or not _same_object(info, seen):
            raise _not_a_stored_file(relative)  # swapped since the lstat
        data = fh.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:  # pragma: no cover - a file that grew since the lstat
        raise _not_a_stored_file(relative)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return ""


#: The frontmatter fields whose masked values go to a destination named on another line.
_DESTINATION_FIELDS = ("hooks", "mcpServers")


def _header_lines(text: str) -> range:
    """Return the 0-based lines of ``text`` that hold its frontmatter header (none if no fence).

    Counted as :func:`~clauster.config_write.restore_masked_lines` counts lines, and after a
    byte order mark, which Claude Code drops before it reads the fence.
    """
    core = text.removeprefix("\ufeff")
    match = _FRONTMATTER_RE.match(core)
    if match is None:
        return range(0)
    # Line 0 is the opening `---` fence; the header ends where group 1 ends.
    return range(1, len(core[: match.end(1)].splitlines()))


def _refuse_masks_with_changed_destinations(restored: str, stored: str, lines: set[int]) -> None:
    """Refuse (→ 422) a mask kept in the frontmatter when ``hooks`` or ``mcpServers`` changed.

    A hook or a server sends its masked header or URL token to the url beside it. A restored
    line is bound only by its own text, so an edit to that url on another line (or through
    an alias or a merge key) would otherwise send the stored credential somewhere new. So
    when any frontmatter line is restored, both fields must load exactly as stored. This is
    stricter than the Hooks tab, and it needs no knowledge of where a field's text sits.

    A restored header that does not parse has no destination to compare, and nothing can
    be written: :func:`_validate_member` runs this same parse on this same text next, so it
    refuses the save and names the real cause (TASK-530). A wholly masked header, which the
    editor shows for a stored header it cannot read as Claude Code does, comes back so.
    """
    if not lines.intersection(_header_lines(restored)):
        return
    try:
        now = parse_frontmatter(restored.removeprefix("\ufeff"))[0]
    except cw.InvalidCandidateError:
        return  # no destination at all: _validate_member refuses it with the real cause
    try:
        then = parse_frontmatter(stored.removeprefix("\ufeff"))[0]
        unchanged = all(cw.same_yaml_value(now.get(f), then.get(f)) for f in _DESTINATION_FIELDS)
    except cw.InvalidCandidateError:
        unchanged = False  # a header that cannot be compared is not unchanged
    if not unchanged:
        raise cw.InvalidCandidateError(
            f"{SKILL_FILENAME} frontmatter keeps a masked value {cw.REDACTION_SENTINEL}, but "
            "its 'hooks' or 'mcpServers' changed, so the stored value is not put back. "
            "Retype the masked values in the frontmatter"
        )


def _keep_masked_lines(
    skills_root: Path, name: str, files: dict[str, Any], *, skill_md: str
) -> dict[str, Any]:
    """Return ``files`` with each mask from the read view bound back to its stored line.

    The read path masks every member line by line (:func:`_member_view`), body and scripts
    included, so the write path restores line by line too
    (:func:`~clauster.config_write.restore_masked_lines`, given that same view), against the
    stored member at the same path. ``skill_md`` is the stored ``SKILL.md`` text, from the same
    read the stale-hash guard checked, so its masks bind to the bytes the operator loaded.
    A mask with no stored line to bind to is refused. A member without a mask is returned
    as sent.

    Memory stays bounded while it restores: two keys for one member (``d/x.sh`` and
    ``./d/x.sh``) are refused before any member read, so one stored file is restored at
    most once, and each restored member and the running total must stay under their caps
    (checked before the restored ``SKILL.md`` is parsed again). The refusals name the cap,
    never a size, so one refusal does not tell the length of the stored value a mask
    hides. Repeated saves near a cap can still find that length (a documented residual).
    """
    if not any(cw.REDACTION_SENTINEL in content for content in files.values()):
        return files
    # Before any member read: each alias key would build its own restored copy.
    named: set[tuple[str, ...]] = set()
    for relative in files:
        parts = Path(relative).parts
        if parts in named:
            # The same wording as `_build`'s refusal of a key that reaches a saved file.
            raise cw.InvalidCandidateError(
                f"{relative!r} names a file that is already in the save: another key "
                "names it too. Nothing was written. Send each file once, under its stored "
                "name"
            )
        named.add(parts)
    out = dict(files)
    total = sum(len(content.encode("utf-8")) for content in files.values())
    for relative, content in files.items():
        if cw.REDACTION_SENTINEL not in content:
            continue
        if relative == SKILL_FILENAME:
            stored = skill_md
        else:
            stored = _read_stored_member(skills_root, name, relative)
        # The read view, which depends on the text only, so a mask binds through the view
        # that showed it whatever the member is called.
        restored, lines = cw.restore_masked_lines(
            content, stored, what=relative, view=_member_view
        )
        size = len(restored.encode("utf-8"))
        cap = MAX_SKILL_MD_BYTES if relative == SKILL_FILENAME else MAX_FILE_BYTES
        total += size - len(content.encode("utf-8"))
        if size > cap or total > MAX_TOTAL_BYTES:
            raise cw.InvalidCandidateError(
                f"{relative!r} with its masked values kept would put the skill over its byte "
                f"cap ({cap} bytes for this file, {MAX_TOTAL_BYTES} for the skill directory)"
            )
        if relative == SKILL_FILENAME:  # after the cap: this parses both texts
            _refuse_masks_with_changed_destinations(restored, stored, lines)
        out[relative] = restored
    return out


#: A ``url:`` line whose whole value is the mask: :func:`mask_url` shows a credential URL
#: or a ``${...}`` reference so (TASK-525). Group 2 is an optional quote around the mask.
_WHOLE_URL_MASK_RE = re.compile(
    rf"^([ \t]*(?:-[ \t]+)*url[ \t]*:[ \t]+)([\"']?){re.escape(cw.REDACTION_SENTINEL)}\2"
    r"([ \t]*(?:#[^\r\n]*)?\r?)$",
    re.MULTILINE,
)


def _neutralized(sent: str) -> str:
    """Return ``sent`` with each mask replaced by a plain stand-in no check refuses by itself.

    A masked credential URL (``********@host``, the whole ``scheme://user@`` part masked)
    becomes ``http://x@host``, the same length, so a URL check still sees a URL and its
    columns stay exact. A ``url:`` value that is the mask alone becomes ``http://x``, also
    the same length. Every other mask becomes a distinct short token (``x0``, ``x1``,
    ...), so two masked keys do not collide as one repeated key. A token is never longer
    than the 8-character mask below ten million masks, so the text never grows.
    """
    text = _WHOLE_URL_MASK_RE.sub(r"\1\2http://x\2\3", sent)
    text = text.replace(f"{cw.REDACTION_SENTINEL}@", "http://x@")
    counter = itertools.count()
    return re.sub(re.escape(cw.REDACTION_SENTINEL), lambda _m: f"x{next(counter)}", text)


def _validate_member(relative: str, content: str, *, sent: str, stored: str | None) -> None:
    """Validate one member as it will be written, without leaking a kept value (→ 422).

    ``content`` may hold stored text put back behind a mask, and a check's message can
    name a size or a column, which tells the length of that hidden text. So for a member
    whose text the restore changed, a failure reports what the SENT text gives instead
    (the operator's own, masked text). If the sent text passes, the stored value is the
    cause, and the refusal says so without any number.

    The sent text is checked with its masks neutralized (:func:`_neutralized`): a
    frontmatter mask (``key: ********``) is itself not valid YAML (it reads as an alias),
    so checking it as sent would always blame the operator's unchanged text.

    One case comes first. When ``stored`` (the stored ``SKILL.md``; None for any other
    member) fails the frontmatter check (:func:`_check_skill_md`) with the same message,
    the stored file is the cause, and the refusal names it (TASK-530): a wholly masked
    header comes back so, and its neutralized mask lines would blame the operator's text
    for a header they never typed. That message comes from the stored text alone, and the
    skill list already shows it as the skill's ``frontmatter_error``, redacted the same
    way, so it tells nothing new. Any other check (the plugin marker, a cap) keeps the
    refusal without the check's message.
    """

    def _run(text: str) -> None:
        """Run the member's own check on ``text``."""
        if relative == SKILL_FILENAME:
            validate_skill_md_content(text)
        else:
            validate_script_body(text, relative)

    try:
        _run(content)
    except cw.InvalidCandidateError as restored_exc:
        if stored is not None:
            try:
                _check_skill_md(stored)  # exactly the check the skill list reports
            except cw.InvalidCandidateError as stored_exc:
                if str(stored_exc) == str(restored_exc):
                    cause = cw.redact_secret_lines(str(stored_exc)).rstrip(".")
                    raise cw.InvalidCandidateError(
                        f"{relative!r} keeps its masked values, and the stored file itself "
                        f"does not pass the checks: {cause}. Fix the stored file on the "
                        "host, or type the whole frontmatter again"
                    ) from restored_exc
        # Raises the operator's own error, if their text has one apart from the masks.
        _run(_neutralized(sent))
        raise cw.InvalidCandidateError(
            f"{relative!r} does not pass the checks once its masked values are kept: the "
            "stored text behind a mask is not valid here. Type the real values, or fix the "
            "stored file on the host"
        ) from restored_exc


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

    A named member replaces its stored copy, and on POSIX it keeps that copy's mode bits
    without setuid or setgid (TASK-500). Every stored member the request does NOT
    name is kept byte for byte, with its mode bits (TASK-496): a save never deletes a
    member as a side effect, and only :func:`delete_skill` removes files. A stored
    symlink or special file is refused, never followed (:func:`_keep_stored_members`), and
    the byte cap counts the kept members too.
    """
    if not is_valid_skill_name(name):
        raise cw.PathEscapeError(f"invalid skill name: {name!r}")
    if not isinstance(files, dict) or not files:
        raise cw.InvalidCandidateError("files must be a non-empty object")
    if SKILL_FILENAME not in files:
        raise cw.InvalidCandidateError(f"files must include {SKILL_FILENAME!r}")
    for relative in files:
        # Every check below picks the SKILL.md checks by the exact key; another spelling of
        # it would get the script checks and the script view instead (TASK-525).
        if relative != SKILL_FILENAME and _spells_skill_md(relative):
            raise cw.InvalidCandidateError(
                f"{relative!r} names {SKILL_FILENAME} under another spelling or case, which is "
                "the same file on macOS or Windows. Nothing was written. Send it once, as "
                f"{SKILL_FILENAME!r}"
            )

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

    # A member that still holds a mask is validated after its stored lines are put back
    # (below): a masked frontmatter line (`key: ********`) is not even valid YAML.
    for relative, content in files.items():
        if cw.REDACTION_SENTINEL in content:
            continue
        if relative == SKILL_FILENAME:
            validate_skill_md_content(content)
        else:
            validate_script_body(content, relative)

    skills_root = _skills_root(base)
    # Read once: the masks below bind to the same bytes this hash checks.
    current = _skill_md_bytes(skills_root, name)
    current_hash = cw.hash_bytes(current)
    existed = current_hash != cw.hash_bytes(b"")
    if expected_hash is None:
        if existed:
            raise cw.StaleConfigWriteError(f"skill {name!r} already exists; a hash is required")
    elif current_hash != expected_hash:
        raise cw.StaleConfigWriteError(f"skill {name!r} changed on disk since it was loaded")

    # TASK-427: the editor shows every member masked (`_member_view`), so a save can
    # send a mask back. Put the stored line back where the mask is bound to it, or refuse;
    # never write the literal `********` over the stored value. Then validate what will be
    # written: a kept line comes from the file on disk, not from the checked candidate.
    try:
        stored_md = current.decode("utf-8")
    except UnicodeDecodeError:
        stored_md = ""  # nothing readable is stored, so no mask can be bound to it
    # The cap above measured the masked text; a kept line can be longer than its mask, so
    # `_keep_masked_lines` checks the restored sizes against the caps as it goes.
    sent_files = files
    files = _keep_masked_lines(skills_root, name, files, skill_md=stored_md)
    for relative, content in files.items():
        if cw.REDACTION_SENTINEL in sent_files[relative]:  # the others were checked above
            _validate_member(
                relative,
                content,
                sent=sent_files[relative],
                stored=stored_md if relative == SKILL_FILENAME else None,
            )

    def _build(staging: Path) -> None:
        """Copy the stored members the request does not name, then write the named ones."""
        # replace_tree runs this INSIDE its lock on the skill dir, the same lock
        # delete_skill and every other write_skill take. So the re-check below, the
        # copy of the kept members, and the promote are one critical section: no other
        # clauster writer can change SKILL.md, add a member or delete one between the
        # check and the swap. The check outside the lock (above) only fails fast.
        if _skill_md_hash(skills_root, name) != current_hash:
            raise cw.StaleConfigWriteError(f"skill {name!r} changed on disk while it was saved")
        # Staging is owner-only (replace_tree), and every directory made below it stays
        # owner-only until _set_final_dir_modes, the last step before the promote.
        default_mode = _default_dir_mode(staging) if fw._is_posix() else 0
        # TASK-496: keep every stored member the request does not name.
        skill_dir = fw.resolve_contained_path(skills_root, name)
        sent = sum(len(content.encode("utf-8")) for content in files.values())
        try:
            kept_dirs, replaced_files = _keep_stored_members(
                skill_dir,
                staging,
                _replaced_by(skill_dir, files),
                name=name,
                sent=sent,
            )
        except OSError as exc:
            # An unreadable member is the operator's own state, not a server fault: a
            # refusal that names the cause, never a bare 500 and never a dropped file.
            raise cw.InvalidCandidateError(
                f"skill {name!r} could not be kept as stored: {exc.strerror or exc}. "
                "Nothing was written"
            ) from exc
        # Contained, direct writes -- NOT fw.write_file(), which additionally takes its
        # own cross-process flock per target. The staging dir is a fresh, private,
        # uncontested tree that no other writer can see until replace_tree promotes it
        # under a single cross-process lock, so taking a per-member lock here would be
        # pure redundant overhead (and would open/close a state-dir lock file per file
        # for nothing). replace_tree's own lock is the only contention point.
        created: list[Path] = []
        staging_root = staging.resolve()
        for relative, content in files.items():
            target = fw.resolve_contained_path(staging, relative)
            if target.is_dir():
                raise cw.InvalidCandidateError(f"{relative!r} is a directory in the skill")
            created += _make_private_dirs(staging_root, target.parent, relative)
            # Something here already is the kept copy of a stored file that the key
            # reaches under another spelling, by a rule the matcher could not prove (an
            # NTFS 8.3 name, a hard link, staging and skill dir with different case
            # rules). Removing it could delete another file's copy, so refuse instead.
            if os.path.lexists(target):
                raise cw.InvalidCandidateError(
                    f"{relative!r} names a file that is already in the save: another key, "
                    "or a stored file under another spelling. The save cannot tell which "
                    "file it replaces. Nothing was written. Send each file once, under its "
                    "stored name"
                )
            # Bytes, not text: text mode would translate "\n" to the platform newline
            # (CRLF on Windows), but the read path (read_skill_file / list_skills) is
            # byte-exact. A new file only: anything planted at the path since the check
            # above (a symlink included) makes the write fail closed, never follow.
            try:
                _create_exclusive(target, content.encode("utf-8"))
            except FileExistsError as exc:
                raise _changed(Path(relative)) from exc
            if fw._is_posix():  # pragma: skip-on-win  # pragma: no branch - Windows skips it
                _keep_replaced_mode(target, skill_dir / relative, replaced_files)
        _set_final_dir_modes(staging_root, kept_dirs, created, default_mode)

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
        # isinstance first: a list or object value is unhashable, so `in` raises TypeError,
        # which validate_candidate reports as a bare "unhashable type" 422. This check keeps
        # the message that names the key and the allowed values.
        if not isinstance(value, str) or value not in SKILL_OVERRIDE_VALUES:
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
