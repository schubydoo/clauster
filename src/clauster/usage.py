"""Cost / token tracking from a session transcript JSONL (v0.3).

Parses the per-session transcript the bridge writes (``…/<sanitized-cwd>/<uuid>.jsonl``),
sums the token usage across assistant messages, groups it by model, and estimates a
USD cost from a price table.

The cost is **approximate and informational**: the price table is hand-maintained
(USD per million tokens, as of 2026-05) and will drift as pricing changes — callers
should treat the dollar figure as a ballpark and can pass their own ``prices``.
Token counts, by contrast, are exact (read straight from the transcript's ``usage``).
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import redact
from .pointers import CLAUDE_PROJECTS_DIR, WORKTREE_SUBDIR, sanitize_cwd

# aggregate_project_usage re-streams every line of every transcript on each call,
# and the /api/projects/{name}/usage badge is fetched once per project at first
# paint (and again on reconnect). Transcripts are append-mostly, so a
# (file_count, total_size, max_mtime_ns) stamp of the transcript directory is a
# cheap, sound invalidation key: an appended line grows the file and bumps its
# mtime, and a new/removed session changes the count. Folding in total_size (an
# append always grows the file) catches a write whose mtime lands in the same
# coarse filesystem tick, so a stale rollup is never served. The cache below
# collapses repeat rollups within a short window while staying correct — it
# re-parses whenever the stamp moves or the TTL lapses.
USAGE_CACHE_TTL_SECONDS = 2.0


@dataclass(frozen=True)
class ModelPrice:
    """USD per *million* tokens for each token category."""

    input: float
    output: float
    cache_write: float  # 5-minute ephemeral cache creation
    cache_read: float


# Hand-maintained ballpark prices (USD / Mtok), as of 2026-05. Matched by family
# substring of the model id (e.g. "claude-opus-4-7" -> opus). Update as pricing moves.
PRICES: dict[str, ModelPrice] = {
    "opus": ModelPrice(input=15.0, output=75.0, cache_write=18.75, cache_read=1.5),
    "sonnet": ModelPrice(input=3.0, output=15.0, cache_write=3.75, cache_read=0.3),
    "haiku": ModelPrice(input=0.8, output=4.0, cache_write=1.0, cache_read=0.08),
}


def _as_int(value: object) -> int:
    """Coerce a usage token field to int; 0 for missing/null/non-numeric values.

    A malformed token value (``"abc"``, ``[1, 2]``, ``null``, or a non-finite
    ``NaN``/``Infinity`` — which ``json.loads`` accepts by default) must not abort the
    whole project rollup with an unhandled coercion error — contribute 0 instead,
    honoring the parser's skip-a-corrupt-line contract.
    """
    if isinstance(value, int):
        # bool is an int subclass (True→1). A Python int is arbitrary-precision and
        # never raises — crucially it must NOT go through math.isfinite below, which
        # coerces to a C double and OverflowErrors on a huge JSON integer literal.
        return value
    if isinstance(value, float):
        # int(nan) raises ValueError and int(±inf) raises OverflowError; json.loads
        # decodes bare NaN/Infinity tokens to these, so coerce a non-finite float to 0.
        return int(value) if math.isfinite(value) else 0
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


@dataclass
class TokenTotals:
    """Running token + message counts for one usage bucket (e.g. a model or day)."""

    input: int = 0
    output: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    messages: int = 0

    def add_usage(self, usage: dict) -> None:
        """Fold one message's ``usage`` dict into these totals (counts one message).

        Token fields are coerced with :func:`_as_int`, so a missing, null, or
        non-numeric value contributes 0 rather than raising — one malformed record
        must never abort the whole rollup (which would 500 the usage endpoint).
        """
        self.input += _as_int(usage.get("input_tokens"))
        self.output += _as_int(usage.get("output_tokens"))
        self.cache_creation += _as_int(usage.get("cache_creation_input_tokens"))
        self.cache_read += _as_int(usage.get("cache_read_input_tokens"))
        self.messages += 1

    def merge(self, other: TokenTotals) -> None:
        """Accumulate another bucket's counts into this one."""
        self.input += other.input
        self.output += other.output
        self.cache_creation += other.cache_creation
        self.cache_read += other.cache_read
        self.messages += other.messages

    @property
    def total_tokens(self) -> int:
        """Sum of input, output, and cache (creation + read) tokens."""
        return self.input + self.output + self.cache_creation + self.cache_read


