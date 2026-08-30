"""Cost / token tracking from transcript JSONL (v0.3). Fully offline."""

from __future__ import annotations

import json
import shutil
import tempfile
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
    invalidate_transcript_summary_cache,
    invalidate_usage_cache,
    parse_transcript,
    read_transcript_summary,
    read_transcript_turns,
    read_transcript_turns_from_offset,
    resolve_session_transcript,
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


@pytest.fixture
def short_tmp_root():
    """A SHORT root for tests that need REAL project directories on disk.

    ``sanitize_cwd`` collapses an absolute path into a single directory NAME, so a
    transcript directory is about as long as the project path itself — the full path is
    effectively doubled. pytest's ``tmp_path`` already carries a
    ``popen-gwN/<test-name>`` segment under xdist (the default here), and the doubled
    length then exceeds Windows' 260-char MAX_PATH: ``mkdir`` fails with
    ``FileNotFoundError: [WinError 206] The filename or extension is too long``. It
    reproduces ONLY under xdist, so a serial run of the same test passes — caught by the
    3-OS matrix, not locally.

    Most tests here pass a fake path (``/srv/projects/...``) that is never created and are
    unaffected; this is for the ones that must enumerate real sibling directories.
    """
    root = Path(tempfile.mkdtemp())
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


def _write_transcript(directory: Path, name: str, cwd: Path | str | None, **extra) -> Path:
    """Write a transcript that records ``cwd``, the way Claude does.

    Ownership of a worktree transcript is proven from this recorded cwd, not from the
    containing directory name (which is a lossy one-way hash of it), so a fixture that
    omits it is correctly refused as unproven — pass ``cwd=None`` to exercise that.
    """
    record: dict = {"type": "user", "message": {"role": "user"}, **extra}
    if cwd is not None:
        record["cwd"] = str(cwd)
    path = directory / name
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def test_recorded_cwd_skips_records_without_one(short_tmp_root):
    # Leading records are often queue-operation/attachment entries carrying no cwd, and a
    # corrupt line can appear anywhere, so the scan must step over both rather than give up
    # at the first record. An explicitly EMPTY cwd is not a cwd either.
    from clauster.usage import _recorded_cwd

    path = short_tmp_root / "t.jsonl"
    path.write_text(
        json.dumps({"type": "queue-operation"}) + "\n"
        "{not json\n"
        + json.dumps({"type": "user", "cwd": ""})
        + "\n"
        + json.dumps({"type": "user", "cwd": "/srv/projects/found"})
        + "\n",
        encoding="utf-8",
    )
    assert _recorded_cwd(path) == "/srv/projects/found"


def test_recorded_cwd_gives_up_past_the_line_bound(short_tmp_root):
    # Bounded scan: a transcript whose cwd only appears beyond the cap reads as unproven
    # rather than making the walk read an arbitrarily large file.
    from clauster.usage import _recorded_cwd

    path = short_tmp_root / "late.jsonl"
    filler = json.dumps({"type": "attachment"}) + "\n"
    path.write_text(
        filler * 20 + json.dumps({"type": "user", "cwd": "/srv/projects/late"}) + "\n",
        encoding="utf-8",
    )
    assert _recorded_cwd(path, max_lines=5) is None
    assert _recorded_cwd(path, max_lines=50) == "/srv/projects/late"


def test_recorded_cwd_unreadable_transcript_is_unproven(short_tmp_root):
    # stat/open failures must read as "unproven" (-> refused downstream), never raise into
    # a listing. A directory stands in for the unreadable file: opening one raises OSError
    # on every platform (IsADirectoryError on POSIX, PermissionError on Windows).
    from clauster.usage import _recorded_cwd

    assert _recorded_cwd(short_tmp_root / "does-not-exist.jsonl") is None
    directory = short_tmp_root / "not-a-file.jsonl"
    directory.mkdir()
    assert _recorded_cwd(directory) is None


def test_recorded_cwd_cache_clears_at_the_cap(short_tmp_root, monkeypatch):
    # The cache is cleared wholesale at the cap rather than evicted per-entry; pin that it
    # actually bounds, so a long-lived process can't grow it without limit.
    from clauster import usage as usage_mod

    monkeypatch.setattr(usage_mod, "_CWD_CACHE_MAX", 1)
    usage_mod._CWD_CACHE.clear()
    for i in range(3):
        p = short_tmp_root / f"c{i}.jsonl"
        p.write_text(json.dumps({"type": "user", "cwd": f"/srv/p{i}"}) + "\n", encoding="utf-8")
        assert usage_mod._recorded_cwd(p) == f"/srv/p{i}"
    assert len(usage_mod._CWD_CACHE) <= 1
    usage_mod._CWD_CACHE.clear()


