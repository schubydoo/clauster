"""Cost / token tracking from transcript JSONL (v0.3). Fully offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clauster import __main__ as cli
from clauster.pointers import sanitize_cwd
from clauster.usage import (
    PRICES,
    ModelPrice,
    ProjectUsage,
    TokenTotals,
    aggregate_project_usage,
    aggregate_project_usage_cached,
    cost_usd,
    invalidate_usage_cache,
    parse_transcript,
    transcript_paths_for,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transcripts" / "test1-session.jsonl"


def _transcript(tmp_path, records) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _assistant(model, **usage):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "model": model, "usage": usage},
    }


# ----- TokenTotals / cost ----------------------------------------------


def test_token_totals_accumulate():
    t = TokenTotals()
    t.add_usage(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 200,
        }
    )
    t.add_usage({"input_tokens": 1, "output_tokens": 2})
    assert (t.input, t.output, t.cache_creation, t.cache_read, t.messages) == (
        11,
        7,
        100,
        200,
        2,
    )
    assert t.total_tokens == 11 + 7 + 100 + 200


def test_add_usage_tolerates_non_numeric_token_values():
    # A malformed token value must contribute 0, not raise — one bad transcript line
    # must never abort the whole rollup (it would 500 the usage endpoint).
    t = TokenTotals()
    t.add_usage(
        {
            "input_tokens": "abc",  # non-numeric string
            "output_tokens": [1, 2],  # structured
            "cache_creation_input_tokens": None,  # null
            "cache_read_input_tokens": "42",  # numeric string → coerced
        }
    )
    assert (t.input, t.output, t.cache_creation, t.cache_read, t.messages) == (0, 0, 0, 42, 1)


def test_add_usage_tolerates_non_finite_floats():
    # json.loads decodes bare NaN/Infinity tokens to these floats; int() raises
    # ValueError/OverflowError on them, so they must coerce to 0 rather than abort.
    t = TokenTotals()
    t.add_usage({"input_tokens": float("inf"), "output_tokens": float("nan")})
    assert (t.input, t.output, t.messages) == (0, 0, 1)


def test_add_usage_handles_huge_integer_without_overflow():
    # JSON integers are unbounded; one larger than a C double would OverflowError if
    # routed through math.isfinite. A real int must pass through at full precision.
    huge = 10**400
    t = TokenTotals()
    t.add_usage({"input_tokens": huge, "output_tokens": 5})
    assert t.input == huge and t.output == 5 and t.messages == 1


def test_cost_usd_opus_exact():
    t = TokenTotals(input=6, output=13, cache_creation=11715, cache_read=17228)
    # 6*15 + 13*75 + 11715*18.75 + 17228*1.5, per Mtok
    expected = (6 * 15 + 13 * 75 + 11715 * 18.75 + 17228 * 1.5) / 1_000_000
    assert cost_usd("claude-opus-4-7", t) == pytest.approx(expected)


@pytest.mark.parametrize(
    "model,family",
    [
        ("claude-opus-4-8", "opus"),
        ("claude-sonnet-4-6", "sonnet"),
        ("claude-haiku-4-5", "haiku"),
    ],
)
def test_cost_matches_family(model, family):
    t = TokenTotals(input=1_000_000)  # 1 Mtok input
    assert cost_usd(model, t) == pytest.approx(PRICES[family].input)


def test_cost_unknown_model_is_none():
    assert cost_usd("gpt-9", TokenTotals(input=100)) is None


def test_custom_prices():
    prices = {"opus": ModelPrice(input=1.0, output=1.0, cache_write=1.0, cache_read=1.0)}
    t = TokenTotals(input=1_000_000)
    assert cost_usd("claude-opus-4-8", t, prices) == pytest.approx(1.0)


# ----- parse_transcript ------------------------------------------------


def test_parse_fixture():
    u = parse_transcript(FIXTURE)
    assert "claude-opus-4-7" in u.by_model
    t = u.by_model["claude-opus-4-7"]
    assert (t.input, t.output, t.cache_creation, t.cache_read) == (6, 13, 11715, 17228)
    assert u.cost_usd() > 0


def test_parse_multi_model_and_totals(tmp_path):
    p = _transcript(
        tmp_path,
        [
            _assistant("claude-opus-4-8", input_tokens=10, output_tokens=20),
            _assistant("claude-sonnet-4-6", input_tokens=100, output_tokens=200),
            {"type": "user", "message": {"role": "user"}},  # no usage -> ignored
        ],
    )
    u = parse_transcript(p)
    assert set(u.by_model) == {"claude-opus-4-8", "claude-sonnet-4-6"}
    assert u.totals.input == 110 and u.totals.output == 220 and u.totals.messages == 2


def test_parse_tolerates_blank_and_corrupt_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps(_assistant("claude-opus-4-8", input_tokens=5)) + "\n"
        "\n"  # blank
        "{not json\n"  # corrupt
         + json.dumps({"type": "attachment"}) + "\n"  # no message
    )
    u = parse_transcript(p)
    assert u.totals.input == 5 and u.totals.messages == 1


def test_parse_tolerates_malformed_token_values(tmp_path):
    # A structurally-valid line whose usage holds a non-numeric ("oops") or non-finite
    # (Infinity — json.loads accepts the bare token) value must be tolerated (counted
    # with that field as 0), not abort the tally with an int()/OverflowError.
    p = _transcript(
        tmp_path,
        [
            _assistant("claude-opus-4-8", input_tokens="oops", output_tokens=5),
            _assistant("claude-opus-4-8", input_tokens=float("inf"), output_tokens=5),
            _assistant("claude-opus-4-8", input_tokens=10, output_tokens=20),
        ],
    )
    u = parse_transcript(p)
    totals = u.by_model["claude-opus-4-8"]
    assert totals.input == 10  # "oops" and Infinity contributed 0 input; the valid line 10
    assert totals.output == 30  # 5 + 5 + 20
    assert totals.messages == 3  # all three lines counted


def test_parse_tolerates_invalid_utf8(tmp_path):
    # Transcripts are written by the external claude bridge and can contain
    # invalid UTF-8. A bad byte must not crash the tally (regression: it used to
    # raise an uncaught UnicodeDecodeError mid-iteration, aborting the whole walk).
    p = tmp_path / "t.jsonl"
    good = json.dumps(_assistant("claude-opus-4-8", input_tokens=7)).encode("utf-8")
    p.write_bytes(b"\xff\xfe garbage line\n" + good + b"\n")
    u = parse_transcript(p)
    assert u.totals.input == 7 and u.totals.messages == 1


def test_parse_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_transcript(tmp_path / "nope.jsonl")


def test_unpriced_models_reported(tmp_path):
    p = _transcript(
        tmp_path,
        [
            _assistant("gpt-9", input_tokens=1),
            _assistant("claude-opus-4-8", input_tokens=1),
        ],
    )
    u = parse_transcript(p)
    assert u.unpriced_models() == ["gpt-9"]
    # unpriced model contributes 0 to the total, doesn't crash
    assert u.cost_usd() >= 0


# ----- CLI -------------------------------------------------------------


def test_cli_usage_fixture(capsys):
    assert cli.main(["usage", str(FIXTURE)]) == 0
    err = capsys.readouterr().err
    assert "claude-opus-4-7" in err and "tokens" in err


def test_cli_usage_missing_file(tmp_path):
    assert cli.main(["usage", str(tmp_path / "nope.jsonl")]) == 2


# ----- TokenTotals.merge -----------------------------------------------


def test_token_totals_merge():
    a = TokenTotals(input=1, output=2, cache_creation=3, cache_read=4, messages=1)
    b = TokenTotals(input=10, output=20, cache_creation=30, cache_read=40, messages=2)
    a.merge(b)
    assert (a.input, a.output, a.cache_creation, a.cache_read, a.messages) == (
        11,
        22,
        33,
        44,
        3,
    )


# ----- per-project discovery + aggregation -----------------------------


def _project_transcript_dir(claude_projects_dir: Path, project_path: Path) -> Path:
    """Build the dir Claude would use for a project's transcripts, and create it."""
    d = claude_projects_dir / sanitize_cwd(project_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_transcript_paths_for_finds_jsonl(tmp_path):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "a.jsonl").write_text("")
    (d / "b.jsonl").write_text("")
    (d / "notes.txt").write_text("ignore me")  # non-jsonl ignored
    paths = transcript_paths_for(project, claude_dir)
    assert [p.name for p in paths] == ["a.jsonl", "b.jsonl"]  # sorted