def _price_for(model: str, prices: dict[str, ModelPrice]) -> ModelPrice | None:
    """Return the first price whose family name is a substring of ``model``, else None."""
    low = model.lower()
    for family, price in prices.items():
        if family in low:
            return price
    return None


def cost_usd(
    model: str, totals: TokenTotals, prices: dict[str, ModelPrice] = PRICES
) -> float | None:
    """Approx USD for one model's totals, or None if the model isn't in the table."""
    price = _price_for(model, prices)
    if price is None:
        return None
    return (
        totals.input * price.input
        + totals.output * price.output
        + totals.cache_creation * price.cache_write
        + totals.cache_read * price.cache_read
    ) / 1_000_000


class _ByModelAggregate:
    """Shared token/cost rollup over a ``by_model`` mapping.

    A plain mixin (not a dataclass) so the two concrete aggregates below can each
    declare their own dataclass fields without inherited-default ordering trouble;
    they only have to provide a ``by_model`` attribute.
    """

    by_model: dict[str, TokenTotals]

    @property
    def totals(self) -> TokenTotals:
        """Every per-model total merged into one ``TokenTotals``."""
        agg = TokenTotals()
        for t in self.by_model.values():
            agg.merge(t)
        return agg

    def cost_usd(self, prices: dict[str, ModelPrice] = PRICES) -> float:
        """Total approx USD across known models (unpriced models contribute 0)."""
        return sum((cost_usd(m, t, prices) or 0.0) for m, t in self.by_model.items())

    def unpriced_models(self, prices: dict[str, ModelPrice] = PRICES) -> list[str]:
        """List the models seen here that have no entry in the price table."""
        return [m for m in self.by_model if _price_for(m, prices) is None]


@dataclass
class TranscriptUsage(_ByModelAggregate):
    """Token usage from a single transcript JSONL."""

    path: Path
    by_model: dict[str, TokenTotals] = field(default_factory=dict)


@dataclass
class ProjectUsage(_ByModelAggregate):
    """Token usage aggregated across all of a project's transcripts."""

    project: str
    transcript_count: int = 0
    by_model: dict[str, TokenTotals] = field(default_factory=dict)


def _iter_transcript_lines(path: Path) -> Iterator[str]:
    """Yield raw text lines from a transcript JSONL, opening with UTF-8 errors='replace'.

    Invalid UTF-8 bytes from the external claude bridge are replaced rather than
    crashing the iterator — a replaced char either parses fine downstream or trips
    the per-line JSON skip, never aborting the whole read. Raises
    :class:`FileNotFoundError` on missing or unreadable file.
    """
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError) as exc:
        raise FileNotFoundError(f"transcript not found: {path}") from exc
    with fh:
        yield from fh


def parse_transcript(path: Path) -> TranscriptUsage:
    """Aggregate token usage from a transcript JSONL, grouped by model.

    Tolerant: blank lines and malformed records are skipped; a record contributes only
    when it carries a ``message`` dict holding a ``usage`` dict — in practice the
    assistant messages, though the role itself is not checked. The transcript can be
    huge, so it is streamed line by line (never loaded whole).
    """
    result = TranscriptUsage(path=Path(path))
    for line in _iter_transcript_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip a corrupt line rather than abort the whole tally
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        model = message.get("model") or "unknown"
        result.by_model.setdefault(model, TokenTotals()).add_usage(usage)
    return result


