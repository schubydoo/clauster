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

import json
from dataclasses import dataclass, field
from pathlib import Path

from .pointers import CLAUDE_PROJECTS_DIR, sanitize_cwd


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


@dataclass
class TokenTotals:
    input: int = 0
    output: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    messages: int = 0

    def add_usage(self, usage: dict) -> None:
        self.input += int(usage.get("input_tokens", 0) or 0)
        self.output += int(usage.get("output_tokens", 0) or 0)
        self.cache_creation += int(usage.get("cache_creation_input_tokens", 0) or 0)
        self.cache_read += int(usage.get("cache_read_input_tokens", 0) or 0)
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
        return self.input + self.output + self.cache_creation + self.cache_read


def _price_for(model: str, prices: dict[str, ModelPrice]) -> ModelPrice | None:
    low = model.lower()
    for family, price in prices.items():
        if family in low:
            return price
    return None


def cost_usd(model: str, totals: TokenTotals, prices: dict[str, ModelPrice] = PRICES) -> float | None:
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
        fh = open(path, encoding="utf-8")
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