def test_transcript_is_owned_rejects_an_unresolvable_cwd(short_tmp_root):
    # A recorded cwd is untrusted input from a file on disk. One that cannot even be
    # resolved (a NUL byte makes Path.resolve raise ValueError) must fail closed, not
    # propagate out of a listing.
    from clauster.usage import _transcript_is_owned

    project = short_tmp_root / "projects" / "my_proj"
    project.mkdir(parents=True)
    path = short_tmp_root / "bad.jsonl"
    path.write_text(json.dumps({"type": "user", "cwd": "/srv/\x00/x"}) + "\n", encoding="utf-8")
    assert _transcript_is_owned(project, path) is False


def test_transcript_paths_for_tolerates_an_unreadable_candidate_dir(short_tmp_root, monkeypatch):
    # An unreadable worktree candidate dir contributes nothing rather than failing the whole
    # walk — the project's own conversations must still list.
    claude_dir = short_tmp_root / "claude_projects"
    project = short_tmp_root / "projects" / "my_proj"
    worktree = project / ".claude" / "worktrees" / "sess-1"
    project.mkdir(parents=True)
    _write_transcript(_project_transcript_dir(claude_dir, project), "main.jsonl", project)
    candidate = _project_transcript_dir(claude_dir, worktree)
    _write_transcript(candidate, "wt.jsonl", worktree)

    real_glob = Path.glob

    def boom(self, pattern):
        if self == candidate:
            raise OSError("permission denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", boom)
    assert [p.name for p in transcript_paths_for(project, claude_dir)] == ["main.jsonl"]


def test_transcript_paths_for_includes_worktree_sessions(short_tmp_root):
    # #1020: a worktree-spawn session runs with the worktree as its cwd, so Claude files its
    # transcript in a SIBLING directory. Keying only on the project root hid those
    # conversations from the Conversation picker and dropped their tokens from the rollup.
    claude_dir = short_tmp_root / "claude_projects"
    project = short_tmp_root / "projects" / "my_proj"
    worktree = project / ".claude" / "worktrees" / "sess-abc123"
    project.mkdir(parents=True)
    _write_transcript(_project_transcript_dir(claude_dir, project), "main.jsonl", project)
    _write_transcript(_project_transcript_dir(claude_dir, worktree), "worktree.jsonl", worktree)

    names = [p.name for p in transcript_paths_for(project, claude_dir)]
    assert sorted(names) == ["main.jsonl", "worktree.jsonl"]


def test_worktree_transcript_without_a_recorded_cwd_is_refused(short_tmp_root):
    # Fail closed on UNPROVEN. A candidate dir is only a lossy name match, so a transcript
    # that never states its cwd cannot be shown to belong here — and this feeds the pty
    # resume ownership gate, so "can't tell" must mean "no".
    claude_dir = short_tmp_root / "claude_projects"
    project = short_tmp_root / "projects" / "my_proj"
    worktree = project / ".claude" / "worktrees" / "sess-1"
    project.mkdir(parents=True)
    _write_transcript(_project_transcript_dir(claude_dir, project), "main.jsonl", project)
    _write_transcript(_project_transcript_dir(claude_dir, worktree), "mystery.jsonl", None)

    assert [p.name for p in transcript_paths_for(project, claude_dir)] == ["main.jsonl"]
    assert resolve_session_transcript(project, "mystery", claude_dir) is None


@pytest.mark.parametrize(
    "foreign",
    [
        # A sibling PROJECT whose name sanitizes into this project's worktree prefix:
        # sanitize_cwd maps `/`, `.`, `-` and `_` all to `-`, and is_valid_project_name
        # admits both spellings.
        "projects/my_proj--claude-worktrees-x",
        "projects/my_proj__claude_worktrees_y",
        # Greptile P1 (security): the colliding family is NOT limited to the projects root.
        # `<root>/projects-my_proj--claude-worktrees-z` sanitizes IDENTICALLY to
        # `<root>/projects/my_proj/.claude/worktrees/z`, yet is not a sibling of the project
        # at all — so no enumeration of neighbours can exclude it. Ownership has to be proven
        # from the transcript's own recorded cwd instead.
        "projects-my_proj--claude-worktrees-z",
    ],
)
def test_transcript_paths_for_excludes_foreign_collisions(short_tmp_root, foreign):
    project = short_tmp_root / "projects" / "my_proj"
    project.mkdir(parents=True)
    foreign_path = short_tmp_root / foreign
    foreign_path.mkdir(parents=True, exist_ok=True)

    claude_dir = short_tmp_root / "claude_projects"
    _write_transcript(_project_transcript_dir(claude_dir, project), "mine.jsonl", project)
    # The foreign transcript honestly records ITS OWN cwd — which is exactly what proves it
    # is not ours, even though its directory name is indistinguishable from one of ours.
    _write_transcript(
        _project_transcript_dir(claude_dir, foreign_path), "theirs.jsonl", foreign_path
    )

    assert [p.name for p in transcript_paths_for(project, claude_dir)] == ["mine.jsonl"]
    # The ownership proof behind pty resume must refuse it too, not just the listing.
    assert resolve_session_transcript(project, "theirs", claude_dir) is None