def transcript_paths_for(
    project_path: Path, claude_projects_dir: Path = CLAUDE_PROJECTS_DIR
) -> list[Path]:
    """Every transcript JSONL Claude has written for ``project_path``.

    Claude stores per-session transcripts under ``<claude_projects_dir>/<sanitized-cwd>/``,
    where the directory name is the cwd with every non-alphanumeric character replaced
    by ``-`` (the same mapping the bridge-pointer walk uses).

    A **worktree-spawn** session runs with the worktree as its cwd, so Claude files it under
    a different sanitized directory — a sibling of the project's own. Those sessions belong
    to the project — Claude Code scopes its own session-id lookup to "the current project
    directory and its git worktrees" (code.claude.com/docs/en/sessions.md), so this widening
    matches the CLI rather than outrunning it. The project directory AND every worktree
    sibling are therefore searched; keying
    only on the project root hid worktree conversations from the launch popover's Conversation
    picker and left their tokens out of the usage rollup (#1020). A directory that is absent
    or unreadable contributes nothing rather than failing the walk.
    """
    out: list[Path] = []
    own = Path(claude_projects_dir) / sanitize_cwd(Path(project_path))
    try:
        out.extend(p for p in own.glob("*.jsonl") if p.is_file())
    except OSError:
        pass
    for directory in _worktree_candidate_dirs(project_path, claude_projects_dir):
        try:
            candidates = [p for p in directory.glob("*.jsonl") if p.is_file()]
        except OSError:
            continue
        # Candidate dirs are matched on a LOSSY name prefix, so each file must prove it
        # belongs here from its own recorded cwd before it is listed.
        out.extend(p for p in candidates if _transcript_is_owned(project_path, p))
    return sorted(out)


# Ownership proofs are read from transcript CONTENT, so they are cached on the same
# (path, mtime_ns, size) identity the summary cache uses — a transcript's recorded cwd
# cannot change without the file changing. Keeps `_transcript_dir_stamp` (documented as a
# cheap probe) from re-reading every worktree candidate on each poll. Cleared wholesale
# at the cap rather than evicted per-entry: the working set is one project's worktrees.
_CWD_CACHE_MAX = 4096
_CWD_CACHE: dict[tuple[str, int, int, int], tuple[str | None]] = {}


def _worktree_candidate_dirs(project_path: Path, claude_projects_dir: Path) -> list[Path]:
    """Directories that MIGHT hold transcripts from ``project_path``'s git worktrees.

    Candidates only — every file taken from one must still clear
    :func:`_transcript_is_owned`. Worktree transcripts have to be found by scanning, not by
    listing the live worktrees: a worktree is usually ``git worktree remove``d when its
    session ends while the transcript it produced stays on disk forever, and those finished
    conversations are exactly what the Conversation picker offers. Enumerating only extant
    worktrees would hide nearly all of them.

    Scanning means matching directory NAMES, and ``sanitize_cwd`` is lossy: it maps every
    non-alphanumeric to ``-``, so ``/``, ``.``, ``-`` and ``_`` are indistinguishable
    afterwards. That makes this prefix test **unsound in both directions** and it is
    deliberately not trusted on its own — see :func:`_transcript_is_owned`.
    """
    base = Path(claude_projects_dir)
    project = Path(project_path)
    worktree_prefix = sanitize_cwd(project / WORKTREE_SUBDIR)
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    return [e for e in entries if e.is_dir() and e.name.startswith(f"{worktree_prefix}-")]


