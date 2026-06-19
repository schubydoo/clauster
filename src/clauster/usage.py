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
from dataclasses import dataclass, field
from pathlib import Path

from .pointers import CLAUDE_PROJECTS_DIR, sanitize_cwd

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
        agg = TokenTotals()
        for t in self.by_model.values():
            agg.merge(t)
        return agg

    def cost_usd(self, prices: dict[str, ModelPrice] = PRICES) -> float:
        """Total approx USD across known models (unpriced models contribute 0)."""
        return sum((cost_usd(m, t, prices) or 0.0) for m, t in self.by_model.items())

    def unpriced_models(self, prices: dict[str, ModelPrice] = PRICES) -> list[str]:
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


def parse_transcript(path: Path) -> TranscriptUsage:
    """Aggregate token usage from a transcript JSONL, grouped by model.

    Tolerant: blank lines and malformed records are skipped; only assistant
    messages carrying a ``usage`` block contribute. The transcript can be huge, so
    it is streamed line by line (never loaded whole).
    """
    result = TranscriptUsage(path=Path(path))
    try:
        # errors="replace": transcripts are written by the external claude bridge
        # and can carry invalid UTF-8. Without this, a bad byte raises
        # UnicodeDecodeError mid-iteration (not a JSONDecodeError, so the per-line
        # guard below misses it), aborting the whole project tally. A replaced
        # char either parses fine or trips the JSONDecodeError skip — never crashes.
        fh = open(path, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError) as exc:
        raise FileNotFoundError(f"transcript not found: {path}") from exc
    with fh:
        for line in fh:
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
    by ``-`` (the same mapping the bridge-pointer walk uses). Returns ``[]`` if that
    directory is absent or unreadable.
    """
    transcript_dir = Path(claude_projects_dir) / sanitize_cwd(Path(project_path))
    try:
        return sorted(p for p in transcript_dir.glob("*.jsonl") if p.is_file())
    except OSError:
        return []


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

    Used to invalidate the usage cache: an appended transcript line moves the
    file's mtime *and* grows it, a new or removed session changes ``file_count``,
    and the aggregate ``total_size`` catches an append whose mtime lands in the
    same coarse filesystem tick as the cached stamp (an append always grows the
    file, even when the second-resolution mtime does not visibly advance). Using
    ``st_mtime_ns`` (integer nanoseconds) rather than the float ``st_mtime`` also
    removes the rounding ambiguity that could mask a sub-second change. Together
    these make a stale token/cost rollup structurally impossible to serve past a
    transcript write while staying a cheap stat-only probe.

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
        :func:`aggregate_project_usage` is preserved exactly (including a propagated
        ``OSError`` from an unreadable transcript file).
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