def test_resolve_session_transcript_finds_worktree_session(short_tmp_root):
    # Cross-layer: the picker lists worktree conversations, so the resolver behind
    # selecting one must find them too. Listing without resolving would 404 every worktree
    # session the moment it was clicked — the two sides must stay in lockstep.
    claude_dir = short_tmp_root / "claude_projects"
    project = short_tmp_root / "projects" / "my_proj"
    worktree = project / ".claude" / "worktrees" / "sess-1"
    project.mkdir(parents=True)
    _project_transcript_dir(claude_dir, project)
    _write_transcript(_project_transcript_dir(claude_dir, worktree), "abc-123.jsonl", worktree)
    resolved = resolve_session_transcript(project, "abc-123", claude_dir)
    assert resolved is not None and resolved.name == "abc-123.jsonl"


def test_resolve_session_transcript_still_rejects_traversal(tmp_path):
    # Widening the candidate DIRECTORIES must not widen what a crafted `session` can reach:
    # the stem checks and the per-directory parent-identity check both still apply.
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    _project_transcript_dir(claude_dir, project)
    outsider = _project_transcript_dir(claude_dir, Path("/srv/projects/other"))
    (outsider / "secret.jsonl").write_text("")
    for evil in ("../" + sanitize_cwd(Path("/srv/projects/other")) + "/secret", "..", "", "a/b"):
        assert resolve_session_transcript(project, evil, claude_dir) is None


def test_project_usage_counts_worktree_transcripts(short_tmp_root):
    # The rollup shares transcript_paths_for, so worktree sessions must now be counted —
    # their tokens were previously invisible in the project's usage totals.
    claude_dir = short_tmp_root / "claude_projects"
    project = short_tmp_root / "projects" / "my_proj"
    project.mkdir(parents=True)
    _project_transcript_dir(claude_dir, project)
    worktree = project / ".claude" / "worktrees" / "sess-1"
    _write_transcript(
        _project_transcript_dir(claude_dir, worktree),
        "s.jsonl",
        worktree,
        type="assistant",
        message={"model": "claude-opus-4", "usage": {"input_tokens": 10, "output_tokens": 5}},
    )
    result = aggregate_project_usage(project, claude_projects_dir=claude_dir)
    assert result.transcript_count == 1
    assert result.by_model["claude-opus-4"].input == 10


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


def test_stamp_keeps_max_mtime_when_later_file_is_older(tmp_path):
    # max_mtime_ns must be the newest file's mtime even when a later-listed (sorted)
    # transcript is older — exercises the branch where st.st_mtime_ns does NOT exceed
    # the running max, so a stale-but-larger dir can't fake a fresh stamp.
    import os

    import clauster.usage as usage_mod

    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    # "a" sorts first and gets the NEWER mtime; "b" sorts second and is older.
    (d / "a.jsonl").write_text("x")  # 1 byte
    (d / "b.jsonl").write_text("yy")  # 2 bytes
    newer_ns, older_ns = 2_000_000_000_000_000_000, 1_000_000_000_000_000_000
    os.utime(d / "a.jsonl", ns=(newer_ns, newer_ns))
    os.utime(d / "b.jsonl", ns=(older_ns, older_ns))

    count, size, max_mtime_ns = usage_mod._transcript_dir_stamp(project, claude_dir)
    assert count == 2 and size == 3
    assert max_mtime_ns == newer_ns  # the older second file did not lower the max


# ----- transcript turn reader (read-only viewer, #431) -----------------

TURNS_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "transcripts" / "turns-session.jsonl"
)