def _recorded_cwd(path: Path, max_lines: int = 200) -> str | None:
    """Return the cwd a transcript records for itself, or ``None`` if it does not.

    Claude writes a ``cwd`` on its conversation records, so a transcript states which
    directory produced it. That is the only NON-ambiguous ownership signal available:
    the containing directory name is a lossy one-way hash of that same path.

    Bounded scan — the leading records can be ``queue-operation``/``attachment`` entries
    that carry no cwd, so it reads until one does, up to ``max_lines``. A missing,
    unreadable or cwd-less transcript returns ``None``, which callers treat as unproven.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    # `max_lines` is part of the key: the answer is "the cwd found WITHIN this bound", so a
    # miss at a small bound must not be served to a caller asking with a larger one.
    key = (str(path), stat.st_mtime_ns, stat.st_size, max_lines)
    cached = _CWD_CACHE.get(key)
    if cached is not None:
        return cached[0]
    found: str | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    break
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                    if record["cwd"]:
                        found = record["cwd"]
                        break
    except OSError:
        return None
    if len(_CWD_CACHE) >= _CWD_CACHE_MAX:
        _CWD_CACHE.clear()
    _CWD_CACHE[key] = (found,)
    return found


def _transcript_is_owned(project_path: Path, path: Path) -> bool:
    """Whether a worktree-candidate transcript really belongs to ``project_path``.

    Proven from the transcript's OWN recorded cwd, which must resolve inside
    ``<project>/.claude/worktrees``. The directory name is NOT evidence: ``sanitize_cwd``
    collapses ``/``, ``.``, ``-`` and ``_`` alike, so wildly different real paths produce
    the same name. Two of those collisions are reachable in practice:

    * a *sibling project* named ``<project>--claude-worktrees-x``, and
    * a path that is not under the projects root at all — ``/srv/projects-foo--claude-
      worktrees-x`` sanitizes exactly like ``/srv/projects/foo/.claude/worktrees/x``.

    The second is why filtering by "is it a sibling project?" was not enough: the colliding
    family is unbounded, so no enumeration of neighbours can close it. This matters because
    :func:`resolve_session_transcript` is the ownership proof behind the pty resume path —
    an unowned transcript that resolved here could be read, or branched into this project's
    session via ``--resume <uuid> --fork-session``.

    Fails closed: a transcript that does not state a cwd is not proven, so it is refused.
    """
    cwd = _recorded_cwd(path)
    if cwd is None:
        return False
    try:
        return Path(cwd).resolve().is_relative_to((Path(project_path) / WORKTREE_SUBDIR).resolve())
    except (OSError, ValueError):
        return False


def _render_content(content: object) -> str:
    """Flatten a message ``content`` to plain text for the read-only viewer.

    A turn's ``message.content`` is either a plain string or a list of typed
    blocks. We surface the text blocks (``{"type": "text", "text": …}`` or a
    bare string element) and, for this first text-turns-only cut, summarize a
    non-text block (``tool_use``/``tool_result``/image/…) as a one-line
    ``[<type>]`` placeholder rather than dumping its raw payload — never raising
    on an unexpected shape. The returned text is **not** yet redacted; the
    caller passes it through :func:`redact.sanitize_line` before it leaves this
    module.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        # An unexpected (non-str, non-list) shape (None, or a bare dict the bridge
        # doesn't write today): never dump its raw payload — surface a generic
        # ``[content]`` placeholder, consistent with the block-level summarization
        # below. A malformed record must summarize, not abort the page.
        return "" if content is None else "[content]"
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        else:
            # Summarize a non-text block (tool_use, tool_result, image, thinking,
            # …) as a typed placeholder; the raw payload is intentionally omitted
            # in this first cut (issue #431: "text turns first").
            label = btype if isinstance(btype, str) and btype else "block"
            parts.append(f"[{label}]")
    return "\n".join(parts)


def _line_to_turn(line: str) -> dict | None:
    """Parse one transcript JSONL line into a redacted, render-ready turn (or ``None``).

    Returns ``{role, content, model, timestamp}`` with every free-text field
    passed through :func:`redact.sanitize_line` — so a session/env identifier or
    an obvious secret shape can never reach the browser. ``None`` is returned for
    a line that isn't a renderable message: blank, not JSON, not a dict, missing a
    ``message`` dict, or missing a non-empty ``role`` (mirroring the skip rules of
    :func:`parse_transcript`). Never raises on a malformed line.

    The single source of truth for "JSONL record → redacted turn", shared by both
    :func:`read_transcript_turns` (whole file) and
    :func:`read_transcript_turns_from_offset` (incremental tail) so the redaction
    and skip semantics can never drift between the two readers.
    """
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None  # skip a corrupt line rather than abort the whole page
    if not isinstance(record, dict):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if not isinstance(role, str) or not role:
        return None  # a turn without a role isn't a renderable message
    text = _render_content(message.get("content"))
    model = message.get("model")
    timestamp = record.get("timestamp")
    return {
        # Every rendered field that can carry user/model free text is
        # sanitized before it leaves this reader (D11 redaction order).
        "role": redact.sanitize_line(role),
        "content": redact.sanitize_line(text),
        "model": redact.sanitize_line(model) if isinstance(model, str) else None,
        "timestamp": timestamp if isinstance(timestamp, str) else None,
    }


def read_transcript_turns(path: Path) -> list[dict]:
    """Stream a transcript JSONL into a list of redacted, render-ready turns.

    Each turn is ``{role, content, model, timestamp}`` where ``content`` is the
    message text flattened by :func:`_render_content` and then passed through
    :func:`redact.sanitize_line` — so a session/env identifier or an obvious
    secret shape can never reach the browser. ``role``/``content``/``model`` come
    from ``record["message"]`` and ``timestamp`` from the record (mirroring
    :func:`parse_transcript`).

    Tolerant like :func:`parse_transcript`: blank and malformed lines, and
    records without a ``message`` dict, are skipped rather than raising. The
    transcript can be huge, so it is streamed line by line (never loaded whole);
    being pure and blocking, the caller runs it off the event loop.
    """
    turns: list[dict] = []
    for line in _iter_transcript_lines(path):
        turn = _line_to_turn(line)
        if turn is not None:
            turns.append(turn)
    return turns


