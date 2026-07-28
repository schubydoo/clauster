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
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

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
# The line-scanning (non-anchored) twin of _SECRETISH_URL_RE, for masking a
# credential-bearing URL that appears *anywhere* in a line of free text (see
# redact_secret_lines), not just when the whole value is the URL.
_SECRETISH_URL_INLINE_RE = re.compile(r"[a-z][a-z0-9+.\-]*://[^/@\s]+@", re.IGNORECASE)
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


def _is_secretish(key: str, value: Any) -> bool:
    """Whether ``(key, value)`` should be masked (structural secret detection)."""
    if not isinstance(value, str) or not value:
        return False
    if _SECRET_KEY_RE.search(key):
        return True
    if _has_interp(value):
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
    reads as ``None`` — never an error; a missing local config is simply empty.
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
