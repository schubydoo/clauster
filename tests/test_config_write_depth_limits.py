"""The nesting-depth cap of the config-write tier (TASK-502, TASK-528).

A frontmatter header's flow collections, a Settings candidate and a ``claude plugin`` list
read may nest at most ``cw.MAX_NESTING_DEPTH`` levels. Deeper is a named refusal, found by
a linear pass before the costly parse or walk. Tests count operations, never wall clock.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_plugins as plugins
from clauster import config_write_settings as cws
from clauster import config_write_skills as sk
from clauster import config_write_subagents as sub
from clauster.app import create_app
from clauster.config import load_config

MAX = cw.MAX_NESTING_DEPTH
MASK = cw.REDACTION_SENTINEL


def _flow(depth: int, inner: str = "a") -> str:
    return "[" * depth + inner + "]" * depth


def _parsers():
    return [
        pytest.param(sub.parse_frontmatter, id="subagents"),
        pytest.param(sk.parse_frontmatter, id="skills"),
    ]


def _simple_key_work(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the scanner's possible-key walk: [keys walked, walks].

    PyYAML's scanner keeps one possible key per open flow collection and walks them all
    at every token (``stale_possible_simple_keys``). That walk is the term that grows with
    the flow depth, so its count stands in for the cost the cap bounds.
    """
    real = yaml.scanner.Scanner.stale_possible_simple_keys
    work = [0, 0]

    def counting(self: yaml.scanner.Scanner) -> None:
        work[0] += len(self.possible_simple_keys)
        work[1] += 1
        real(self)

    monkeypatch.setattr(yaml.scanner.Scanner, "stale_possible_simple_keys", counting)
    return work


# --- frontmatter: the scan before the parse -------------------------------------------------