def _is_subagent_transcript(path: Path, max_records: int = 200) -> bool:
    """Whether ``path`` is a SUBAGENT (sidechain) transcript rather than a real conversation.

    Claude Code stamps every record it writes for a dispatched subagent with
    ``"isSidechain": true``; records belonging to the top-level conversation carry
    ``false``. A subagent run therefore produces a transcript whose records are
    *uniformly* sidechain, which is what distinguishes it from a conversation a user
    would want to fork — the picker listed those alongside real ones and buried them
    (#1092).

    Read-only inspection of a file clauster does not own (invariant 5): the transcript is
    only parsed, never rewritten, and an unparseable line is skipped rather than raising.

    **Deliberately biased toward "real conversation".** ``True`` is returned only when,
    within the first ``max_records`` records, at least one record is flagged sidechain and
    **none** is flagged non-sidechain. Anything else — an empty or unreadable file, a
    transcript that never carries the key (older Claude Code builds), or a mixed file
    where a main-thread record appears — is reported as a real conversation. The failure
    mode is therefore always "a subagent transcript is still listed", never "a genuine
    conversation silently disappears".

    Bounded like :func:`_recorded_cwd`: the deciding non-sidechain record sits in the
    opening handful of lines in practice, so ``max_records`` is generous headroom rather
    than a tuned limit, and it keeps a huge transcript from being walked end to end.
    """
    saw_sidechain = False
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= max_records:
                    break
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                flag = record.get("isSidechain")
                if flag is False:
                    # A main-thread record proves this is not a pure subagent transcript.
                    return False
                if flag is True:
                    saw_sidechain = True
    except OSError:
        # Unreadable is unproven, and unproven means "show it" — see the docstring.
        return False
    return saw_sidechain


@dataclass(frozen=True)
class TranscriptSummary:
    """The transcript-listing fields both the Transcripts selector and the resume picker need.

    ``turn_count`` drives the picker's ``turn_count > 0`` filter; ``first_prompt`` (the first
    user turn, already truncated + redacted) labels the conversation; ``first_ts``/``last_ts``
    bound its "when · duration" display; ``is_subagent`` marks a sidechain transcript so the
    picker can leave it out (#1092). Derived from a full parse but far smaller than the turn
    list, so it caches per file (see :func:`read_transcript_summary`).
    """

    turn_count: int
    first_prompt: str
    first_ts: str
    last_ts: str
    is_subagent: bool = False


def _derive_transcript_summary(path: Path) -> TranscriptSummary:
    """Parse ``path`` and reduce it to a :class:`TranscriptSummary` (the cache-miss body)."""
    turns = read_transcript_turns(path)
    # First USER turn labels the conversation in the resume picker — truncated server-side so a
    # pasted wall of text can't bloat the listing payload.
    first_prompt = next(
        (t["content"] for t in turns if t.get("role") == "user" and t.get("content")),
        "",
    )[:120]
    return TranscriptSummary(
        turn_count=len(turns),
        first_prompt=first_prompt,
        first_ts=(turns[0].get("timestamp") or "") if turns else "",
        last_ts=(turns[-1].get("timestamp") or "") if turns else "",
        # Classified here so it rides the same per-file cache as the rest of the summary;
        # a bounded second pass, not a re-parse of the whole transcript.
        is_subagent=_is_subagent_transcript(path),
    )