def test_transcript_paths_for_missing_dir_is_empty(tmp_path):
    # No transcript dir for this project -> [] rather than raising.
    assert (
        transcript_paths_for(Path("/srv/projects/never_run"), tmp_path / "claude_projects") == []
    )


def test_aggregate_project_usage_sums_across_transcripts(tmp_path):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "s1.jsonl").write_text(
        json.dumps(_assistant("claude-opus-4-8", input_tokens=10, output_tokens=20)) + "\n"
    )
    (d / "s2.jsonl").write_text(
        json.dumps(_assistant("claude-opus-4-8", input_tokens=5, output_tokens=1))
        + "\n"
        + json.dumps(_assistant("claude-sonnet-4-6", input_tokens=100, output_tokens=200))
        + "\n"
    )
    pu = aggregate_project_usage(project, project_name="my_proj", claude_projects_dir=claude_dir)
    assert isinstance(pu, ProjectUsage)
    assert pu.project == "my_proj"
    assert pu.transcript_count == 2
    assert set(pu.by_model) == {"claude-opus-4-8", "claude-sonnet-4-6"}
    # opus input merged across both transcripts; sonnet only in s2
    assert pu.by_model["claude-opus-4-8"].input == 15
    assert pu.by_model["claude-sonnet-4-6"].input == 100
    assert pu.totals.input == 115 and pu.totals.messages == 3
    assert pu.cost_usd() > 0