@pytest.mark.parametrize("parse", _parsers())
@pytest.mark.parametrize(
    ("open_", "close", "per"),
    [("[", "]", 1), ("{a: ", "}", 1), ("[{a: ", "}]", 2)],
    ids=["seq", "map", "mixed"],
)
def test_flow_nesting_at_the_cap_loads_and_one_more_level_is_refused(
    parse, open_: str, close: str, per: int
) -> None:
    def text(repeats: int) -> str:  # `per` collections open per repeat
        return f"---\nname: a\ndescription: d\nk: {open_ * repeats}x{close * repeats}\n---\nb\n"

    parse(text(MAX // per))  # no raise
    with pytest.raises(cw.InvalidCandidateError, match=rf"more than {MAX} levels deep \(line 4\)"):
        parse(text(MAX // per + 1))


@pytest.mark.parametrize("parse", _parsers())
@pytest.mark.parametrize(
    ("value", "loaded"),
    [
        pytest.param('"' + "[" * 500 + '"', "[" * 500, id="double-quoted"),
        pytest.param("'" + "{" * 500 + "'", "{" * 500, id="single-quoted"),
        pytest.param("x # " + "[" * 500, "x", id="comment"),
        pytest.param("|-\n  " + "[" * 500, "[" * 500, id="block-scalar"),
        pytest.param("a" + "[" * 500, "a" + "[" * 500, id="plain-in-block-context"),
    ],
)
def test_brackets_the_parse_does_not_read_as_collections_do_not_count(
    parse, value: str, loaded: str
) -> None:
    # The cap is applied by PyYAML's own scanner, so it reads quotes, comments and block
    # scalars exactly as the parse does: none of these is a collection at all.
    frontmatter, _body = parse(f"---\nname: a\ndescription: d\nk: {value}\n---\nb\n")
    assert frontmatter["k"] == loaded


def test_a_too_deep_header_is_refused_before_any_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC 1: nothing composes or loads the header. Only the token scan runs.
    def no_parse(*_a: object, **_k: object) -> None:
        raise AssertionError("the header was parsed")

    monkeypatch.setattr(cw.yaml, "compose", no_parse)
    monkeypatch.setattr(cw.yaml, "safe_load", no_parse)
    header = "name: a\nk: " + _flow(5_000)
    with pytest.raises(cw.InvalidCandidateError, match="nested too deeply"):
        cw.load_frontmatter_yaml(header, what="frontmatter", line_offset=1)


def test_the_refusal_stops_the_scan_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unbounded, this header costs about depth x tokens of key walking. The scan stops at
    # level MAX + 1, so the walk never holds more than MAX + 1 keys.
    work = _simple_key_work(monkeypatch)
    header = "k: " + "[" * 400 + ", ".join(["a"] * 2000) + "]" * 400
    with pytest.raises(cw.InvalidCandidateError, match="nested too deeply"):
        cw.load_frontmatter_yaml(header, what="frontmatter", line_offset=1)
    assert work[0] <= (MAX + 1) * work[1]
    assert work[0] < 10_000  # unbounded: about 400 x 2000 keys walked for this header


@pytest.mark.parametrize("parse", _parsers())
def test_deep_block_nesting_is_not_capped_but_still_fails_closed(parse) -> None:
    # Block nesting costs the scanner nothing per level, so the cap leaves it alone. Past
    # the recursion limit, the composer's RecursionError is still a refusal, never a 500.
    parse("---\nname: a\ndescription: d\nk:\n" + "- " * (MAX + 10) + "x\n---\nb\n")
    deep = "---\nname: a\ndescription: d\nk:\n" + "- " * 5_000 + "x\n---\nb\n"
    with pytest.raises(cw.InvalidCandidateError, match="nested too deeply to parse"):
        parse(deep)


def test_a_syntax_error_ends_the_depth_scan_and_the_parse_reports_it() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="is not valid YAML"):
        cw.load_frontmatter_yaml("k: 'unclosed\n" + _flow(5_000), what="fm", line_offset=1)


def test_the_cli_text_raises_for_a_too_deep_header() -> None:
    # A caller composes the text this returns, so a header nested past the cap must never
    # come back from it: the error is a YAMLError, which those callers already handle.
    with pytest.raises(cw.FlowNestingTooDeepError) as caught:
        cw.cli_frontmatter_text("a: 1\nk: " + _flow(MAX + 1))
    assert isinstance(caught.value, yaml.YAMLError)
    assert caught.value.mark.line == 1
    header = "k: " + _flow(MAX)
    assert cw.cli_frontmatter_text(header) == header


@pytest.mark.parametrize(
    "deep_line",
    [
        pytest.param("b: " + _flow(MAX + 1), id="a-top-level-list-value"),
        pytest.param("m:\n  b: " + _flow(MAX + 1), id="a-line-the-retry-keeps"),
    ],
)
def test_a_retry_that_would_nest_past_the_cap_keeps_the_header(deep_line: str) -> None:
    # The header is not valid YAML (`[x] [y]`), so the CLI reads the retry text. Its deep
    # value, or the deep retry text, is never composed: the header comes back, and every
    # parse of it stops at the parser error on line 1, before the deep part. The seam's own
    # scan does not stop at a parser error, so it reads on and refuses line 2 by name.
    header = f"argument-hint: [x] [y]\n{deep_line}"
    assert cw.cli_frontmatter_text(header) == header
    with pytest.raises(cw.InvalidCandidateError, match=r"nested too deeply.*\(line [34]\)"):
        cw.load_frontmatter_yaml(header, what="frontmatter", line_offset=1)


def test_a_too_deep_skill_header_on_disk_is_shown_masked_whole(tmp_path: Path) -> None:
    # The read path masks a header it cannot trust line by line, never by its nodes. A
    # deep one used to compose (slowly) and show every line that held no secret.
    skill = tmp_path / "skills" / "s"
    skill.mkdir(parents=True)
    text = (
        "---\ndescription: d\nk: "
        + _flow(MAX + 1, "visible-TEXT")
        + "\nmcpServers:\n  s:\n    url: https://u:pw-SECRET@h.example/x\n---\nbody\n"
    )
    (skill / sk.SKILL_FILENAME).write_bytes(text.encode("utf-8"))
    view, _hash, _exists, _note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert "pw-SECRET" not in view and "visible-TEXT" not in view
    assert view.endswith("---\nbody\n")


def test_a_too_deep_subagent_on_disk_reads_degraded_with_bounded_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file an older release or another editor wrote. The read used to parse it in full,
    # at depth x size, several times; now it degrades like any header that does not load.
    agents = sub.project_agents_dir(tmp_path)
    agents.mkdir(parents=True)
    deep = "[" * 400 + ", ".join(["a"] * 500) + "]" * 400
    text = f"---\nname: my-agent\ndescription: d\napi_token: fake-TOKEN\nk: {deep}\n---\nbody\n"
    (agents / "my-agent.md").write_bytes(text.encode("utf-8"))
    work = _simple_key_work(monkeypatch)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["frontmatter"] == {}
    assert "fake-TOKEN" not in doc["content"] and doc["content"].endswith("---\nbody\n")
    assert work[0] <= (MAX + 1) * work[1]


# --- frontmatter: a masked save at the cap (AC 2) -------------------------------------------

_STDIO = (
    "---\nname: my-agent\ndescription: d\nmcpServers:\n  - local:\n      command: npx\n"
    "      env:\n        GH_PAT: fake-pat-SECRET\n"
)


def _masked_save_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int) -> list[int]:
    agents = sub.project_agents_dir(tmp_path)
    agents.mkdir(parents=True, exist_ok=True)
    deep = "[" * depth + ", ".join(["a"] * 200) + "]" * depth
    text = _STDIO + f"x: {deep}\n---\nbody\n"
    path = agents / "my-agent.md"
    path.write_bytes(text.encode("utf-8"))
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert MASK in doc["content"] and "fake-pat-SECRET" not in doc["content"]
    work = _simple_key_work(monkeypatch)
    sub.write_project_agent(tmp_path, "my-agent", doc["content"], doc["hash"])
    assert path.read_bytes() == text.encode("utf-8")  # the masked value was kept
    return work


def test_a_masked_save_at_the_cap_does_bounded_scanner_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _masked_save_work(tmp_path, monkeypatch, MAX)
    assert work[0] <= (MAX + 1) * work[1]  # at most one key per open collection per token
    # The save scans the header a few dozen times. At the cap the key walk is about 2.2 M
    # steps (a few tenths of a second); the same save 200 deep took about 9 M, and 400
    # deep 32 M (measured with the cap lifted; see the next test).
    assert work[0] < 3_000_000


def test_the_scanner_work_count_sees_the_depth_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Positive control for the budget above: lift the cap and the same save 200 deep walks
    # far more keys, so a count under budget at the cap is a real bound.
    monkeypatch.setattr(cw, "MAX_NESTING_DEPTH", 10_000)
    work = _masked_save_work(tmp_path, monkeypatch, 200)
    assert work[0] > 6_000_000


# --- frontmatter: the _changed_scalars sequence branch (TASK-502) -------------------------


def test_changed_scalars_never_walks_a_list_its_node_did_not_load() -> None:
    # The sequence branch compares only the lengths of the loaded and the target list: the
    # loaded list has one item per node child. A value that does not match its node is a
    # caller bug, and the strict zip raises rather than pair the wrong items.
    root = yaml.compose("k: [a, b]\n", Loader=yaml.SafeLoader)
    with pytest.raises(ValueError):
        sub._changed_scalars(root, {"k": ["a"]}, {"k": ["b"]})


# --- settings --------------------------------------------------------------------------------


def _nested(depth: int, leaf: object = "v") -> object:
    """Return ``leaf`` inside ``depth`` lists."""
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def test_a_settings_candidate_at_the_cap_writes_and_one_more_level_is_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "settings.json"
    ok = {"k": _nested(MAX - 1)}  # the candidate object is level 1
    cws.write_user_settings(path, ok, None)
    assert json.loads(path.read_text(encoding="utf-8"))["k"] == ok["k"]
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match=rf"nested too deeply.*more than {MAX}"):
        cws.write_user_settings(path, {"k": _nested(MAX)}, cw.hash_bytes(before))
    assert path.read_bytes() == before


def test_exceeds_nesting_depth_counts_dicts_and_lists_from_the_outermost() -> None:
    assert not cw.exceeds_nesting_depth("scalar")
    assert not cw.exceeds_nesting_depth(_nested(MAX))
    assert cw.exceeds_nesting_depth(_nested(MAX + 1))
    deep_dict: object = 1
    for _ in range(MAX + 1):
        deep_dict = {"k": deep_dict}
    assert cw.exceeds_nesting_depth(deep_dict)
    # Iterative: any depth json.loads builds.
    assert cw.exceeds_nesting_depth(_nested(100_000))


def _deep_masked_settings(depth: int, width: int) -> tuple[dict, dict]:
    """Return ``(stored, incoming)``: a secret at the end of a wide list ``depth`` deep."""
    stored = {"k": _nested(depth - 1, ["a"] * width + [{"token": "fake-SECRET"}])}
    incoming = json.loads(json.dumps(stored))
    leaf = incoming["k"]
    for _ in range(depth - 1):
        leaf = leaf[0]
    leaf[-1]["token"] = MASK
    return stored, incoming


def test_the_mask_checks_of_a_settings_save_are_linear_in_its_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TASK-502: the orphan check asked every list on a path whether it held a mask, and
    # walked the whole list again each time: depth x size. Counted: calls of the walkers.
    calls = [0]
    for name in ("_has_sentinel", "_unchanged_but_masks"):
        real = getattr(cws, name)

        def counting(*args: object, _real=real) -> bool:
            calls[0] += 1
            return _real(*args)

        monkeypatch.setattr(cws, name, counting)
    depth, width = MAX - 1, 1000
    stored, incoming = _deep_masked_settings(depth, width)
    assert cws._redact_misc(stored) == incoming  # the editor's view, resent unchanged
    cws._reject_orphan_sentinels(incoming, stored)  # no raise
    assert calls[0] <= 4 * (width + depth + 4)  # each node a few times, not once per level


def test_a_moved_mask_in_a_deep_list_is_still_refused() -> None:
    stored, incoming = _deep_masked_settings(MAX - 1, 3)
    leaf = incoming["k"]
    for _ in range(MAX - 2):
        leaf = leaf[0]
    leaf[0], leaf[-1] = leaf[-1], leaf[0]  # the masked entry moves to another slot
    with pytest.raises(cw.InvalidCandidateError, match="no longer matches"):
        cws._reject_orphan_sentinels(incoming, stored)


def test_sentinel_holders_marks_every_container_on_the_path_to_a_mask() -> None:
    inner = ["a", MASK]
    middle = {"x": inner, "y": ["b"]}
    outer = [middle, "c"]
    holders = cws._sentinel_holders(outer, "plain", {"z": ["d"]})
    assert holders == {id(outer), id(middle), id(inner)}


# --- settings: through the route --------------------------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"
_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"


def _client(write_config, tmp_path: Path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    return TestClient(create_app(load_config(cfg)))


def test_route_settings_put_too_deep_is_a_named_422(write_config, tmp_path: Path) -> None:
    with _client(write_config, tmp_path) as c:
        resp = c.put(
            "/api/config-write/settings",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "settings": {"k": _nested(300)},
            },
        )
    assert resp.status_code == 422
    assert "nested too deeply" in resp.text


# --- claude plugin list readers (TASK-528) --------------------------------------------------


def _fake_run(stdout: str):
    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(list(argv), 0, stdout=stdout, stderr="")

    return run


@pytest.mark.parametrize(
    ("reader", "wrap"),
    [
        ("cli_list_plugins", lambda deep: [{"id": "d@m", "x": deep}]),
        ("cli_list_marketplaces", lambda deep: [{"name": "m", "x": deep}]),
    ],
)
def test_list_readers_take_output_at_the_cap_and_refuse_one_more_level(
    tmp_path: Path, reader: str, wrap
) -> None:
    # The list is level 1 and its entry level 2, so the value under `x` gets MAX - 2.
    ok = json.dumps(wrap(_nested(MAX - 2)))
    out = getattr(plugins, reader)("claude", tmp_path, run=_fake_run(ok))
    assert out[0]["x"] == _nested(MAX - 2)
    deep = json.dumps(wrap(_nested(MAX - 1)))
    with pytest.raises(plugins.PluginCliError, match=rf"nested too deeply \(more than {MAX}"):
        getattr(plugins, reader)("claude", tmp_path, run=_fake_run(deep))


@pytest.mark.parametrize(
    ("url", "env", "wrap"),
    [
        (
            "/api/config-write/plugins?scope=user",
            "FAKE_CLAUDE_PLUGIN_LIST_STDOUT",
            lambda deep: [{"id": "d@m", "scope": "user", "x": deep}],
        ),
        (
            "/api/config-write/marketplaces?scope=user",
            "FAKE_CLAUDE_PLUGIN_MARKETPLACE_LIST_STDOUT",
            lambda deep: [{"name": "m", "x": deep}],
        ),
    ],
)
def test_route_list_of_output_nested_past_the_encoder_is_a_named_error_not_500(
    write_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, env: str, wrap
) -> None:
    # About 300 levels: the parse and the redaction walk take it on every supported
    # Python, and FastAPI's response encoder did not (a 500).
    monkeypatch.setenv(env, json.dumps(wrap(_nested(300))))
    with _client(write_config, tmp_path) as c:
        resp = c.get(url)
    assert resp.status_code == 400
    assert "nested too deeply" in resp.text