def read_transcript_turns_from_offset(path: Path, offset: int) -> tuple[list[dict], int, bool]:
    """Read the redacted turns appended to a transcript since byte ``offset`` (live tail, #614).

    Returns ``(turns, new_offset, reset)``:

    - ``turns`` — the renderable turns in **file order** (oldest-first append
      order) parsed from the bytes after ``offset``, each already passed through
      :func:`redact.sanitize_line` via the shared :func:`_line_to_turn` (so the
      tail is redacted identically to the paged reader — never raw).
    - ``new_offset`` — the byte position the caller should poll from next: it
      advances **only past the last complete line** (the final newline). A
      partially-written trailing line is intentionally left unconsumed so the next
      poll reparses it once the bridge finishes writing it — a half-written JSON
      line never reaches the browser as a corrupt/empty turn.
    - ``reset`` — ``True`` when the file is now **shorter than** ``offset`` (the
      session was rotated/truncated/replaced): the read restarts from byte 0 and
      the caller should replace, not append. A negative ``offset`` is clamped to 0
      and is not itself a reset.

    Read in **binary** and decoded ``utf-8`` with ``errors="replace"`` (same
    tolerance as :func:`read_transcript_turns`) — invalid bytes from the external
    bridge never crash the tail. Blocking + pure; the caller runs it off the event
    loop. Raises :class:`FileNotFoundError` if the file can't be opened, matching
    :func:`read_transcript_turns` so the route maps both the same way.
    """
    start = max(int(offset), 0)
    try:
        fh = open(path, "rb")
    except (FileNotFoundError, OSError) as exc:
        raise FileNotFoundError(f"transcript not found: {path}") from exc
    with fh:
        size = fh.seek(0, 2)  # SEEK_END → current size in bytes
        reset = start > size  # the file shrank past our cursor → rotated/truncated
        if reset:
            start = 0
        fh.seek(start)
        chunk = fh.read()  # start..EOF
        # Consume only up to and including the last newline; a trailing partial
        # line (no newline yet) is left for the next poll so we never parse a
        # half-written record. With no newline at all, nothing is consumed.
        nl = chunk.rfind(b"\n")
        consumed = b"" if nl == -1 else chunk[: nl + 1]
        new_offset = start + len(consumed)
        text = consumed.decode("utf-8", errors="replace")
        turns = [t for line in text.splitlines() if (t := _line_to_turn(line)) is not None]
    return turns, new_offset, reset


