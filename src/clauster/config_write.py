"""Foundation plumbing for the code-executing config-write trust tier (#347/#687).

This is the **shared seam** the child surfaces (MCP servers #688, permission rules
#689, hooks #690, skills #691) build on; it ships **no concrete writer endpoint of
its own**. Everything here is fail-closed by construction:

* :func:`require_capability` — the route-level 404 gate. Capability off ⇒ 404; a
  user-scope request with ``allow_user_scope`` off ⇒ 404 (a disabled capability is
  *invisible*, exactly like the reaper UI). Use 404, never 403. ``local`` scope is
  gated identically to ``project`` scope (no extra opt-in): like project scope it is
  confined to a single project directory, so it does not carry the account-wide blast
  radius that justifies the separate ``allow_user_scope`` gate (#766 scope decision).
* :func:`expected_confirm_token` / :func:`require_confirm` — the per-write
  type-the-name confirm. The server re-derives the expected token (the literal
  resolved project name for project scope, the same name suffixed with
  :data:`LOCAL_SCOPE_SUFFIX` for local scope, or the literal ``USER SCOPE`` for user
  scope) and rejects a mismatch with 400 **before any validation or I/O**, so a CSRF /
  accidental-click write is structurally impossible. Local scope gets its own,
  distinct token so a confirm typed for a project-scope write can never be replayed
  to confirm a local-scope write on the same project (or vice versa).
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

import contextvars
import datetime
import hashlib
import json
import math
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import HTTPException

from .claude_json import locked_replace_json_file, update_claude_json
from .config import ClausterConfig
from .discovery import is_valid_project_name

Scope = Literal["project", "user", "local"]

#: The literal token the UI shows (and the caller must retype) for a user-scope write.
#: Not a credential — it is a fixed, public type-the-name confirm string the user reads
#: off the screen and retypes, so the hardcoded-password lint is a false positive here.
USER_SCOPE_TOKEN = "USER SCOPE"  # noqa: S105 - public confirm literal, not a secret

#: Suffix appended to the project name to build the **local**-scope confirm token
#: (e.g. ``"myproject (local)"``). Deliberately distinct from the plain project name
#: (the project-scope token) so a confirm typed for one scope can never be replayed to
#: confirm a write in the other scope on the same project.
LOCAL_SCOPE_SUFFIX = " (local)"

#: The masked sentinel emitted in place of any secret-shaped value. A write that sends
#: it back is treated as "keep the stored value" (a no-op merge for that key).
REDACTION_SENTINEL = "********"

# Secret-shaped value detection (structural redaction). A value is masked when EITHER
# its KEY looks credential-shaped (the vocabulary below) OR its VALUE looks like
# credential material: a ``${...}`` interpolation or a token-bearing URL scheme (e.g.
# ``slack://TOKEN@host``). There is **no value-entropy check** — a real secret stored
# under a benign key (``{"DEPLOY_KEY": "AKIA…"}``) is NOT detected here, so redaction
# alone must never be relied on to decide whether a value is safe to place in argv (see
# ``config_write_mcp_cli.entry_needs_direct_write``, which errs on any env/headers value
# instead). This is deliberately conservative — over-masking a non-secret only forces the
# caller to resend it; under-masking would leak. Keys are matched case-insensitively.
# The ``auth`` alternative excludes ONLY the benign ``author``/``authors`` field via a
# negative lookahead (``(?!ors?\b)``) rather than an enumerated suffix list: every other
# credential-shaped ``auth`` key still masks — ``auth``, ``authn``, ``authorization``,
# ``auth_key``/``authHeader``/``auth_cookie`` (``_``/camelCase joins), ``AUTH_TOKEN`` — while a
# bare ``auth`` substring no longer over-matches ``author`` and redacts a real name/email
# (#958/DF-4). An enumerated ``auth(?:...)?\b`` list looked tighter but silently under-masked
# every compound ``auth`` key (``\b`` does not fire before ``_`` or a camelCase boundary).
_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[-_]?key|apikey|"
    r"auth(?!ors?\b)|credential|bearer)",
    re.IGNORECASE,
)


def _interp_spans(value: str) -> Iterator[tuple[int, int]]:
    r"""Yield the ``(start, end)`` of each ``${…}`` interpolation with a non-empty body.

    The linear replacement for a ``\\$\\{[^}]+\\}`` regex, which is **quadratic** on hostile
    input (CodeQL ``py/polynomial-redos``). ``[^}]+`` stops only at ``}`` or end-of-string,
    so on a value made of many ``${`` and no ``}`` the engine consumes to the end at EVERY
    starting position: measured 0.03s at 16 KB rising to 2.1s at 128 KB, i.e. ~2 minutes at
    1 MB. Values reach here unbounded from a project's ``.claude/settings.json``, which
    arrives with a cloned repository, so the length is not ours to trust.

    A possessive ``[^}]++`` is NOT the fix — it removes the backtracking but the scan still
    restarts at every ``${``, so it stays quadratic (measured: 0.82s at 128 KB, only a ~2.5x
    smaller constant). Narrowing the class to ``[^{}]`` or ``[^}$]`` would make it linear,
    but both stop matching bodies the old pattern matched (``${a{b}``, ``${A$B}``) — and
    under-masking is the one direction a redaction helper must never fail in.

    So the scan is done by hand instead, matching the regex exactly: each match runs from a
    ``${`` to the FIRST following ``}``, and requires at least one character between them
    (``${}`` is not a match, which is what ``[^}]+`` means). Each index only moves forward,
    so the whole walk is O(n).
    """
    i, n = 0, len(value)
    while i < n:
        start = value.find("${", i)
        if start == -1:
            return
        close = value.find("}", start + 2)
        if close == -1:
            return
        if close > start + 2:
            yield start, close + 1
            i = close + 1
        else:
            # `${}` — no body, so the regex would not match here either. Resume after the
            # opener: a match cannot start at the `{` or the `}` in between.
            i = start + 2


def _has_interp(value: str) -> bool:
    """Whether ``value`` contains a ``${…}`` interpolation (see :func:`_interp_spans`)."""
    return next(_interp_spans(value), None) is not None


def _mask_interps(body: str, sentinel: str) -> str:
    """Replace every ``${…}`` in ``body`` with ``sentinel`` (see :func:`_interp_spans`)."""
    out: list[str] = []
    prev = 0
    for start, end in _interp_spans(body):
        out.append(body[prev:start])
        out.append(sentinel)
        prev = end
    if not out:
        return body
    out.append(body[prev:])
    return "".join(out)


_SECRETISH_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://[^/@\s]+@", re.IGNORECASE)
# A `key: value` / `key = value` line (the shape of a settings.json-adjacent config
# line, a frontmatter field, or a shell-style env assignment) — used to mask the value
# side of a secret-shaped key in free text that has no JSON/dict structure to recurse.
# Greedy `.*` with the trailing whitespace split off in Python, NOT the natural-looking
# `(?P<value>\S.*?)(?P<trail>\s*)$`. That form is quadratic: the lazy `.*?` grows one
# character at a time while the greedy `\s*` re-scans the run behind it, so a line like
# `token: a` + 32 KB of spaces + `b` took 2.1s, and 64 KB took 8.6s (x4 per doubling).
# `redact_secret_lines` strips the line ending before matching, so the body holds no
# newline and greedy `.*` + `$` is unambiguous — one pass, no backtracking.
_KV_LINE_RE = re.compile(r"^(?P<prefix>\s*[\w.\-]+\s*[:=]\s*)(?P<rest>\S.*)$")


def _split_kv_line(body: str) -> tuple[str, str] | None:
    r"""Return ``(prefix, trail)`` for a ``key: value`` line, or ``None``.

    The linear replacement for the old three-group regex (see :data:`_KV_LINE_RE`).
    ``trail`` is the run of trailing whitespace the old ``(?P<trail>\\s*)$`` captured,
    recovered here with ``rstrip`` so the caller can rebuild the line byte-for-byte.
    """
    kv = _KV_LINE_RE.match(body)
    if kv is None:
        return None
    rest = kv.group("rest")
    value = rest.rstrip()
    return kv.group("prefix"), rest[len(value) :]


# Character classification for the URL scheme, delegated to the regex ENGINE rather than
# reimplemented, because `[a-z]` under `re.IGNORECASE` is Unicode-aware and matches more
# than ASCII: U+212A KELVIN SIGN case-folds to `k`, U+017F LONG S to `s`, and U+0130/U+0131
# likewise fold into the range. An ASCII-only classification silently stops matching
# `\u212a://user@host` — which the old regex DID mask — leaking the userinfo. That is the
# under-masking direction, and it is exactly the failure the docstring below warns about;
# an earlier revision of this fix shipped it. The ASCII fast path keeps the common case a
# set lookup and only pays for a regex call on a non-ASCII character.
_SCHEME_START_RE = re.compile(r"[a-z]", re.IGNORECASE)
_SCHEME_CHAR_RE = re.compile(r"[a-z0-9+.\-]", re.IGNORECASE)
_SCHEME_ASCII = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")
_SCHEME_START_ASCII = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _is_scheme_char(ch: str) -> bool:
    r"""Whether ``ch`` matches ``[a-z0-9+.\\-]`` under ``re.IGNORECASE`` (Unicode-aware)."""
    if ch.isascii():
        return ch in _SCHEME_ASCII
    return _SCHEME_CHAR_RE.match(ch) is not None


def _is_scheme_start(ch: str) -> bool:
    """Whether ``ch`` matches ``[a-z]`` under ``re.IGNORECASE`` (Unicode-aware)."""
    if ch.isascii():
        return ch in _SCHEME_START_ASCII
    return _SCHEME_START_RE.match(ch) is not None


def _url_cred_spans(text: str) -> Iterator[tuple[int, int]]:
    r"""Yield ``(start, end)`` of each ``scheme://user@`` credential-bearing URL prefix.

    The linear replacement for ``[a-z][a-z0-9+.\\-]*://[^/@\\s]+@`` used **non-anchored**
    over a whole line. That pattern is quadratic for the same reason the ``${…}`` one was:
    ``[a-z0-9+.\\-]*`` consumes to the end of a run and then backtracks hunting ``://`` at
    EVERY starting position, so an ordinary long alphanumeric run — a minified blob, a long
    base64 value, a one-line JSON file — costs 0.18s at 8 KB and 11s at 64 KB. Unlike the
    ``${`` flood it needs no crafted payload at all.

    A ``(?<![a-z0-9+.\\-])`` lookbehind is the obvious fix and is WRONG: it under-masks.
    ``-https://u@h`` stops matching entirely, because the ``-`` blocks the lookbehind and
    no match may begin at the ``-`` itself. Under-masking is the direction that leaks.

    So the scan anchors on the ``://`` literal and expands outward, which reproduces the
    regex exactly: the scheme is the maximal ``[A-Za-z0-9+.-]`` run ending at ``://``,
    starting from the first ASCII letter in it (the pattern's first atom is ``[a-z]``);
    the userinfo is the run after ``://`` up to the first ``/``, ``@`` or whitespace, which
    must be a non-empty run terminated by ``@`` (``@`` is outside ``[^/@\\s]``, so greedy
    matching cannot cross it and backtracking cannot rescue a different terminator).
    Matches are non-overlapping and left-to-right, exactly as ``re.sub`` applies them.
    """
    i = 0
    while True:
        sep = text.find("://", i)
        if sep == -1:
            return
        run = sep
        while run > i and _is_scheme_char(text[run - 1]):
            run -= 1
        start = -1
        for j in range(run, sep):
            if _is_scheme_start(text[j]):
                start = j
                break
        if start == -1:
            i = sep + 3  # no scheme letter before `://` — no match can start here
            continue
        end = sep + 3
        while end < len(text) and text[end] not in "/@" and not text[end].isspace():
            end += 1
        if end > sep + 3 and end < len(text) and text[end] == "@":
            yield start, end + 1
            i = end + 1
        else:
            i = sep + 3


def _mask_url_creds(body: str, replacement: str) -> str:
    """Replace each ``scheme://user@`` with ``replacement`` (see :func:`_url_cred_spans`)."""
    out: list[str] = []
    prev = 0
    for start, end in _url_cred_spans(body):
        out.append(body[prev:start])
        out.append(replacement)
        prev = end
    if not out:
        return body
    out.append(body[prev:])
    return "".join(out)


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
    # local scope carries no additional opt-in: like project scope it is confined to a
    # single project directory, so the base `enabled` flag alone gates it.


def expected_confirm_token(scope: Scope, project_name: str | None) -> str:
    """Re-derive the confirmation token the caller must retype for this write.

    Project-scope ⇒ the literal resolved project name; local-scope ⇒ that same name
    suffixed with :data:`LOCAL_SCOPE_SUFFIX` (a distinct third token); user-scope ⇒ the
    literal :data:`USER_SCOPE_TOKEN`. The server *always* derives this itself and never
    trusts a client-supplied "expected" value — that is what makes the confirm a real
    CSRF / accidental-click guard.

    Raises ``HTTPException(400)`` for a project/local-scope write with no resolved project
    name: it can then have no valid token, so it fails closed rather than matching an
    empty string.
    """
    if scope == "user":
        return USER_SCOPE_TOKEN
    if not project_name:
        # A project/local-scope write with no resolved project can have no valid token,
        # so it can never be confirmed — fail closed rather than accept an empty match.
        raise HTTPException(status_code=400, detail=f"{scope}-scope write requires a project")
    if scope == "local":
        return f"{project_name}{LOCAL_SCOPE_SUFFIX}"
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

    **Footgun for child writers:** ``expected_hash=None`` SKIPS the check entirely (returns
    the bytes unguarded). That is the deliberate "no prior hash" path, but a child that
    conditionally computes the hash and accidentally passes ``None`` gets an unguarded write
    with no signal. Pass a real hash whenever the caller loaded the file first.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    if expected_hash is not None and hash_bytes(data) != expected_hash:
        raise StaleConfigWriteError("config file changed on disk since it was loaded")
    return data


#: Every container ``yaml.safe_load`` can build, and every container ``jsonable_encoder``
#: expands: ``!!omap``/``!!pairs`` construct a list of **tuples** and ``!!set`` a **set**,
#: which the encoder emits as a JSON array, one entry per member.
#:
#: ONE enumeration, deliberately, because both walks over a parsed header must agree on what
#: a container is and they disagreed for different reasons. :func:`_expands_beyond` has to
#: count exactly what the serializer emits, or a ``!!set`` leaf counted as a single scalar
#: walks past the size cap. :func:`redact_secrets` has to *descend* exactly what the
#: serializer emits, or a secret inside a tuple is returned by identity and reaches the
#: browser unmasked (#1393) — which it did: ``auth: !!omap [{token: sk-live-…}]`` was served
#: verbatim by ``config_write_subagents._read_agent``.
#:
#: :func:`_is_self_referential` keeps its own NARROWER set and that is correct, not drift: it
#: hunts cycles, and a ``set``/``frozenset`` holds only hashables, so a cycle cannot route
#: through one. Its docstring says so.
_YAML_CONTAINERS = dict | list | tuple | set | frozenset


def _is_secretish(key: str, value: Any) -> bool:
    """Whether ``(key, value)`` should be masked (structural secret detection)."""
    if not isinstance(value, str | bytes) or not value:
        return False
    if _SECRET_KEY_RE.search(key):
        return True
    if isinstance(value, bytes):
        # A `!!binary` value parses to `bytes`, and the rules below are regexes over text.
        # `jsonable_encoder` decodes the bytes back to text for the display, so scan the decoded
        # text too -- otherwise a credential URL or `${interp}` spelled as base64 leaks past a
        # benign key exactly as a plain-string secret used to leak past the whole predicate
        # (#1450, same spelling-independence the #1393 omap fix restored). `redact_secrets`
        # still returns the original `bytes` when this is False, so a benign value decodes
        # unchanged. `load_frontmatter_yaml` rejects non-UTF-8 binary, so `replace` only guards
        # a caller that hands raw bytes; a value masked either way needs no round-trip fidelity.
        value = value.decode("utf-8", "replace")
    if _has_interp(value):
        return True
    if _SECRETISH_URL_RE.match(value):
        return True
    return False


def redact_secrets(
    data: Any, _key: str = "", _memo: dict[tuple[int, bool], Any] | None = None
) -> Any:
    """Return a deep copy of ``data`` with secret-shaped values masked from the start.

    Redaction is **structural, not a post-filter**: a secret-shaped leaf is emitted as
    :data:`REDACTION_SENTINEL` while building the display value, so the live secret is
    never assembled into the response in the first place. Recurses every container in
    :data:`_YAML_CONTAINERS`; non-secret scalars pass through unchanged. Mirrors
    ``config_editor.editable_values`` (read only what's safe to surface).

    A ``tuple``/``set``/``frozenset`` is emitted as a **list**, because that is what
    ``jsonable_encoder`` was already going to do with it and this field is display-only (a PUT
    round-trips ``content``, never ``frontmatter``, so nothing here is written back). Walking
    them is not cosmetic: ``dict | list`` alone returned an ``!!omap`` tuple by IDENTITY, so
    ``auth: !!omap [{token: sk-live-…}]`` reached the browser unmasked past all three
    detection rules — key-name, ``${interp}`` and credential-URL alike — and the "deep copy"
    promise in this docstring's first line did not hold for a mutable value under a tuple.
    A ``set``'s iteration order is not defined, so the emitted list's order is not either;
    that is not new (``jsonable_encoder`` already iterated the raw set) and it carries no
    information, since redaction maps every distinct secret in it to one sentinel.

    Hint scope: a secret-shaped key marks its **whole subtree** secret. The hint reaches its
    own scalar leaf, the elements of a list value, and every value nested beneath it at any
    depth — so ``{"auth": {"value": "sk-live-…"}}`` is masked. Masking is deliberately wider
    than the key that triggered it: over-masking costs a resend, under-masking leaks.

    A key is a key whichever way the document spells it. An ``!!omap``/``!!pairs`` entry is a
    two-element tuple whose first element IS the mapping key, so it gives the hint for the
    second exactly as a dict key does. Without that, redaction was spelling-dependent on
    identical data — ``auth: {token: sk-live-…}`` masked and
    ``mcpServers: !!omap [{token: sk-live-…}]`` did not, because only the benign outer key
    was ever consulted. ``safe_load`` builds a 2-tuple from those two tags and nothing else,
    so treating one as a pair cannot mis-read a plain sequence.

    Memoized per container, because a YAML alias makes the parsed structure a DAG rather than
    a tree and an unmemoized walk re-expands every distinct *path* to a node. A non-recursive
    ``&a``/``*a`` pyramid — legal, acyclic YAML that :func:`_is_self_referential` neither does
    nor should reject — makes that exponential in the header's BYTE length: a 346-byte header
    ``safe_load`` parses in 0.8 ms took 0.72 s to redact, and a 472-byte one did not finish,
    with ``config_write_subagents._read_agent`` holding an ``asyncio.to_thread`` worker for
    the whole walk (#1393). The memo takes both under 0.1 ms. Same fix, for the same reason,
    as the ``done`` set in :func:`_is_self_referential`.

    The memo key's second half is a BOOLEAN, not the hint string, and both halves are
    load-bearing. The hint reaches behaviour only through ``_SECRET_KEY_RE.search`` (in
    :func:`_is_secretish`, and where a child key decides whether to replace it), so two
    secret-shaped hints are interchangeable and so are two benign ones — but a secret one and
    a benign one are not. Keying on ``id`` alone would let one aliased object reached under
    both ``token:`` and ``name:`` reuse the unmasked result and **under-mask**. Keying on the
    hint STRING would be correct but unbounded: a header carrying many distinct secret-shaped
    keys would multiply the memo, where the predicate keeps it O(nodes).

    Two consequences worth stating. An aliased input yields ONE shared output object per
    ``(node, hint-is-secret)`` pair, so the result is a deep copy but not a fully expanded
    tree — callers replace whole keys on it (``config_write_settings._redact_misc``,
    ``config_write_subagents._read_agent``) and must not mutate a nested value in place. And
    an entry is recorded only AFTER its children are done, so a self-referential structure
    still recurses to ``RecursionError`` exactly as before rather than yielding a
    self-referential result: :func:`load_frontmatter_yaml` refuses those, and the read path
    degrades on the error. ``id`` reuse is not a hazard — every memoized node stays reachable
    from ``data`` for the whole walk.
    """
    memo = {} if _memo is None else _memo
    if isinstance(data, _YAML_CONTAINERS):
        memo_key = (id(data), bool(_SECRET_KEY_RE.search(_key)))
        cached = memo.get(memo_key)
        if cached is not None:
            return cached
        out: Any
        if isinstance(data, dict):
            out = {}
            for k, v in data.items():
                # Inherit the ancestor's hint unless this key introduces its own. Re-deriving
                # the hint purely from the child's keys is what let a nested secret through: a
                # dict value never satisfies _is_secretish (it only matches non-empty str), so
                # the parent's "this subtree is secret" signal was dropped at every dict
                # boundary. {"auth": {"value": "sk-live-…"}} is not exotic — it is how a lot
                # of MCP server configs and settings blobs are written.
                hint = str(k) if _SECRET_KEY_RE.search(str(k)) else _key
                out[k] = (
                    REDACTION_SENTINEL if _is_secretish(hint, v) else redact_secrets(v, hint, memo)
                )
        elif isinstance(data, tuple) and len(data) == 2:
            # An `!!omap`/`!!pairs` ENTRY. ``safe_load`` builds a 2-tuple from those two tags
            # and from nothing else, and both mean (key, value) — so element 0 is a KEY and
            # must act as the secret hint for element 1, exactly as a dict key does. Without
            # this, redaction was spelling-dependent on the same data: `auth: {token: …}`
            # masked while `mcpServers: !!omap [{token: …}]` did not, because only the benign
            # OUTER key was ever a hint. Element 0 itself keeps the AMBIENT hint rather than
            # its own, so it is never masked by its own name — matching the dict branch, which
            # never masks a key by itself. Element 0 DOES mask under a secret-shaped ancestor,
            # where a dict key would not: over-masking, which is the safe direction.
            hint = str(data[0]) if _SECRET_KEY_RE.search(str(data[0])) else _key
            out = [redact_secrets(data[0], _key, memo), redact_secrets(data[1], hint, memo)]
        else:
            # list, tuple, set and frozenset all iterate their members, and all four are
            # emitted as a list — see the docstring for why a tuple must not pass by identity.
            out = [redact_secrets(v, _key, memo) for v in data]
        memo[memo_key] = out
        return out
    if _is_secretish(_key, data):
        return REDACTION_SENTINEL
    return data


def redact_secret_lines(text: str) -> str:
    """Return ``text`` with secret-shaped content masked, line by line.

    :func:`redact_secrets` is *structural* — it recurses a parsed dict/list, using each
    key as the secret-hint. Free-form text (a skill script, a subagent's frontmatter, a
    ``CLAUDE.md``) has no such structure to recurse, so this is the line-oriented twin
    the file/dir writer primitive (:mod:`clauster.config_file_writer`) uses on its read
    path: each line is scanned independently and never assembled into anything else.

    Per line:

    * Any ``${...}`` interpolation is masked in place (same shape as the structural
      check).
    * Any credential-bearing URL (``scheme://user@host``) occurring anywhere in the
      line is masked in place.
    * A ``key: value`` / ``key = value`` line whose key looks secret-shaped (the same
      :data:`_SECRET_KEY_RE` vocabulary) has its value replaced with the sentinel.

    Deliberately conservative in the same direction as :func:`redact_secrets`:
    over-masking a non-secret line only costs a resend; under-masking would leak.
    Preserves the original line endings and count exactly (a caller diffing line
    numbers against the source never sees a shift).
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        body, eol = line, ""
        if body.endswith("\r\n"):
            body, eol = body[:-2], "\r\n"
        elif body.endswith("\n"):
            body, eol = body[:-1], "\n"
        masked = _mask_interps(body, REDACTION_SENTINEL)
        masked = _mask_url_creds(masked, f"{REDACTION_SENTINEL}@")
        kv = _split_kv_line(masked)
        if kv and _SECRET_KEY_RE.search(kv[0]):
            masked = f"{kv[0]}{REDACTION_SENTINEL}{kv[1]}"
        out.append(masked + eol)
    return "".join(out)


#: Request-scoped sink for the redacted argv of the ``claude …`` commands a config-write
#: spawns (#958 Part 6). A route sets this to a fresh list around the write; each CLI
#: ``_run`` appends its ``[verb, *redacted-args]`` via :func:`record_cli_argv`, so the audit
#: line can record exactly what ran without threading an accumulator through every writer.
#: Default ``None`` = "not capturing" (the CLI runs normally, nothing is recorded).
cli_argv_sink: contextvars.ContextVar[list[list[str]] | None] = contextvars.ContextVar(
    "cli_argv_sink", default=None
)


def _mask_json_values(obj: Any) -> Any:
    """Return ``obj`` with every leaf value replaced by the sentinel (keys/structure kept).

    Applied to a serialized config entry in argv so the audit records the entry's *shape*
    (its keys), never a value — because a value can be a secret under a benign key (an API
    key inside an ``args`` list) that neither key-name (:func:`redact_secrets`) nor
    line-based (:func:`redact_secret_lines`) heuristics can catch.
    """
    if isinstance(obj, dict):
        return {k: _mask_json_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_json_values(v) for v in obj]
    return REDACTION_SENTINEL


def _redact_argv_arg(arg: str) -> str:
    """Redact one argv element for the audit trail.

    A JSON object/array arg (e.g. ``claude mcp add-json``'s serialized entry) has ALL its
    values masked via :func:`_mask_json_values` — structure + keys kept, values dropped —
    since it can carry a secret no heuristic detects. Every other arg is scanned by
    :func:`redact_secret_lines` (masks ``${…}`` / ``scheme://user@host`` / secret-keyed KV).
    """
    if arg.lstrip()[:1] in ("{", "["):
        try:
            return json.dumps(_mask_json_values(json.loads(arg)))
        except (ValueError, TypeError):
            pass
    return redact_secret_lines(arg)


def record_cli_argv(verb: str, args: list[str]) -> None:
    """Append ``[verb, *args]`` (each arg redacted) to the active :data:`cli_argv_sink`, if any.

    A no-op when no route is capturing (``cli_argv_sink`` is ``None``). Each arg is redacted
    by :func:`_redact_argv_arg` — a JSON entry has all its *values* masked (keys kept), other
    args scanned line-wise — so the audit records what ran without ever persisting a value.
    The CLI paths already keep secrets off argv by construction; this is the belt-and-braces
    that also closes the benign-keyed-secret-inside-``args`` gap. The binary path itself is
    intentionally not recorded (host-specific noise). Propagates across ``asyncio.to_thread``
    because the sink is a mutable list the worker thread shares with the setting context.
    """
    sink = cli_argv_sink.get()
    if sink is not None:
        sink.append([verb, *(_redact_argv_arg(a) for a in args)])


def merge_redacted(incoming: Any, stored: Any) -> Any:
    """Merge a write-side ``incoming`` value over ``stored``, honoring keep-stored.

    Implements the "unchanged-masked ⇒ keep-stored" rule: any leaf in ``incoming``
    equal to :data:`REDACTION_SENTINEL` is dropped and the ``stored`` value is kept —
    the browser can never read a secret out, and a write that doesn't touch it need not
    resend it. Dicts merge key-by-key (recursing); a sentinel for a key the store
    doesn't have is dropped entirely (there is nothing to keep). Non-dict / non-sentinel
    values from ``incoming`` win. Dicts and lists both recurse (``redact_secrets`` masks
    inside lists too — e.g. a token in an MCP ``args`` list — so the merge must restore
    from them symmetrically, or the literal ``"********"`` would be written verbatim).
    When ``incoming`` is a dict/list but ``stored`` is not (e.g. ``None`` for an absent
    subtree — exactly what ``write_subtree`` passes), ``stored`` is treated as empty so a
    sentinel for a never-stored slot is dropped rather than written as the literal sentinel.
    """
    if incoming == REDACTION_SENTINEL:
        return stored  # keep-stored (may be a missing-key sentinel handled by caller)
    if isinstance(incoming, dict):
        stored_dict = stored if isinstance(stored, dict) else {}
        out: dict[Any, Any] = {}
        for k, v in incoming.items():
            if v == REDACTION_SENTINEL:
                if k in stored_dict:
                    out[k] = stored_dict[k]  # keep the stored secret
                # else: sentinel for an absent key ⇒ nothing to keep, drop it
            else:
                out[k] = merge_redacted(v, stored_dict.get(k))
        return out
    if isinstance(incoming, list):
        stored_list = stored if isinstance(stored, list) else []
        out_list: list[Any] = []
        for i, v in enumerate(incoming):
            if v == REDACTION_SENTINEL:
                if i < len(stored_list):
                    out_list.append(stored_list[i])  # keep the stored secret at this index
                # else: sentinel past the stored list ⇒ nothing to keep, drop it
            else:
                out_list.append(
                    merge_redacted(v, stored_list[i] if i < len(stored_list) else None)
                )
        return out_list
    return incoming


def write_subtree(claude_json: Path, subtree_key: str, mutate: Callable[[Any], Any]) -> None:
    """Locked read → set **only** ``subtree_key`` → atomic replace of ``claude_json``.

    The user-scope writer primitive. ``mutate`` receives the *current* value of
    ``data[subtree_key]`` (or ``None`` when absent) and returns the new subtree value;
    every other top-level key is preserved verbatim by the atomic replace — never a
    whole-file browser blob over the top, which would wipe the operator's trust grants
    and tokens. Runs through the shared
    :func:`~clauster.claude_json.update_claude_json` transaction (``flock`` + one-time
    ``.bak`` + mode-preserving atomic replace) — the same machinery ``trust`` uses.
    """

    def _apply(data: dict) -> None:
        """Replace the top-level subtree with whatever ``mutate`` returns for it."""
        data[subtree_key] = mutate(data.get(subtree_key))

    update_claude_json(claude_json, _apply)


def read_nested_subtree(
    claude_json: Path, outer_key: str, inner_key: str, subtree_key: str
) -> Any:
    """Return ``data[outer_key][inner_key][subtree_key]`` from ``claude_json``, or ``None``.

    The read-side twin of :func:`write_nested_subtree` — the per-project local-scope
    shape Claude Code's own ``~/.claude.json`` uses (``projects[<abs-project-path>]
    .mcpServers`` for local-scope MCP servers, mirroring the existing trust flags at
    ``projects[<abs-project-path>].hasTrustDialogAccepted``, see :mod:`clauster.trust`).
    Any missing level (file, ``outer_key``, ``inner_key``, or a non-dict at any level)
    reads as ``None`` — a missing local config is simply empty. A file that exists but is
    unparseable (non-UTF-8, malformed JSON, or not a JSON object) raises
    :class:`InvalidCandidateError` (→ 422): we never treat an unreadable config as empty.
    """
    try:
        raw = claude_json.read_bytes()
    except FileNotFoundError:
        raw = b""
    data = load_settings_json_obj(raw)
    outer = data.get(outer_key)
    if not isinstance(outer, dict):
        return None
    inner = outer.get(inner_key)
    if not isinstance(inner, dict):
        return None
    return inner.get(subtree_key)


def write_nested_subtree(
    claude_json: Path,
    outer_key: str,
    inner_key: str,
    subtree_key: str,
    mutate: Callable[[Any], Any],
) -> None:
    """Locked read → set **only** ``data[outer_key][inner_key][subtree_key]`` → atomic replace.

    The per-project nested twin of :func:`write_subtree`. Claude Code's own
    ``~/.claude.json`` keys per-project state under ``projects[<abs-project-path>]``
    (trust flags today; local-scope MCP servers here) — this writes exactly one leaf of
    that nested shape. ``mutate`` receives the *current* value of the inner subtree (or
    ``None`` when absent) and returns the replacement; every sibling — every other
    project's entry, every other subtree of *this* project's entry, every other
    top-level key — is preserved verbatim by the atomic replace. Runs through the same
    :func:`~clauster.claude_json.update_claude_json` transaction (``flock`` + one-time
    ``.bak`` + mode-preserving atomic replace) the flat :func:`write_subtree` and
    :mod:`clauster.trust` use.
    """

    def _apply(data: dict) -> None:
        """Create the outer/inner path as needed, then replace the nested subtree."""
        outer = data.get(outer_key)
        if not isinstance(outer, dict):
            outer = {}
            data[outer_key] = outer
        inner = outer.get(inner_key)
        if not isinstance(inner, dict):
            inner = {}
        inner[subtree_key] = mutate(inner.get(subtree_key))
        outer[inner_key] = inner

    update_claude_json(claude_json, _apply)


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


# ---------------------------------------------------------------------------
# Shared helpers used by child surfaces (MCP #688, permissions #689, hooks #690)
# ---------------------------------------------------------------------------


#: The ONE ``---``-delimited frontmatter fence for the whole write tier, shared by
#: :mod:`clauster.config_write_subagents` and :mod:`clauster.config_write_skills`.
#: Both surfaces are code-executing (a header becomes a subagent's ``tools`` or a
#: skill's ``allowed-tools``), so a file must not parse in one and be rejected — or
#: split differently — in the other. It lived as two near-identical copies until they
#: drifted on trailing spaces/tabs after a ``---``, in both directions (#1352): on the
#: OPENING fence the skill copy rejected what the subagent copy accepted, so one file
#: was a 422 on one surface and a write on the other; on the CLOSING fence both
#: matched, but the skill copy handed the whitespace back as the head of the body, so
#: ``---\na: 1\n--- \nbody`` split as ``'body'`` here and ``' \nbody'`` there. One
#: object, aliased by both modules, makes that class of drift structurally impossible
#: rather than merely tested for. The tolerant direction is deliberate: an editor that
#: strips nothing on save is the common case, and rejecting such a file helps no one.
#:
#: Mechanics: DOTALL so ``.`` spans newlines inside the captured YAML; non-greedy so the
#: FIRST closing ``---`` ends the block (a body that itself contains a ``---`` line is
#: not swallowed into the header).
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


def _is_self_referential(value: Any) -> bool:
    """Whether ``value`` contains a container that is reachable from itself.

    ``yaml.safe_load`` builds a *recursive* structure without complaint for a
    self-referential alias (``extra: &a [*a]`` — a list whose only element is itself). It is
    well-formed YAML and never raises, so the parse seam accepted it and the crash landed on
    an unguarded consumer instead: :func:`redact_secrets` walked it forever and a GET raised
    ``RecursionError`` — a 500 on the code-executing tier whose parse errors are contracted
    to 422 (#1368).

    Cycle detection, NOT alias rejection. A non-recursive alias is ordinary, legitimate YAML
    (``a: &x [1,2]`` / ``b: *x`` yields the same object under two keys) and every consumer
    handles it fine, so refusing aliases outright would reject valid frontmatter — the repo's
    own fuzz corpus carries a merge-key seed that uses one. Only a container reachable from
    *itself* is refused, which is exactly the shape no consumer can finish walking.

    ``tuple`` is in the container set alongside ``dict``/``list`` because ``safe_load`` builds
    one: ``!!omap`` and ``!!pairs`` construct a list of **tuples**, so ``extra: &a !!omap [{k:
    *a}]`` routes its cycle through a tuple. Skipping tuples as scalars left that shape
    accepted, and it does not even reach ``redact_secrets`` — ``jsonable_encoder`` hits it
    first, so the 500 lands on the same route with a different traceback.

    Iterative, not recursive, on purpose: this runs immediately after a ``safe_load`` whose
    own ``RecursionError`` the caller catches, so a recursive checker would add stack frames
    to a structure already near the limit and reintroduce the escape it exists to close.

    Two id sets, and both are load-bearing. ``on_path`` is scoped to the current path — added
    on descent, dropped on the way back up — so a diamond (the same object under two sibling
    keys) is not mistaken for a cycle. ``done`` records containers already fully explored, so
    each is entered once: O(nodes + edges). Without it the walk is O(*paths*), which a
    billion-laughs header turns exponential — a 310-byte alias pyramid that ``safe_load``
    parses in 0.7 ms took 1.6 s to check, and a 430-byte one did not finish. That runs at the
    WRITE seam, synchronously, in a threadpool slot, on attacker-supplied bytes.

    Dict keys are not walked: ``safe_load`` builds keys only from hashable scalars, and a
    tuple holding a dict or list is itself unhashable, so a key cannot be a recursive
    container either.
    """
    stack: list[tuple[Any, bool]] = [(value, False)]
    on_path: set[int] = set()
    done: set[int] = set()
    while stack:
        node, leaving = stack.pop()
        if leaving:
            on_path.discard(id(node))
            done.add(id(node))
            continue
        if not isinstance(node, dict | list | tuple):
            continue
        if id(node) in on_path:
            return True
        if id(node) in done:
            continue  # already proven acyclic; re-walking it is what makes this exponential
        on_path.add(id(node))
        stack.append((node, True))  # popped after this node's children: the "ascend" marker
        children = node.values() if isinstance(node, dict) else node
        stack.extend((child, False) for child in children)
    return False


#: Cap on the characters a parsed frontmatter expands to once its YAML aliases are followed
#: — roughly the length of the JSON a route would serialize it to. It exists to fire on alias
#: AMPLIFICATION and never on an alias-free document, and the margin for that is measured, not
#: assumed. The densest alias-free expansion is a ``!!timestamp``: ``2001-12-14,`` is 11 source
#: bytes and :data:`_TIMESTAMP_CHARS` counted characters, i.e. **2.909x**, ahead of a hex
#: integer's 1.2x and a string's 1.0x. Both surfaces cap the written FILE at 64 KiB
#: (``config_write_subagents.MAX_BYTES``, ``config_write_skills.MAX_SKILL_MD_BYTES``), so an
#: alias-free header a write could have produced tops out at ~190_625 — inside this cap, but
#: by 1.31x, not by the order of magnitude the byte figures suggest.
#:
#: ⚠️ That makes the two constants COUPLED: ``MAX_BYTES * 2.91`` must stay below this. Raising
#: the file cap to 128 KiB reads as safe and is not — it would put an alias-free document at
#: 381k and have this guard reject it as an alias pyramid.
#:
#: Not "never on an ordinary document", either: the READ path applies no byte cap
#: (``_read_agent`` reads whatever is on disk), so an alias-free header over ~86 KB degrades to
#: ``frontmatter: {}`` rather than serving. That is not a shape a subagent header takes, and it
#: degrades rather than fails.
MAX_EXPANDED_CHARS = 250_000

#: Characters an ISO-8601 ``!!timestamp`` serializes to, worst case
#: (``2001-12-14T21:59:43.100000+05:00``). Counted rather than folded into the one-character
#: default because a ``date``/``datetime`` is the highest-amplification leaf a pyramid can
#: alias: at one character each, a 500-byte header of timestamp leaves was still an accepted
#: 4 MB response. A ``float`` is counted by its own ``repr`` rather than by this, because its
#: SOURCE spelling can be much shorter (``1.``) and a flat 32 would then over-count an
#: alias-free document past :data:`MAX_EXPANDED_CHARS` — see :func:`_scalar_chars`. Only
#: ``bool`` and ``None`` are left at one character.
_TIMESTAMP_CHARS = 32


def _scalar_chars(node: Any) -> int:
    """Approximate the characters a *non-container* ``node`` serializes to (at least one).

    A count of *values* would be the wrong metric, because a value's serialized length is not
    bounded: one 30 KB string aliased into a pyramid's 46_656 leaf slots is 67_184 values but
    1.4 GB of JSON, and an integer is a string of digits with no length limit either. Both
    walk straight past a value-count cap; length is what the consumer actually pays.

    An int is counted by its DECIMAL digits, derived from ``bit_length`` (``log10(2)`` as a
    ratio of integers, rounded up, so the estimate is never short — the minus sign on a
    negative int is the one character it does not carry, a constant, like the scalars below).
    ``len(str(n))`` would be exact but CPython refuses ``str(int)`` above 4300 digits, which
    would turn this guard into a fresh ``ValueError`` — and a hex literal reaches that size
    without ``safe_load``'s own decimal-literal limit firing. Counting the BITS instead was the
    first attempt and was wrong in the other direction: it over-states by 3.3x, which rejected
    an alias-free ``n: 0x<65500 digits>`` header — a false rejection, and the exact property
    the cap claims not to have.

    A ``float`` is counted by its actual ``repr`` length, which runs to 23
    (``1.7976931348623157e+308``) and is the widest counted-vs-emitted gap otherwise left.
    ``repr`` and not a flat :data:`_TIMESTAMP_CHARS`, because a float's source spelling can be
    far SHORTER than a timestamp's: ``1.`` is two characters, so a flat 32 would count an
    alias-free list of them at **10.67x** its source — 698_915 characters for a 64 KiB header,
    past :data:`MAX_EXPANDED_CHARS`, falsely rejecting a document with no alias in it. Measured,
    and note ``test_byte_caps_stay_under_the_expansion_cap`` would NOT have caught it: that gate
    checks the byte caps against 3x, the timestamp ceiling. ``repr`` keeps a float at 1.0x, so
    the alias-free ceiling stays where the timestamp puts it. A float's repr is bounded, so this
    carries none of the ``str(int)`` digit-limit hazard above.

    The only scalars left counted as one are ``bool`` and ``None``, whose reprs are 4 and 5
    characters. The non-finite floats are the widest of these constants: ``repr`` gives
    ``inf``/``-inf``/``nan`` where ``json.dumps`` emits ``Infinity``/``-Infinity``/``NaN``,
    an under-count of at most 2.67x. Every one of them is a per-node constant, not a factor
    that grows with the document, which is the only property the cap needs.
    """
    if isinstance(node, str | bytes):
        return len(node)
    if isinstance(node, int):  # bool included: both bit_lengths floor to a single digit
        return node.bit_length() * 30_103 // 100_000 + 1
    if isinstance(node, float):  # bounded repr, so `repr` is safe here where `str(int)` is not
        return len(repr(node))
    if isinstance(node, datetime.date):  # `!!timestamp` — `datetime` is a `date` subclass
        return _TIMESTAMP_CHARS
    return 1


def _emitted_children(node: Any) -> Iterator[Any]:
    """Yield everything a serializer emits for container ``node`` — a dict's KEYS included."""
    if isinstance(node, dict):
        yield from node.keys()
        yield from node.values()
    else:
        yield from node


def _expands_beyond(value: Any, limit: int) -> bool:
    """Whether ``value`` expands past ``limit`` characters once its YAML aliases are followed.

    Memoizing :func:`redact_secrets` stops the *redaction* re-expanding an alias pyramid, but
    it does not make the document servable: the redacted view is then a DAG, and the next
    unguarded consumer — ``jsonable_encoder`` plus ``json.dumps`` on the route's response, in
    the event loop rather than a worker thread — expands it right back. Measured (#1393): a
    346-byte pyramid redacts in 0.04 ms and then spends **6.7–7.0 s in ``jsonable_encoder``
    plus 0.60 s in ``json.dumps``** to reach 101 MB. The encoder, not the dump, is the bulk —
    an earlier draft of this docstring quoted the 0.6 s dump alone and understated the block by
    an order of magnitude. A 472-byte pyramid redacts in 0.05 ms and never finishes. The
    expansion is inherent to the document, so the only fix is to refuse it, at the seam, the
    way a cycle is refused.

    Rejection is by expanded SIZE, not by alias count or depth. A plain alias is ordinary YAML
    (:func:`_is_self_referential` says why refusing aliases outright is wrong) and a depth cap
    was measured and does not work — the blow-up is in breadth, and capping depth left the
    346-byte case at 1.94 s. Size, in turn, is characters and not values: see
    :func:`_scalar_chars` for the two aliased-scalar shapes that slip past a value count, and
    :data:`_YAML_CONTAINERS` for the two container types that do.

    Requires an acyclic ``value``, which is why it runs only after :func:`_is_self_referential`
    has passed: a cycle would keep re-entering a node that never finishes and never gets a
    size. Acyclicity is also what bounds the walk — a node can never be re-encountered from
    inside its own subtree, so its "ascend" marker is always popped before any duplicate entry
    beneath it.

    Iterative, not recursive, for the same reason as the cycle check: it runs right after a
    ``safe_load`` whose own ``RecursionError`` the caller catches, so adding stack frames to a
    structure already near the limit would reintroduce the escape this seam exists to close.

    An approximation, and deliberately so: it counts the PARSED document, while what a route
    serializes is the *redacted* view, where a short secret-shaped value grows into the
    8-character :data:`REDACTION_SENTINEL`. That, and the bounded-repr scalars
    :func:`_scalar_chars` counts as one, put the true response size within a small constant
    factor of the count — a factor that does not grow with the document, which is the only
    property the cap needs. Being off by 32x on an exotic header is a slow response; being off
    by a factor that grows with the byte length is the unbounded hang this guard exists for.
    """
    sizes: dict[int, int] = {}
    stack: list[tuple[Any, bool]] = [(value, False)]
    while stack:
        node, leaving = stack.pop()
        if not isinstance(node, _YAML_CONTAINERS):
            continue
        if leaving:
            total = 1  # the brackets/braces the container itself costs
            for child in _emitted_children(node):
                # A container's own total is already memoized, because this marker pops only
                # after every child has ascended; anything else is a scalar, counted by length.
                counted = sizes.get(id(child))
                total += _scalar_chars(child) if counted is None else counted
                if total > limit:
                    return True
            sizes[id(node)] = total
            continue
        if id(node) in sizes:
            continue  # counted once, then reused: this is what keeps the walk linear
        stack.append((node, True))  # popped after this node's children: the "ascend" marker
        stack.extend((child, False) for child in _emitted_children(node))
    return False


def _yaml_error_where(exc: yaml.YAMLError, line_offset: int = 0, *, block_name: str) -> str:
    """Describe *where* a ``YAMLError`` happened, using nothing derived from the document.

    The rejection message reaches the browser — ``list_skills`` surfaces it as a skill's
    ``frontmatter_error`` — so it must satisfy invariant 4. Interpolating ``str(exc)`` did
    not: PyYAML writes the offending token into the message **prose** for at least three
    shapes, where it sits mid-line with no key anchor::

        found undefined alias 'sk-live-…'
        found duplicate anchor 'sk-live-…'; first occurrence …
        could not determine a constructor for the tag '!sk-live-…'

    :func:`redact_secret_lines` is line-anchored (it masks a ``key: value`` whose KEY looks
    secret-shaped), so it cannot reach a bare mid-line payload. Verified: all three survive
    it unmasked. A credential pasted into frontmatter therefore reached the dashboard.

    So this builds the message from the exception's **positions only** — the integer
    ``.line``/``.column`` off a mark, or ``ReaderError.position``, plus PyYAML's own class as
    the category (``ScannerError``, ``ComposerError``, …). Nothing here is derived from the
    document text, which makes it fail-closed by construction rather than by a predicate that
    has to be right about every message PyYAML will ever emit. Masking the quoted run instead
    was considered and rejected: an unquoted token in a future release would leak, and an
    unanchored sweep over the prose over-masks the useful part — the same dead end #1379
    records.

    ⚠️ **Only ``.line`` and ``.column`` are safe to touch on a mark.** A ``yaml.error.Mark``
    is an object, not an offset: it holds ``.buffer`` — the WHOLE document — and its
    ``__str__``/``get_snippet()`` render a source snippet. Interpolating the mark itself
    (``f"{mark}"``) would re-leak everything this function exists to withhold.

    ``line_offset`` shifts the reported line so it names the line in the FILE, not in the
    header slice: :func:`load_frontmatter_yaml`'s callers hand it ``FRONTMATTER_RE.group(1)``,
    which begins after the opening ``---`` fence. Without it every message pointed one line
    above the fault, and with the prose gone that number is all the operator has.

    ``block_name`` names the coordinate space the ``ReaderError`` branch counts characters in,
    and is **keyword-required with no default** so a new caller cannot inherit a wrong one:
    ``ops.run_doctor`` parses a whole ``clauster.yml`` (#1395), where "of the frontmatter
    block" would name a thing the file does not have.

    The cost is the ``problem`` phrase. The operator still gets the category, the position,
    and ``content`` in the editor beside it. Positions are reported 1-based to match PyYAML's
    own ``str()`` and any editor.
    """
    kind = type(exc).__name__
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        # Defence, not a live alternative: every error `safe_load` can raise sets
        # `problem_mark` (scanner/parser/composer/constructor all use the 4-arg
        # MarkedYAMLError form). The short-arg raises live in the emitter/representer/
        # serializer/resolver, which `safe_load` never touches.
        mark = getattr(exc, "context_mark", None)
    if mark is not None:
        where = f"line {mark.line + 1 + line_offset}, column {mark.column + 1}"
        other = getattr(exc, "context_mark", None)
        if other is not None and (other.line, other.column) != (mark.line, mark.column):
            # `context_mark` means different things per shape and this seam deliberately does
            # not know which error it is holding — that is what makes it trustworthy. For an
            # unterminated quote it is where the construct opened; for a duplicate anchor it is
            # the FIRST occurrence. So the label is shape-neutral: "started at" would be a lie
            # on the duplicate-anchor shape, which is one of the three this fix exists for.
            # Both numbers are the ones the operator needs either way.
            line, column = other.line + 1 + line_offset, other.column + 1
            where += f" (see also line {line}, column {column})"
        return f" ({kind} at {where})"
    position = getattr(exc, "position", None)
    if isinstance(position, int):
        # `ReaderError` is the real no-mark shape, and not hypothetical: a control character
        # pasted into frontmatter (terminal output, an escape sequence) raises it. It carries
        # a plain character offset instead of a mark — positions-only by definition.
        #
        # Named as an offset into whatever `block_name` says was handed to `safe_load`, which
        # for the frontmatter callers is the header SLICE and not the file: `.position` indexes
        # that string, and `line_offset` cannot convert it — the shift is the length of the
        # opening fence, which varies (`---\n` vs `---\r\n`, plus the trailing whitespace
        # `FRONTMATTER_RE` tolerates) and this seam does not see it. Saying which coordinate
        # space it is beats sending an operator counting from the top of the file a few
        # characters wrong. `run_doctor` hands over a whole file, so it says so.
        return f" ({kind} at character {position} of the {block_name})"
    return f" ({kind})"


def _json_unsafe(node: Any) -> str | None:
    """Reason ``node`` cannot cross the JSON response path, or ``None`` if it can.

    ``safe_load`` builds four scalar shapes that pass every parse and structural guard and
    then raise on the route's serializer — the response body is
    ``json.dumps(jsonable_encoder(obj), allow_nan=False, ensure_ascii=False).encode("utf-8")``
    (Starlette's ``JSONResponse``) — or, for a huge int in KEY position, inside
    :func:`redact_secrets`' ``str(k)`` before any serializer runs (#1415):

    * a non-finite float (``.nan``/``.inf``/``-.inf``): ``allow_nan=False`` makes ``json.dumps``
      raise ``ValueError``.
    * an int whose decimal length exceeds :func:`sys.get_int_max_str_digits`: ``str(int)``
      raises, felling both ``json.dumps`` and ``redact_secrets``' key stringification. A hex
      literal reaches this size where the same value in decimal is refused by ``safe_load``
      itself. Measured by ``bit_length``, never by ``str(node)``, because forming that string
      is the raise this guard avoids — the estimate (``log10(2)`` as a ratio of integers,
      rounded up) is never short, like :func:`_scalar_chars`, at the cost of refusing an int
      of exactly the limit's digit count on the rare bit-length that rounds up by one. When
      the limit is DISABLED (``get_int_max_str_digits()`` returns ``0``), ``str(int)`` never
      raises, so no int is refused.
    * a ``str`` holding a lone surrogate code point (U+D800 to U+DFFF): JSON accepts it, but
      the ``.encode("utf-8")`` of the response body raises ``UnicodeEncodeError``.
    * ``!!binary`` bytes that are not valid UTF-8: ``jsonable_encoder`` raises
      ``UnicodeDecodeError`` decoding them for the response.
    """
    if isinstance(node, bool):
        return None  # a bool is an int subclass, and serializes fine
    if isinstance(node, float):
        if not math.isfinite(node):
            return "a non-finite float (.nan/.inf) that JSON cannot represent"
        return None
    if isinstance(node, int):
        limit = sys.get_int_max_str_digits()
        if limit and node.bit_length() * 30_103 // 100_000 + 1 > limit:
            return "an integer too large for the interpreter to serialize"
        return None
    if isinstance(node, str):
        try:
            node.encode()
        except UnicodeEncodeError:
            return "a string with a lone surrogate that UTF-8 cannot encode"
        return None
    if isinstance(node, bytes):
        try:
            node.decode()
        except UnicodeDecodeError:
            return "a !!binary value whose bytes are not valid UTF-8"
        return None
    return None


def _first_json_unsafe(value: Any) -> str | None:
    """First :func:`_json_unsafe` reason among ``value``'s scalars, KEYS included, or None.

    Walks keys as well as values (:func:`_emitted_children`) so a huge int in a ``?``-explicit
    or ``!!omap`` KEY — which reaches ``str(k)`` in :func:`redact_secrets` before any
    serializer, and whose ``?`` syntax bypasses PyYAML's 1024-character simple-key cap
    (#1415) — is refused too, not only a value-position one.

    Iterative, and runs after :func:`_is_self_referential`, for the same reason as
    :func:`_expands_beyond`: the acyclic structure bounds the walk, and ``seen`` keeps a
    shared (aliased) subtree from being re-walked.
    """
    seen: set[int] = set()
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, _YAML_CONTAINERS):
            if id(node) in seen:
                continue
            seen.add(id(node))
            stack.extend(_emitted_children(node))
            continue
        if (reason := _json_unsafe(node)) is not None:
            return reason
    return None


def load_frontmatter_yaml(header: str, *, what: str, line_offset: int = 0) -> Any:
    """``safe_load`` a frontmatter ``header``, mapping every failure to a rejection.

    The shared YAML seam behind both ``parse_frontmatter`` implementations, for the same
    reason :data:`FRONTMATTER_RE` is shared: each surface names itself via ``what``
    (``"frontmatter"`` / ``"SKILL.md frontmatter"``) but the *contract* — only
    :class:`InvalidCandidateError` escapes — is enforced in one place.

    ``safe_load`` never evaluates the document (no ``python/object`` construction), but
    the composer and ``SafeConstructor`` can raise outside :class:`yaml.YAMLError`, and
    each escape used to reach the route as a 500 on a path documented to fail as a 422:

    * ``RecursionError`` — deeply-nested flow collections overflow the composer before it
      can raise ``YAMLError`` (#1326).
    * A resolvable **explicit tag** whose scalar does not fit it (#1354): ``!!int x`` and
      ``!!float x`` raise ``ValueError``, ``!!bool x`` a ``KeyError``, ``!!timestamp x``
      an ``AttributeError`` (``construct_yaml_timestamp`` calls ``.groupdict()`` on an
      unchecked ``re.match``), and an **empty or all-underscore scalar** under ``!!int``
      / ``!!float`` an ``IndexError`` — both constructors index the scalar before
      parsing it. Every *other* tag already fails as a ``ConstructorError``, which is a
      ``YAMLError``. Caught only around the ``safe_load`` call itself, so nothing beyond
      the parse is swallowed.

    The bullets record where each class was SEEN, not a proof of exhaustiveness — the
    fuzz harness (``fuzz/parse_frontmatter_fuzzer.py``) is what hunts for members this
    tuple is still missing; a new one belongs in the tuple below, not in a wider
    blanket ``except``.

    All of them are the same structural verdict — we could not parse the header, so we
    refuse to act on it. Fails closed: this only ever converts a crash into a rejection,
    never a rejection into an accept.

    Three rejections are NOT parse failures. A header that parses fine but builds a
    self-referential structure (:func:`_is_self_referential`) is refused after the load,
    because no consumer can finish walking it; so is one that parses fine but expands past
    :data:`MAX_EXPANDED_CHARS` through its aliases (:func:`_expands_beyond`), because no
    consumer can finish serializing it; and so is one that parses fine but holds a scalar
    the JSON response path cannot represent (:func:`_first_json_unsafe`), because that
    scalar would 500 the route where this tier's contract is 422 (#1415).

    ``line_offset`` is added to any line number the rejection reports, so a caller passing a
    slice of a larger file can have the message name the line in the FILE. Both
    ``parse_frontmatter`` implementations pass ``1``: they hand us ``FRONTMATTER_RE.group(1)``,
    which begins after the opening ``---`` fence — exactly one line in.
    """
    try:
        value = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise InvalidCandidateError(
            f"{what} is not valid YAML"
            f"{_yaml_error_where(exc, line_offset, block_name='frontmatter block')}"
        ) from exc
    except RecursionError as exc:
        raise InvalidCandidateError(f"{what} is nested too deeply to parse") from exc
    except (ValueError, KeyError, AttributeError, IndexError) as exc:
        # The class name ONLY — never the exception's payload. PyYAML embeds the offending
        # scalar in the unfitting-value shapes ("KeyError('<the value>')"), and this message is
        # surfaced to the browser as a skill's ``frontmatter_error``, so echoing it would
        # put a credential pasted into a frontmatter value straight onto the dashboard:
        # :func:`redact_secret_lines` scans line-anchored ``key: value`` pairs, and a bare
        # payload sits mid-line where that scanner cannot reach it.
        #
        # The ``YAMLError`` branch above no longer interpolates ``exc`` either (#1369): it
        # used to, on the theory that its mark re-emits the source line with its key intact
        # for the redactor to mask — the common shape, but not a guarantee. PyYAML also
        # writes the token into message *prose* ("found undefined alias 'X'", duplicate
        # anchor, unknown tag), mid-line and unreachable by the line-anchored scanner. Both
        # branches are now positions-and-class-name only; see :func:`_yaml_error_where`.
        raise InvalidCandidateError(
            f"{what} has a YAML tag its value does not satisfy ({type(exc).__name__})"
        ) from exc
    if _is_self_referential(value):
        # Well-formed YAML that no consumer can finish walking — see :func:`_is_self_referential`.
        # Rejecting here is the fail-closed direction: it stops the write, so such a document
        # never reaches disk. A document an EARLIER release wrote still reads back — `_read_agent`,
        # `_list_agents` and `list_skills` each catch this error around the parse and degrade the
        # derived frontmatter field while still surfacing `content`, so the operator can see and
        # repair the file. That is why no second guard is needed in `redact_secrets`.
        raise InvalidCandidateError(f"{what} contains a self-referential YAML alias")
    if _expands_beyond(value, MAX_EXPANDED_CHARS):
        # Acyclic, so the check above passes it, and a few hundred bytes long, so every byte
        # cap passes it — but a `&a`/`*a` pyramid expands to more than any consumer can walk or
        # serialize (#1393). Same fail-closed shape and same degradation on read as the cycle
        # rejection above; see :func:`_expands_beyond` for why memoizing the consumers is not
        # enough on its own.
        # Names the cap, like every sibling cap on this tier (`content is N bytes, over the
        # M byte cap`). The counted SIZE is deliberately not named: `_expands_beyond`
        # short-circuits the moment it passes the limit, so it does not have one to report,
        # and finishing the count to produce a nicer message is the work this guard exists to
        # refuse. Nothing here comes from the document — invariant 4.
        raise InvalidCandidateError(
            f"{what} expands past the {MAX_EXPANDED_CHARS} character cap through its YAML aliases"
        )
    if (reason := _first_json_unsafe(value)) is not None:
        # Parses and fits every size guard, but a scalar in it cannot cross the JSON
        # response path (#1415): the route would 500 where this tier's contract is 422.
        # Rejecting at the seam covers KEY positions too, which a coerce-just-before-the-
        # response fix misses — a huge int in a key hits `str(k)` in `redact_secrets` before
        # any serializer. The reason is a static class label, never the offending scalar —
        # invariant 4.
        raise InvalidCandidateError(f"{what} contains {reason}")
    return value


def load_settings_json_obj(raw: bytes) -> dict[str, Any]:
    """Parse ``raw`` bytes as a JSON object, returning ``{}`` for empty/whitespace.

    Shared by the child surfaces to parse an existing settings/config file before
    merging into it. A non-object or malformed JSON is a structural error (→ caller
    maps to 422): we will not overwrite a file we could not parse.
    """
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise InvalidCandidateError(f"existing settings file is not valid UTF-8: {exc}") from exc
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidCandidateError(f"existing settings file is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # Before CPython 3.14.7, deeply-nested JSON overflowed the recursive scanner before
        # json could raise JSONDecodeError, and RecursionError is not a ValueError — so it
        # escaped the handler above and left this function raising outside its documented
        # contract. (3.14.7+ bounds the depth itself and raises JSONDecodeError, which the
        # handler above already maps.) Fail closed as the same structural error: we still
        # refuse to overwrite it.
        raise InvalidCandidateError(
            "existing settings file is nested too deeply to parse"
        ) from exc
    if not isinstance(data, dict):
        raise InvalidCandidateError("existing settings file is not a JSON object")
    return data


def render_json(data: dict[str, Any]) -> str:
    """Render ``data`` as pretty JSON with a trailing newline (matches CLI style)."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def project_settings_path(project_dir: Path) -> Path:
    """Return the project-scope settings file path (``<project>/.claude/settings.json``)."""
    return project_dir / ".claude" / "settings.json"


def project_local_settings_path(project_dir: Path) -> Path:
    """Return the local-scope settings file path (``<project>/.claude/settings.local.json``).

    Mirrors :func:`project_settings_path` for the third (local) scope: a real file
    that is **you, this project only** — never shared, never committed (see
    :func:`ensure_gitignored`, which the local-scope writers call *before* their write,
    since that write drops a secret-bearing ``.bak``). Same stale-hash / atomic-write
    discipline as the project file.
    """
    return project_dir / ".claude" / "settings.local.json"


def ensure_gitignored(
    project_dir: Path, relative_path: str, *, ignore_backup_sibling: bool = False
) -> None:
    """Idempotently append ``relative_path`` to ``<project_dir>/.gitignore``.

    Mirrors Claude Code's own behavior: a project-local file (``.claude/settings.local
    .json``, ``CLAUDE.local.md``) is private to the operator and must never be
    accidentally committed.

    **Call order depends on whether the writer takes a backup.** The four
    ``settings.local.json`` writers call this **before** their write: that write drops a
    ``<name>.bak`` holding the PREVIOUS (unredacted) ``env`` values, so an ignore that
    failed afterwards would leave a secret-bearing file trackable. Ignoring first is the
    fail-closed order, at the cost of a failed write (stale-hash 409, malformed-JSON 422)
    leaving a ``.gitignore`` entry for a file that was not created — harmless, and this
    call is exact-line idempotent so the later successful write is a no-op here.
    :func:`clauster.claude_md.write_local` legitimately still calls this *after* its write,
    because its writer takes no backup and so has no such window. Shape validation
    (:func:`validate_candidate`) runs before either order, so a 422 for a malformed
    candidate still never reaches ``.gitignore``.

    ``ignore_backup_sibling`` additionally ignores the ``<relative_path>.bak``
    sibling. The backup-taking JSON writers reach
    :func:`~clauster.claude_json._atomic_write_json`, which — on an overwrite — drops
    a one-time ``<name>.bak`` holding the *previous* file contents beside the target.
    For the secret-bearing ``settings.local.json`` that snapshot can carry the prior
    ``env`` map (API keys and the like), so ignoring only the file itself lets the
    plaintext backup be committed and pushed. Ignoring the sibling up front closes
    that gap before the ``.bak`` is ever created. Callers whose writer takes no backup
    (``CLAUDE.local.md`` is replaced without one) leave this off, so no phantom
    ``.bak`` entry is added for a file that never exists.

    Idempotent: an entry already present as an exact line (surrounding whitespace
    ignored) is left untouched — no duplicate append, and the existing content is
    never rewritten or reordered (only ever appended to). A missing ``.gitignore`` is
    created. This is deliberately a simple exact-line check, not a full
    gitignore-pattern matcher — good enough for the fixed, known entries this surface
    writes, and conservative (a near-duplicate pattern is harmless, just untidy, never
    unsafe).
    """
    gitignore = project_dir / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    present = {line.strip() for line in existing.splitlines()}
    wanted = [relative_path]
    if ignore_backup_sibling:
        wanted.append(relative_path + ".bak")
    missing = [entry for entry in wanted if entry not in present]
    if not missing:
        return
    with gitignore.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        for entry in missing:
            fh.write(entry + "\n")


def write_settings_subtree(
    path: Path,
    subtree_key: str,
    incoming: Any,
    expected_hash: str | None,
    *,
    merge: Callable[[Any, Any], Any] | None = None,
) -> None:
    """Locked write of a named subtree of a settings/config JSON file, fail-closed.

    Shared writer used by the child config-write surfaces (MCP #688, permissions #689,
    hooks #690) to avoid duplicating the stale-hash / parse / merge / render pipeline:

    1. Under the lock, read the current bytes.
    2. Stale-hash guard (→ 409 :class:`StaleConfigWriteError`): a ``None`` hash is the
       legitimate first-write path; an existing file refuses an unguarded overwrite.
    3. Parse the bytes (→ 422 :class:`InvalidCandidateError` on malformed JSON).
    4. Merge: if ``merge`` is given, call ``merge(incoming, current[subtree_key])`` so
       the MCP surface can run :func:`merge_redacted` (keep-stored sentinel handling);
       otherwise set ``current[subtree_key] = incoming`` directly.
    5. Atomic replace via the shared :func:`~clauster.claude_json.locked_replace_json_file`
       machinery (``flock`` + ``.bak`` + ``mkstemp`` + ``os.replace``).

    The caller must have already run the capability gate, type-the-name confirm, path
    containment, and structural validation — this helper trusts an already-validated
    ``incoming``.
    """

    def _mutate(current_bytes: bytes) -> dict[str, Any]:
        """Check the expected hash, then merge ``incoming`` into the target subtree."""
        if expected_hash is None:
            if current_bytes:
                raise StaleConfigWriteError(f"{path.name} already exists; a hash is required")
        elif hash_bytes(current_bytes) != expected_hash:
            raise StaleConfigWriteError("config file changed on disk since it was loaded")
        current = load_settings_json_obj(current_bytes)
        stored = current.get(subtree_key)
        current[subtree_key] = merge(incoming, stored) if merge is not None else incoming
        return current

    locked_replace_json_file(path, _mutate, render=render_json)