def test_read_transcript_turns_shape_and_skips():
    # The realistic fixture has 4 renderable turns; a summary (no message), a
    # non-JSON line, and a role-less record are all skipped (tolerant like parse).
    turns = read_transcript_turns(TURNS_FIXTURE)
    assert len(turns) == 4
    assert all(set(t) == {"role", "content", "model", "timestamp"} for t in turns)
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    # role/content/model from message; timestamp from the record.
    assert turns[1]["model"] == "claude-opus-4-8"
    assert turns[1]["timestamp"] == "2026-06-25T10:00:05Z"
    assert turns[0]["model"] is None  # user turn carries no model


def test_read_transcript_turns_renders_block_list():
    # A list-of-blocks content surfaces text blocks and summarizes non-text blocks
    # (tool_use/tool_result) as a typed placeholder — never the raw payload.
    turns = read_transcript_turns(TURNS_FIXTURE)
    assistant = turns[1]["content"]
    assert "Sure — here is the plan." in assistant
    assert "[tool_use]" in assistant
    user_blocks = turns[2]["content"]
    assert "[tool_result]" in user_blocks


def test_read_transcript_turns_redacts_planted_secrets():
    # THE security gate: planted session id / sk- key / AKIA id in turn text must
    # be redacted (sanitize_line applied) before the reader returns them.
    turns = read_transcript_turns(TURNS_FIXTURE)
    blob = "\n".join(t["content"] for t in turns)
    assert "DEADBEEF012345" not in blob
    assert "sk-ABCDEF0123456789ghij" not in blob
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "<redacted>" in blob


def test_read_transcript_turns_tolerates_malformed(tmp_path):
    # Blank lines, corrupt JSON, a bare-list record, and a non-dict message are all
    # skipped rather than raising — the page never aborts on one bad line.
    p = tmp_path / "messy.jsonl"
    p.write_text(
        "\n"
        + '{"message": {"role": "user", "content": "hello"}}\n'
        + "{not json}\n"
        + "[1, 2, 3]\n"
        + '{"message": "a string, not a dict"}\n'
        + '{"message": {"role": "assistant", "content": null}}\n'
    )
    turns = read_transcript_turns(p)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["content"] == ""  # None content renders empty, no crash


def test_read_transcript_turns_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_transcript_turns(tmp_path / "nope.jsonl")


# ----- read_transcript_summary (#1035) ----------------------------------
# The summary cache is process-wide, so every cache test clears it around itself.


@pytest.fixture
def _clean_transcript_cache():
    """Drop the process-wide transcript-summary cache before and after a cache test."""
    invalidate_transcript_summary_cache()
    yield
    invalidate_transcript_summary_cache()


def _summary_records():
    return [
        {
            "message": {"role": "user", "content": "first question"},
            "timestamp": "2026-01-01T00:00:00Z",
        },
        {
            "message": {"role": "assistant", "content": "an answer"},
            "timestamp": "2026-01-01T00:01:00Z",
        },
        {
            "message": {"role": "user", "content": "second question"},
            "timestamp": "2026-01-01T00:02:00Z",
        },
    ]


def test_read_transcript_summary_fields(tmp_path, _clean_transcript_cache):
    p = _transcript(tmp_path, _summary_records())
    summary = read_transcript_summary(p)
    assert summary.turn_count == 3
    assert summary.first_prompt == "first question"  # first USER turn labels it
    assert summary.first_ts == "2026-01-01T00:00:00Z"
    assert summary.last_ts == "2026-01-01T00:02:00Z"


def test_transcript_summary_first_prompt_truncated_to_120(tmp_path, _clean_transcript_cache):
    p = _transcript(tmp_path, [{"message": {"role": "user", "content": "x" * 500}}])
    assert len(read_transcript_summary(p).first_prompt) == 120


def test_transcript_summary_first_prompt_skips_command_wrappers(tmp_path, _clean_transcript_cache):
    # #1315: a session started via a slash command opens with wrapper turns; the label
    # must be the first HUMAN prompt, not the caveat/command markup.
    p = _transcript(
        tmp_path,
        [
            {"message": {"role": "user", "content": "<local-command-caveat>Caveat: ..."}},
            {"message": {"role": "user", "content": "<command-name>/model</command-name>"}},
            {"message": {"role": "assistant", "content": "switched"}},
            {"message": {"role": "user", "content": "the real question"}},
        ],
    )
    assert read_transcript_summary(p).first_prompt == "the real question"


def test_transcript_summary_all_wrapper_turns_fall_back_to_first(
    tmp_path, _clean_transcript_cache
):
    # When every user turn is wrapper machinery, keep the old first-turn label — a
    # previously non-empty label must never go blank.
    p = _transcript(
        tmp_path,
        [
            {"message": {"role": "user", "content": "<command-name>/status</command-name>"}},
            {"message": {"role": "assistant", "content": "ok"}},
        ],
    )
    assert read_transcript_summary(p).first_prompt == "<command-name>/status</command-name>"