def resolve_session_transcript(
    project_path: Path,
    session: str,
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> Path | None:
    """Resolve a session id to its transcript file *strictly inside* the project's own dirs.

    ``session`` is the transcript filename stem (the per-session uuid). This
    fails closed against path traversal: it rejects an empty stem, any path
    separator or ``..`` component, and confirms the resolved path's parent is one
    of the project's transcript directories before returning it — so a crafted
    ``session`` can never escape to read an arbitrary file. Returns ``None`` when
    the session is unsafe or no matching transcript exists (the caller maps that
    to a 404), never raising.

    The candidate directories are the project's own plus its worktree siblings — the
    same set :func:`transcript_paths_for` lists. They must stay in lockstep: listing a
    worktree conversation in the picker while resolving only the project root would make
    every worktree session 404 the moment it was selected (#1020). Widening the set does
    not weaken the gate — the parent-identity check is applied per directory, against an
    enumerated set derived from the project path, never from ``session``.
    """
    # Reject the obvious traversal shapes outright: empty stem, parent refs, separators,
    # NUL. A drive-qualified value ("C:evil") survives this — pathlib drops the left
    # operand when the right side carries a drive — and is caught by the parent-identity
    # check below, which is therefore load-bearing, not belt-and-braces.
    if (
        not session
        or session in (".", "..")
        or "/" in session
        or "\\" in session
        or "\x00" in session
    ):
        return None
    own = Path(claude_projects_dir) / sanitize_cwd(Path(project_path))
    for directory in [own, *_worktree_candidate_dirs(project_path, claude_projects_dir)]:
        transcript_dir = directory.resolve()
        candidate = (transcript_dir / f"{session}.jsonl").resolve()
        # Confirm the resolved file sits directly in the expected dir (parent identity),
        # so a drive-qualified stem, a symlink, or a surprise normalization can't smuggle
        # it elsewhere.
        if candidate.parent != transcript_dir:
            continue
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        # A worktree candidate is only a NAME match until its own recorded cwd proves it.
        # This is the ownership gate the pty resume path relies on, so it fails closed.
        if directory != own and not _transcript_is_owned(project_path, candidate):
            continue
        return candidate
    return None


def aggregate_project_usage(
    project_path: Path,
    *,
    project_name: str | None = None,
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> ProjectUsage:
    """Sum token usage across every transcript belonging to a project.

    Each transcript is streamed (never loaded whole); one that vanishes mid-walk
    (a racing session cleanup) is skipped rather than aborting the whole tally.
    """
    result = ProjectUsage(project=project_name or Path(project_path).name)
    for path in transcript_paths_for(project_path, claude_projects_dir):
        try:
            transcript = parse_transcript(path)
        except FileNotFoundError:
            continue
        for model, totals in transcript.by_model.items():
            result.by_model.setdefault(model, TokenTotals()).merge(totals)
        result.transcript_count += 1
    return result


def _transcript_dir_stamp(project_path: Path, claude_projects_dir: Path) -> tuple[int, int, int]:
    """Return a ``(file_count, total_size, max_mtime_ns)`` stamp of a project's transcripts.

    The usage cache's invalidation key — see the rationale at the top of this module for why
    all three components are needed (``st_mtime_ns`` rather than the float ``st_mtime`` also
    removes the rounding ambiguity that could mask a sub-second change).

    A file that vanishes between the listing and its ``stat`` (a racing session
    cleanup) is skipped — the next rollup re-stats and re-stamps, so a transient
    race never wedges the cache. An empty/absent directory stamps as ``(0, 0, -1)``.
    """
    paths = transcript_paths_for(project_path, claude_projects_dir)
    max_mtime_ns = -1
    total_size = 0
    counted = 0
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        counted += 1
        total_size += st.st_size
        if st.st_mtime_ns > max_mtime_ns:
            max_mtime_ns = st.st_mtime_ns
    return counted, total_size, max_mtime_ns


class _UsageCache:
    """Process-wide TTL + transcript-dir-stamp cache for :func:`aggregate_project_usage`.

    Invalidates a cached rollup when the TTL lapses **or** when the project's
    transcript directory stamp (``(file_count, total_size, max_mtime_ns)``) moves
    — i.e. a transcript is appended to, added, or removed. Thread-safe: the rollup is run
    from ``asyncio.to_thread`` worker threads. Returns a deep copy on every call
    so a caller that mutates the returned :class:`ProjectUsage` (its ``by_model``
    ``TokenTotals`` are mutable) never writes into the cached snapshot.
    """

    def __init__(self, ttl_seconds: float = USAGE_CACHE_TTL_SECONDS) -> None:
        """Create an empty cache with the given freshness window (seconds)."""
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # key -> (expires_at, stamp, rollup)
        self._entries: dict[tuple[str, str], tuple[float, tuple[int, int, int], ProjectUsage]] = {}

    def get(
        self,
        project_path: Path,
        *,
        project_name: str | None = None,
        claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
    ) -> ProjectUsage:
        """Return a cached rollup when fresh, else re-aggregate, cache, and return.

        Hands back a deep copy on every call so a mutating caller never touches the
        cached object. On a cache miss the public behavior of
        :func:`aggregate_project_usage` is preserved exactly: an ``OSError`` raised while
        *reading* a transcript propagates and is not cached, while a transcript that fails
        to *open* is skipped by the aggregator rather than surfaced.
        """
        key = (str(project_path), str(claude_projects_dir))
        stamp = _transcript_dir_stamp(project_path, claude_projects_dir)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            cached = (
                entry[2] if entry is not None and now < entry[0] and entry[1] == stamp else None
            )
        if cached is not None:
            # Deep-copy *outside* the lock: the stored rollup is never mutated (every
            # caller gets its own copy), so holding the single shared lock across the
            # copy would needlessly serialize the concurrent badge requests this cache
            # exists to speed up.
            return copy.deepcopy(cached)
        # Aggregate outside the lock — the per-line parse must not serialize
        # concurrent callers for *different* projects, and a duplicate parse on a
        # race is harmless. An OSError here propagates (matching the uncached path)
        # and is intentionally not cached.
        rollup = aggregate_project_usage(
            project_path,
            project_name=project_name,
            claude_projects_dir=claude_projects_dir,
        )
        with self._lock:
            self._entries[key] = (now + self._ttl, stamp, rollup)
        return copy.deepcopy(rollup)

    def clear(self) -> None:
        """Drop all cached entries (used by tests)."""
        with self._lock:
            self._entries.clear()


_USAGE_CACHE = _UsageCache()


def aggregate_project_usage_cached(
    project_path: Path,
    *,
    project_name: str | None = None,
    claude_projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> ProjectUsage:
    """Return :func:`aggregate_project_usage` through a TTL + dir-stamp cache.

    Use on the hot ``/api/projects/{name}/usage`` read path, where re-parsing every
    transcript line on every first-paint/reconnect fetch is the bottleneck. A repeat
    call within the TTL whose transcript directory is unchanged (same file count and
    max-mtime) skips the re-parse; any append/add/remove re-aggregates. The cache
    miss path is behavior-identical to :func:`aggregate_project_usage` (a returned
    rollup is a fresh deep copy; an ``OSError`` still propagates).
    """
    return _USAGE_CACHE.get(
        project_path,
        project_name=project_name,
        claude_projects_dir=claude_projects_dir,
    )


def invalidate_usage_cache() -> None:
    """Drop the usage cache so the next read re-aggregates (used by tests)."""
    _USAGE_CACHE.clear()


# Upper bound on the per-file transcript-summary cache. Without it, a path removed from disk
# would linger forever, so a long-running install with ongoing session churn accumulates an
# ever-growing set of summaries (#1058 review). A summary is tiny (4 fields), so the cap is
# generous; past it the least-recently-used entries are evicted and an evicted-then-reopened
# transcript simply re-parses once.
_TRANSCRIPT_SUMMARY_CACHE_MAX = 4096


class _TranscriptSummaryCache:
    """Process-wide, bounded per-file cache of a transcript's :class:`TranscriptSummary` (#1035).

    The Transcripts selector and the resume/fork picker share ``/api/projects/{name}/transcripts``,
    which re-parsed *every* ``.jsonl`` on every open just to derive turn_count + labels — O(total
    turns), a ~20 s stall on a large project. Cache the derived summary keyed on the file's
    ``(st_mtime_ns, st_size)`` stamp: an append moves the mtime **and** grows the file, so an
    unchanged transcript never re-parses (the same append-mostly reasoning as the #424 usage
    cache). No TTL is needed — the per-file stamp captures every change exactly. **LRU-bounded** to
    ``max_entries`` so churn of deleted transcripts can't grow it without limit (#1058).
    Thread-safe: the listing runs from an ``asyncio`` worker thread. Summaries are immutable.
    """

    def __init__(self, max_entries: int = _TRANSCRIPT_SUMMARY_CACHE_MAX) -> None:
        """Create an empty cache bounded to ``max_entries`` (least-recently-used eviction)."""
        self._lock = threading.Lock()
        self._max = max_entries
        # str(path) -> ((st_mtime_ns, st_size), summary); ordered least- to most-recently-used.
        self._entries: OrderedDict[str, tuple[tuple[int, int], TranscriptSummary]] = OrderedDict()

    def get(self, path: Path) -> TranscriptSummary:
        """Return the cached summary for ``path`` when its stamp is unchanged, else re-derive.

        ``path.stat()`` (and thus a ``FileNotFoundError`` for a session removed mid-walk) is
        raised exactly as :func:`read_transcript_turns` would, so callers skip a racing removal.
        """
        st = path.stat()
        key = str(path)
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] == stamp:
                self._entries.move_to_end(key)  # mark most-recently-used
                return entry[1]
        # Derive outside the lock: the per-line parse must not serialize concurrent listings for
        # different projects, and a duplicate parse on a race is harmless.
        summary = _derive_transcript_summary(path)
        with self._lock:
            self._entries[key] = (stamp, summary)
            self._entries.move_to_end(key)
            # Evict least-recently-used entries so deleted-transcript churn stays bounded (#1058).
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return summary

    def clear(self) -> None:
        """Drop all cached entries (used by tests)."""
        with self._lock:
            self._entries.clear()


_TRANSCRIPT_SUMMARY_CACHE = _TranscriptSummaryCache()


def read_transcript_summary(path: Path) -> TranscriptSummary:
    """Return ``path``'s :class:`TranscriptSummary` via a per-file ``(mtime, size)`` cache (#1035).

    Use on the transcript-listing path in place of a full :func:`read_transcript_turns` + manual
    field derivation: a repeat open of an unchanged transcript skips the re-parse entirely. The
    cache-miss path is behavior-identical to deriving the fields inline (a ``FileNotFoundError``
    for a vanished file still propagates).
    """
    return _TRANSCRIPT_SUMMARY_CACHE.get(path)


def invalidate_transcript_summary_cache() -> None:
    """Drop the transcript-summary cache so the next listing re-derives (used by tests)."""
    _TRANSCRIPT_SUMMARY_CACHE.clear()