def test_aggregate_project_usage_no_transcripts_is_zero(tmp_path):
    pu = aggregate_project_usage(
        Path("/srv/projects/never_run"),
        claude_projects_dir=tmp_path / "claude_projects",
    )
    assert pu.transcript_count == 0
    assert pu.by_model == {}
    assert pu.cost_usd() == 0.0
    assert pu.totals.total_tokens == 0


def test_aggregate_project_usage_defaults_name_to_basename(tmp_path):
    pu = aggregate_project_usage(
        Path("/srv/projects/widget"), claude_projects_dir=tmp_path / "claude_projects"
    )
    assert pu.project == "widget"


def test_transcript_paths_for_oserror_is_empty(tmp_path, monkeypatch):
    # If the filesystem raises while globbing (e.g. a permission error), swallow it
    # and return [] rather than crashing the dashboard badge.
    def _boom(self, pattern):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "glob", _boom)
    assert transcript_paths_for(Path("/srv/projects/my_proj"), tmp_path / "claude_projects") == []


def test_aggregate_skips_transcript_deleted_mid_walk(tmp_path, monkeypatch):
    # A transcript listed by the walk vanishes before parse -> skipped, not fatal.
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "gone.jsonl").write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=1)) + "\n")

    import clauster.usage as usage_mod

    def _raise(_path):
        raise FileNotFoundError("raced deletion")

    monkeypatch.setattr(usage_mod, "parse_transcript", _raise)
    pu = aggregate_project_usage(project, claude_projects_dir=claude_dir)
    assert pu.transcript_count == 0 and pu.by_model == {}


# ----- aggregate_project_usage_cached ----------------------------------
# The cache is process-wide, so every cache test clears it around itself.


@pytest.fixture
def _clean_usage_cache():
    """Drop the process-wide usage cache before and after a cache test."""
    invalidate_usage_cache()
    yield
    invalidate_usage_cache()


def _count_parses(monkeypatch) -> list[int]:
    """Wrap usage.parse_transcript with a call counter; returns a 1-slot tally list."""
    import clauster.usage as usage_mod

    calls = [0]
    real = usage_mod.parse_transcript

    def _counting(path):
        calls[0] += 1
        return real(path)

    monkeypatch.setattr(usage_mod, "parse_transcript", _counting)
    return calls


def test_cached_hit_skips_reparse(tmp_path, monkeypatch, _clean_usage_cache):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "s1.jsonl").write_text(
        json.dumps(_assistant("claude-opus-4-8", input_tokens=10, output_tokens=20)) + "\n"
    )
    calls = _count_parses(monkeypatch)

    first = aggregate_project_usage_cached(
        project, project_name="my_proj", claude_projects_dir=claude_dir
    )
    second = aggregate_project_usage_cached(
        project, project_name="my_proj", claude_projects_dir=claude_dir
    )
    # The second call is served from cache: the transcript is not re-parsed.
    assert calls[0] == 1
    # ...and the result is identical to the uncached aggregate.
    uncached = aggregate_project_usage(
        project, project_name="my_proj", claude_projects_dir=claude_dir
    )
    assert first.transcript_count == second.transcript_count == uncached.transcript_count == 1
    assert second.totals.input == uncached.totals.input == 10
    assert second.cost_usd() == uncached.cost_usd()


def test_cached_invalidates_on_append(tmp_path, monkeypatch, _clean_usage_cache):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    f = d / "s1.jsonl"
    f.write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=10)) + "\n")
    calls = _count_parses(monkeypatch)

    first = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert first.totals.input == 10
    assert calls[0] == 1

    # Append a line and bump the mtime forward so the max-mtime stamp moves even on
    # a coarse-resolution clock; the next call must re-parse, not serve stale.
    with f.open("a") as fh:
        fh.write(json.dumps(_assistant("claude-opus-4-8", input_tokens=5)) + "\n")
    bumped = f.stat().st_mtime + 10
    import os

    os.utime(f, (bumped, bumped))

    second = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert calls[0] == 2  # re-parsed because the dir stamp moved
    assert second.totals.input == 15