def test_transcript_summary_cached_hit_skips_reparse(
    tmp_path, monkeypatch, _clean_transcript_cache
):
    import clauster.usage as usage_mod

    p = _transcript(tmp_path, _summary_records())
    calls = [0]
    real = usage_mod.read_transcript_turns

    def _counting(path):
        calls[0] += 1
        return real(path)

    monkeypatch.setattr(usage_mod, "read_transcript_turns", _counting)
    first = read_transcript_summary(p)
    second = read_transcript_summary(p)
    # The unchanged transcript is parsed once; the second open is served from cache.
    assert calls[0] == 1
    assert first == second


def test_transcript_summary_reparses_when_file_changes(
    tmp_path, monkeypatch, _clean_transcript_cache
):
    import clauster.usage as usage_mod

    p = _transcript(tmp_path, _summary_records())
    calls = [0]
    real = usage_mod.read_transcript_turns

    def _counting(path):
        calls[0] += 1
        return real(path)

    monkeypatch.setattr(usage_mod, "read_transcript_turns", _counting)
    assert read_transcript_summary(p).turn_count == 3
    # Append a turn: the (mtime_ns, size) stamp moves, so the summary re-derives.
    with p.open("a") as fh:
        fh.write(json.dumps({"message": {"role": "user", "content": "third"}}) + "\n")
    assert read_transcript_summary(p).turn_count == 4
    assert calls[0] == 2  # a real re-parse, not a stale cache hit


def test_read_transcript_summary_missing_file_raises_filenotfound(
    tmp_path, _clean_transcript_cache
):
    with pytest.raises(FileNotFoundError):
        read_transcript_summary(tmp_path / "nope.jsonl")


def test_transcript_summary_cache_is_lru_bounded(tmp_path):
    # #1058: the cache is bounded so a churn of deleted transcripts can't grow it without limit.
    # Past the cap the least-recently-used entry is evicted; a recently-touched one survives.
    from clauster.usage import _TranscriptSummaryCache

    def _make(name):
        p = tmp_path / name
        p.write_text(json.dumps({"message": {"role": "user", "content": name}}) + "\n")
        return p

    cache = _TranscriptSummaryCache(max_entries=2)
    p0, p1 = _make("t0.jsonl"), _make("t1.jsonl")
    cache.get(p0)
    cache.get(p1)
    cache.get(p0)  # touch t0 so t1 becomes the least-recently-used
    p2 = _make("t2.jsonl")
    cache.get(p2)  # over the cap -> evict the LRU (t1), keep the touched t0 + the new t2
    assert len(cache._entries) == 2
    assert str(p0) in cache._entries
    assert str(p1) not in cache._entries
    assert str(p2) in cache._entries


# ----- subagent (sidechain) classification (#1092) -----------------------
# The picker must not list a dispatched subagent's transcript as a forkable conversation.
# Every ambiguous shape has to fall back to "real conversation" — never silently hidden.


def _sidechain(flag, content="hi"):
    return {"isSidechain": flag, "message": {"role": "user", "content": content}}


def test_is_subagent_transcript_true_when_every_flagged_record_is_sidechain(tmp_path):
    from clauster.usage import _is_subagent_transcript

    p = _transcript(tmp_path, [_sidechain(True), _sidechain(True, "more")])
    assert _is_subagent_transcript(p) is True


def test_is_subagent_transcript_false_for_a_normal_conversation(tmp_path):
    from clauster.usage import _is_subagent_transcript

    p = _transcript(tmp_path, [_sidechain(False), _sidechain(False, "more")])
    assert _is_subagent_transcript(p) is False


def test_is_subagent_transcript_false_when_a_main_thread_record_is_present(tmp_path):
    # A MIXED transcript is a real conversation that merely embeds a sidechain turn.
    # Hiding it would lose a genuine conversation, so one non-sidechain record decides.
    from clauster.usage import _is_subagent_transcript

    p = _transcript(tmp_path, [_sidechain(False), _sidechain(True, "subagent turn")])
    assert _is_subagent_transcript(p) is False


def test_is_subagent_transcript_false_when_a_sidechain_record_leads_a_mixed_file(tmp_path):
    # Order must not matter: the non-sidechain record still wins even when it comes second.
    from clauster.usage import _is_subagent_transcript

    p = _transcript(tmp_path, [_sidechain(True), _sidechain(False, "main turn")])
    assert _is_subagent_transcript(p) is False


def test_is_subagent_transcript_false_when_the_key_is_absent(tmp_path):
    # An older Claude Code build never wrote isSidechain — unflagged is a real conversation.
    from clauster.usage import _is_subagent_transcript

    p = _transcript(tmp_path, [{"message": {"role": "user", "content": "hi"}}])
    assert _is_subagent_transcript(p) is False


def test_is_subagent_transcript_ignores_malformed_and_non_dict_records(tmp_path):
    from clauster.usage import _is_subagent_transcript

    p = tmp_path / "t.jsonl"
    p.write_text(
        "not json at all\n"
        + json.dumps(["a bare list, not a record"])
        + "\n"
        + json.dumps(_sidechain(True))
        + "\n"
    )
    # The junk lines are skipped, leaving a uniformly-sidechain transcript.
    assert _is_subagent_transcript(p) is True


def test_is_subagent_transcript_true_for_a_headless_sdk_py_run(tmp_path):
    # #1309: a headless agent run (SDK/hook dispatch) self-declares entrypoint "sdk-py" on
    # records that ALSO carry isSidechain false — the entrypoint marker must win.
    from clauster.usage import _is_subagent_transcript

    record = dict(_sidechain(False), entrypoint="sdk-py")
    p = _transcript(tmp_path, [record, _sidechain(False, "more")])
    assert _is_subagent_transcript(p) is True


def test_is_subagent_transcript_false_for_interactive_entrypoints(tmp_path):
    # Only the exact "sdk-py" value hides; interactive entrypoints stay listed.
    from clauster.usage import _is_subagent_transcript

    for value in ("cli", "sdk-cli", "claude-desktop"):
        p = _transcript(tmp_path, [dict(_sidechain(False), entrypoint=value)])
        assert _is_subagent_transcript(p) is False, value


def test_is_subagent_transcript_sdk_py_beyond_the_bound_is_not_seen(tmp_path):
    # The scan is bounded; a marker past max_records falls back to "real conversation".
    from clauster.usage import _is_subagent_transcript

    unflagged = {"message": {"role": "user", "content": "hi"}}  # neither marker key
    records = [unflagged] * 3 + [dict(_sidechain(False), entrypoint="sdk-py")]
    p = _transcript(tmp_path, records)
    assert _is_subagent_transcript(p, max_records=2) is False
    # Same file, default bound: the marker IS within reach and decides.
    assert _is_subagent_transcript(p) is True


def test_is_subagent_transcript_unreadable_file_is_treated_as_a_conversation(tmp_path):
    from clauster.usage import _is_subagent_transcript

    # A missing file raises OSError on open; unproven means "leave it listed".
    assert _is_subagent_transcript(tmp_path / "nope.jsonl") is False


def test_is_subagent_transcript_stops_at_the_record_bound(tmp_path):
    from clauster.usage import _is_subagent_transcript

    # The deciding non-sidechain record sits past the bound, so it is never read and the
    # leading sidechain records decide. Documents the bound rather than endorsing it.
    p = _transcript(tmp_path, [_sidechain(True), _sidechain(True), _sidechain(False)])
    assert _is_subagent_transcript(p, max_records=2) is True
    assert _is_subagent_transcript(p, max_records=3) is False


def test_transcript_summary_carries_is_subagent(tmp_path, _clean_transcript_cache):
    p = _transcript(tmp_path, [_sidechain(True), _sidechain(True, "more")])
    assert read_transcript_summary(p).is_subagent is True


def test_transcript_summary_is_subagent_false_for_the_shared_fixture(
    tmp_path, _clean_transcript_cache
):
    # The repo fixture is a real conversation (isSidechain: false), so the default listing
    # behavior is unchanged for it.
    p = tmp_path / "fixture.jsonl"
    p.write_bytes(FIXTURE.read_bytes())
    assert read_transcript_summary(p).is_subagent is False


def test_read_transcript_turns_render_content_unexpected_shape(tmp_path):
    # A content that is neither str nor list (an int, or a bare dict) is summarized
    # as a generic [content] placeholder — never dumped raw and never raised.
    p = tmp_path / "weird.jsonl"
    p.write_text(
        json.dumps({"message": {"role": "user", "content": 42}})
        + "\n"
        + json.dumps({"message": {"role": "user", "content": {"secret": "payload"}}})
        + "\n"
    )
    turns = read_transcript_turns(p)
    assert turns[0]["content"] == "[content]"
    # The raw dict payload is never surfaced — only the placeholder.
    assert turns[1]["content"] == "[content]"
    assert "payload" not in turns[1]["content"]