def test_cached_invalidates_on_same_mtime_append(tmp_path, monkeypatch, _clean_usage_cache):
    # The coarse-mtime case the cache must survive: an append whose mtime lands in
    # the same filesystem tick as the cached stamp. The total_size component of the
    # stamp catches it (an append always grows the file) even when max_mtime_ns is
    # pinned, so a stale cost/token rollup is never served.
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    f = d / "s1.jsonl"
    f.write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=10)) + "\n")
    frozen = f.stat().st_mtime
    calls = _count_parses(monkeypatch)

    first = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert first.totals.input == 10 and calls[0] == 1

    with f.open("a") as fh:
        fh.write(json.dumps(_assistant("claude-opus-4-8", input_tokens=5)) + "\n")
    import os

    # Pin the mtime back to the original so ONLY the size differs.
    os.utime(f, (frozen, frozen))

    second = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert calls[0] == 2  # re-parsed because total_size moved despite the frozen mtime
    assert second.totals.input == 15


def test_cached_invalidates_on_new_transcript(tmp_path, monkeypatch, _clean_usage_cache):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "s1.jsonl").write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=10)) + "\n")
    calls = _count_parses(monkeypatch)

    first = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert first.transcript_count == 1
    assert calls[0] == 1

    # A new session file changes the file count -> cache invalidates, re-parses both.
    (d / "s2.jsonl").write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=7)) + "\n")
    second = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert second.transcript_count == 2
    assert second.totals.input == 17
    assert calls[0] == 3  # 1 (first) + 2 (both re-parsed on the count change)


def test_cached_invalidates_on_removed_transcript(tmp_path, monkeypatch, _clean_usage_cache):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "s1.jsonl").write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=10)) + "\n")
    (d / "s2.jsonl").write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=7)) + "\n")
    calls = _count_parses(monkeypatch)

    first = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert first.transcript_count == 2 and first.totals.input == 17

    (d / "s2.jsonl").unlink()
    second = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    # File count dropped -> re-parsed; stale 17 must not be served.
    assert second.transcript_count == 1 and second.totals.input == 10
    assert calls[0] == 3  # 2 (first) + 1 (single remaining file re-parsed)


def test_cached_ttl_expiry_reparses(tmp_path, monkeypatch, _clean_usage_cache):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "s1.jsonl").write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=10)) + "\n")
    calls = _count_parses(monkeypatch)

    import clauster.usage as usage_mod

    # Drive time forward past the TTL with the dir stamp unchanged; the entry should
    # expire and re-aggregate even though nothing on disk moved.
    fake_now = [1000.0]
    monkeypatch.setattr(usage_mod.time, "monotonic", lambda: fake_now[0])

    aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert calls[0] == 1
    fake_now[0] += usage_mod.USAGE_CACHE_TTL_SECONDS + 1.0
    aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert calls[0] == 2  # TTL lapsed -> re-parsed


def test_cached_returns_independent_copy(tmp_path, monkeypatch, _clean_usage_cache):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "s1.jsonl").write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=10)) + "\n")
    calls = _count_parses(monkeypatch)

    first = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    # Mutate the returned rollup's nested totals; the cache must not be poisoned.
    first.by_model["claude-opus-4-8"].input = 99999
    first.transcript_count = 42

    second = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert calls[0] == 1  # still a cache hit (deep copy, not the cached object)
    assert second.by_model["claude-opus-4-8"].input == 10
    assert second.transcript_count == 1


def test_cached_oserror_propagates_and_is_not_cached(tmp_path, monkeypatch, _clean_usage_cache):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "s1.jsonl").write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=10)) + "\n")

    import clauster.usage as usage_mod

    def _boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(usage_mod, "parse_transcript", _boom)
    # OSError from an unreadable transcript propagates on a miss, matching the
    # uncached path (the app layer maps it to a 503), and is not cached.
    with pytest.raises(OSError):
        aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)

    # Once the read recovers, the next call succeeds (the failure left no cache entry).
    monkeypatch.setattr(usage_mod, "parse_transcript", parse_transcript)
    ok = aggregate_project_usage_cached(project, claude_projects_dir=claude_dir)
    assert ok.totals.input == 10


def test_cached_stamp_skips_unstattable_file(tmp_path, monkeypatch, _clean_usage_cache):
    # A transcript listed by transcript_paths_for whose explicit stat then fails (a
    # racing cleanup between the listing and the stamp) is skipped in the stamp
    # rather than aborting. Patch only the stamp's stat so the path is still listed.
    import clauster.usage as usage_mod

    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    f = d / "s1.jsonl"
    f.write_text(json.dumps(_assistant("claude-opus-4-8", input_tokens=10)) + "\n")

    monkeypatch.setattr(usage_mod, "transcript_paths_for", lambda *a, **k: [f])

    def _boom_stat(self, *args, **kwargs):
        raise OSError("vanished")

    monkeypatch.setattr(Path, "stat", _boom_stat)
    # The stamp degrades to (0, 0, -1) for the unstattable file (no crash).
    assert usage_mod._transcript_dir_stamp(project, claude_dir) == (0, 0, -1)