def test_read_transcript_turns_render_content_mixed_block_elements(tmp_path):
    # A block list with a bare string element and a non-dict element (a number):
    # the string is surfaced, the non-dict element is skipped — no crash.
    p = tmp_path / "blocks.jsonl"
    content = ["plain text element", 99, {"type": "text", "text": "typed"}, {"no": "type"}]
    p.write_text(json.dumps({"message": {"role": "user", "content": content}}) + "\n")
    [turn] = read_transcript_turns(p)
    assert "plain text element" in turn["content"]
    assert "typed" in turn["content"]
    assert "[block]" in turn["content"]  # a dict block with no "type" -> generic label
    assert "99" not in turn["content"]  # the non-dict element is skipped, not stringified


def test_read_transcript_turns_text_block_with_non_string_text_is_skipped(tmp_path):
    # A {"type": "text"} block whose `text` is missing or non-string is skipped (not
    # appended, never raised) — covers the `isinstance(text, str)` False branch in
    # _render_content (the 269->260 partial codecov flagged).
    p = tmp_path / "badtext.jsonl"
    content = [
        {"type": "text", "text": "kept"},
        {"type": "text"},  # no text field
        {"type": "text", "text": None},  # non-string text
        {"type": "text", "text": 42},  # non-string text
    ]
    p.write_text(json.dumps({"message": {"role": "assistant", "content": content}}) + "\n")
    [turn] = read_transcript_turns(p)
    assert turn["content"] == "kept"  # only the valid text block survives


# ----- incremental offset reader (live tail, #614 Part 2) ---------------


def test_read_transcript_turns_from_offset_zero_matches_whole_reader(tmp_path):
    # From offset 0, the tail reader yields the SAME turns as the whole-file reader
    # (shared _line_to_turn) but in file order; the offset lands at the file size.
    p = _transcript(
        tmp_path,
        [
            {"message": {"role": "user", "content": "a"}},
            {"message": {"role": "assistant", "content": "b"}},
        ],
    )
    turns, offset, reset = read_transcript_turns_from_offset(p, 0)
    assert [t["content"] for t in turns] == ["a", "b"]
    assert offset == p.stat().st_size
    assert reset is False


def test_read_transcript_turns_from_offset_reads_only_appended(tmp_path):
    p = _transcript(tmp_path, [{"message": {"role": "user", "content": "a"}}])
    _, offset, _ = read_transcript_turns_from_offset(p, 0)
    with p.open("ab") as fh:
        fh.write(b'{"message": {"role": "assistant", "content": "b"}}\n')
    turns, new_offset, reset = read_transcript_turns_from_offset(p, offset)
    assert [t["content"] for t in turns] == ["b"]  # only the appended record
    assert new_offset == p.stat().st_size
    assert reset is False


def test_read_transcript_turns_from_offset_eof_returns_nothing(tmp_path):
    p = _transcript(tmp_path, [{"message": {"role": "user", "content": "a"}}])
    size = p.stat().st_size
    turns, offset, reset = read_transcript_turns_from_offset(p, size)
    assert turns == []
    assert offset == size
    assert reset is False


def test_read_transcript_turns_from_offset_partial_line_left_unconsumed(tmp_path):
    # A trailing line with no newline yet is not parsed and not consumed; once it's
    # completed, the next read picks it up exactly once.
    p = _transcript(tmp_path, [{"message": {"role": "user", "content": "a"}}])
    _, offset, _ = read_transcript_turns_from_offset(p, 0)
    with p.open("ab") as fh:
        fh.write(b'{"message": {"role": "user", "content": "par')  # half-written
    turns, mid_offset, reset = read_transcript_turns_from_offset(p, offset)
    assert turns == []
    assert mid_offset == offset  # did not advance past the partial line
    with p.open("ab") as fh:
        fh.write(b'tial"}}\n')
    turns2, _, _ = read_transcript_turns_from_offset(p, mid_offset)
    assert [t["content"] for t in turns2] == ["partial"]


def test_read_transcript_turns_from_offset_truncated_file_resets(tmp_path):
    p = _transcript(tmp_path, [{"message": {"role": "user", "content": "longer original"}}])
    _, offset, _ = read_transcript_turns_from_offset(p, 0)
    # Replace with a shorter file: now offset > size -> reset, re-read from 0.
    p.write_bytes(b'{"message": {"role": "user", "content": "x"}}\n')
    turns, new_offset, reset = read_transcript_turns_from_offset(p, offset)
    assert reset is True
    assert [t["content"] for t in turns] == ["x"]
    assert new_offset == p.stat().st_size


def test_read_transcript_turns_from_offset_negative_clamped(tmp_path):
    p = _transcript(tmp_path, [{"message": {"role": "user", "content": "a"}}])
    turns, _, reset = read_transcript_turns_from_offset(p, -10)
    assert [t["content"] for t in turns] == ["a"]
    assert reset is False  # a clamped negative offset is not a reset


def test_read_transcript_turns_from_offset_redacts(tmp_path):
    p = _transcript(
        tmp_path,
        [{"message": {"role": "user", "content": "key sk-ABCDEF0123456789ghij here"}}],
    )
    turns, _, _ = read_transcript_turns_from_offset(p, 0)
    assert "sk-ABCDEF0123456789ghij" not in turns[0]["content"]
    assert "<redacted>" in turns[0]["content"]


def test_read_transcript_turns_from_offset_skips_malformed(tmp_path):
    # Blank / non-JSON / no-message / no-role lines are skipped, mirroring the
    # whole-file reader (shared _line_to_turn), never raising.
    p = tmp_path / "mixed.jsonl"
    p.write_text(
        "\n"
        "not json\n"
        '{"summary": "no message field"}\n'
        '{"message": {"content": "no role"}}\n'
        '{"message": {"role": "user", "content": "kept"}}\n'
    )
    turns, _, _ = read_transcript_turns_from_offset(p, 0)
    assert [t["content"] for t in turns] == ["kept"]


def test_read_transcript_turns_from_offset_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_transcript_turns_from_offset(tmp_path / "nope.jsonl", 0)


def test_resolve_session_transcript_happy_path(tmp_path):
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "abc123.jsonl").write_text("")
    resolved = resolve_session_transcript(project, "abc123", claude_dir)
    assert resolved is not None
    assert resolved.name == "abc123.jsonl"
    assert resolved.parent == d.resolve()


@pytest.mark.parametrize(
    "session",
    [
        "",
        ".",
        "..",
        "../secret",
        "sub/abc",
        "a\\b",
        "abc\x00d",
        "/etc/passwd",
    ],
)
def test_resolve_session_transcript_rejects_unsafe(tmp_path, session):
    # Path-traversal / separator / NUL / absolute inputs all fail closed to None —
    # never escaping the project's transcript dir.
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    _project_transcript_dir(claude_dir, project)
    assert resolve_session_transcript(project, session, claude_dir) is None


def test_resolve_session_transcript_unknown_session_is_none(tmp_path):
    # A safe stem with no matching file resolves to None (caller maps to 404).
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    _project_transcript_dir(claude_dir, project)
    assert resolve_session_transcript(project, "ghost", claude_dir) is None


def test_resolve_session_transcript_traversal_cannot_escape(tmp_path):
    # Plant a file OUTSIDE the transcript dir and prove a crafted session can't
    # reach it: even if a separator slipped past, the parent-identity check fails.
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    outside = d.parent / "outside.jsonl"
    outside.write_text("secret")
    # The component guard rejects the separator outright -> None (no escape).
    assert resolve_session_transcript(project, "../outside", claude_dir) is None


def test_resolve_session_transcript_parent_identity_mismatch(tmp_path, monkeypatch):
    # Defense-in-depth: if the resolved candidate's parent isn't the expected dir
    # (e.g. a symlinked transcript dir that normalizes elsewhere), fail closed even
    # though the bare-stem component guard passed. Force the mismatch by making
    # resolve() return a path in a different directory.
    import clauster.usage as usage_mod

    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    _project_transcript_dir(claude_dir, project)
    elsewhere = tmp_path / "elsewhere" / "abc.jsonl"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("")
    real_resolve = Path.resolve

    def _fake_resolve(self, *a, **k):
        # Redirect only the candidate file to a foreign directory; leave the dir.
        if self.name == "abc.jsonl":
            return elsewhere
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(usage_mod.Path, "resolve", _fake_resolve)
    assert resolve_session_transcript(project, "abc", claude_dir) is None


def test_resolve_session_transcript_is_file_oserror_is_none(tmp_path, monkeypatch):
    # An OSError from the is_file() probe (a racing permission/IO fault) fails closed
    # to None rather than propagating — the route maps that to a clean 404.
    claude_dir = tmp_path / "claude_projects"
    project = Path("/srv/projects/my_proj")
    d = _project_transcript_dir(claude_dir, project)
    (d / "abc.jsonl").write_text("")

    def _boom_is_file(self):
        raise OSError("io error")

    monkeypatch.setattr(Path, "is_file", _boom_is_file)
    assert resolve_session_transcript(project, "abc", claude_dir) is None
