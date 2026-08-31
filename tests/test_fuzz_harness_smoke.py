"""Import-smoke test for the Atheris fuzz harnesses (issue #356).

The harnesses under ``fuzz/`` are standalone OSS-Fuzz entry points, not part of
the package, so nothing in the default suite imports them — a rename or
signature change to a fuzzed function (``redact.sanitize_line``,
``provisioning.validate_clone_url``, …) silently breaks a harness until the next
scheduled fuzz run. This test imports each harness so that drift fails fast.

Importing a harness runs its module body, which calls
``atheris.instrument_imports()`` around the real ``clauster`` import — so the
fuzzed symbols must still exist and import cleanly. ``main()`` / ``atheris.Fuzz()``
is guarded behind ``if __name__ == "__main__"`` and never runs here.

Atheris ships Linux-only wheels (see ``pyproject.toml``); the test skips where it
is unavailable rather than failing a Windows/macOS CI cell. It also skips on a
Python newer than Atheris supports — Atheris 2.0 raises ``RuntimeError`` (not
``ImportError``) at import on an unsupported version (e.g. 3.14), which
``importorskip`` would not catch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

try:
    import atheris  # noqa: F401  — availability gate only; harnesses import it themselves
except (ImportError, RuntimeError) as exc:  # RuntimeError: unsupported Python (Atheris 2.0)
    pytest.skip(f"atheris unavailable: {exc}", allow_module_level=True)

_FUZZ_DIR = Path(__file__).resolve().parent.parent / "fuzz"
_HARNESSES = sorted(p.name for p in _FUZZ_DIR.glob("*_fuzzer.py"))


def test_fuzz_dir_has_harnesses() -> None:
    """Guard against the glob silently matching nothing (e.g. a moved fuzz/ dir)."""
    assert _HARNESSES, f"no *_fuzzer.py harnesses found under {_FUZZ_DIR}"


@pytest.mark.parametrize("harness", _HARNESSES)
def test_fuzz_harness_imports_and_exposes_entrypoints(harness: str) -> None:
    """Each harness imports cleanly and exposes the Atheris TestOneInput + main entry points."""
    path = _FUZZ_DIR / harness
    spec = importlib.util.spec_from_file_location(f"_fuzz_smoke.{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Executes the module body: instruments + imports the real clauster symbols,
    # so a signature/rename drift surfaces here as an ImportError/AttributeError.
    spec.loader.exec_module(module)
    assert callable(module.TestOneInput), f"{harness} missing a callable TestOneInput"
    assert callable(module.main), f"{harness} missing a callable main"


def _load(harness: str):
    path = _FUZZ_DIR / harness
    spec = importlib.util.spec_from_file_location(f"_fuzz_smoke.{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# URLs spanning every branch of the authorize-path / known-auth-host predicates:
# the real endpoint, the bare-/authorize fallback, the query-string decoy the path
# check exists to reject, docs/marketing exclusions, an ACCEPTED subdomain on each
# parent domain (the `endswith("." + suffix)` branch, which every excluded-prefix row
# short-circuits past), an unknown host that proves the leading dot is load-bearing,
# and the invalid-IPv6-bracket URL that makes urlsplit raise.
_PREDICATE_URLS = [
    "https://claude.com/cai/oauth/authorize?client_id=x",
    "https://claude.ai/oauth/authorize",
    "https://console.anthropic.com/authorize",
    "https://platform.claude.com/oauth/authorize",
    "https://claude.com/settings?redirect_uri=%2Foauth%2Fauthorize",
    "https://docs.anthropic.com/en/docs/oauth/authorize",
    "https://help.claude.com/oauth/authorize",
    "https://support.claude.ai/authorize",
    "https://www.claude.com/oauth/authorize",
    "https://anthropic.com/oauth/authorize",
    "https://notclaude.com/oauth/authorize",
    "https://evil.example/cai/oauth/authorize",
    "https://claude.com/",
    # Parses fine — urlsplit validates a port only when `.port` is read, and neither
    # helper reads it. Kept to pin that, not to drive the ValueError branch below.
    "https://claude.com:notaport/oauth/authorize",
    "https://[::1/oauth/authorize",
    "https://[bad/authorize",
    "",
]


@pytest.mark.parametrize("url", _PREDICATE_URLS)
def test_pty_login_scan_restated_predicates_match_pty_screen(url: str) -> None:
    """The harness's independent oracles must agree with the functions they judge.

    ``pty_login_scan_fuzzer`` deliberately restates ``_is_authorize_path`` and
    ``_is_known_auth_host`` rather than calling them, so an oracle cannot be fooled by
    a misclassification inside the code it is checking. That independence has a cost:
    a maintainer who legitimately broadens either predicate would otherwise get a green
    ``just check`` and learn about the drift days later, as an ``assert`` in ``fuzz/``
    surfacing in the Security tab. This test moves that failure back into the suite.
    """
    from clauster import pty_screen

    harness = _load("pty_login_scan_fuzzer.py")
    assert harness._path_is_authorize(url) == pty_screen._is_authorize_path(url), url
    assert harness._host_is_known_auth(url) == pty_screen._is_known_auth_host(
        pty_screen._url_host(url)
    ), url


# Inputs spanning every branch `redact_secret_lines_fuzzer` differentiates: each masked
# span shape, the empty-body `${}` and unterminated `${` the interpolation scan must NOT
# match, the leading `-` and doubled `://` the URL scan's lookbehind-free walk exists for,
# a non-ASCII scheme letter that case-folds into `[a-z]`, the author/authn boundary of the
# secret-key lookahead, and the CR/LF/vertical-tab line shapes.
_REDACT_LINE_INPUTS = [
    "",
    "nothing secret here",
    "env: ${GITHUB_TOKEN}",
    "a ${b${c} d ${} e ${f",
    "clone https://user:pw@example.test/repo.git",
    "-slack+v2://xoxb-0@host",
    "http://http://u@h",
    "q://<a://b@h",  # redacts to a URL shape that was never in the input
    "\u212a://user@host",
    "\u017fsh://u@h",
    # Low-entropy on purpose: gitleaks scans the PR's commit range and its generic-api-key
    # rule has an entropy gate; the KV matcher under test reads only the key + value SHAPE.
    "api_key: sk-live-FAKEFAKEFAKEFAKE",
    "AUTH_TOKEN = xoxb-1111   ",
    "authors: Someone",
    "author = Someone Else",
    "authn: masked",
    "token: token",
    "token: a\r\nplain\r\n${X}\rmid\x0bvtab: b\n",
    "data=" + "a" * 64 + "://b@c",
    "*" * 8,
]


@pytest.mark.parametrize("text", _REDACT_LINE_INPUTS)
def test_redact_secret_lines_reference_oracle_matches_implementation(text: str) -> None:
    """The differential harness's regex oracle must agree with the shipped scanners.

    ``redact_secret_lines_fuzzer`` re-derives the answer from the quadratic regexes the
    linear scanners in ``config_write`` replaced — deliberately *not* by calling those
    scanners, so a rewrite that under-masks cannot move both sides of the comparison
    together. Same trade as the ``pty_login_scan`` predicates above: a maintainer who
    changes the contract on purpose should fail ``just check``, not learn about it days
    later from a Security-tab SARIF.
    """
    from clauster import config_write

    harness = _load("redact_secret_lines_fuzzer.py")
    assert harness._reference_redact(text) == config_write.redact_secret_lines(text), text


def test_redact_secret_lines_oracle_fires_on_a_broken_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break the redaction on purpose; the harness must notice.

    ``fuzz/README.md``: an oracle that has never fired is indistinguishable from no
    oracle at all. Each property is fired **separately**, because they short-circuit in
    order: with only ``redact_secret_lines`` broken the differential rejects it first and
    the payload-survival assert is never reached, so a single case would leave the one
    property that directly encodes "no ``${…}`` leaks" with no proof it can fail at all.
    Breaking the reference the same way makes the two sides agree and hands the leak
    check the pass-through output it exists to catch.
    """
    from clauster import config_write

    harness = _load("redact_secret_lines_fuzzer.py")
    leaky = "env: ${GITHUB_TOKEN}"
    harness.check(leaky)  # unbroken: passes

    monkeypatch.setattr(config_write, "redact_secret_lines", lambda text: text)
    with pytest.raises(AssertionError, match="^differential:"):
        harness.check(leaky)

    monkeypatch.setattr(harness, "_reference_redact", lambda text: text)
    with pytest.raises(AssertionError, match="^leak:"):
        harness.check(leaky)


# --- usage_line_to_turn_fuzzer -------------------------------------------------------


_TRANSCRIPT_LINES = [
    "",
    "   ",
    "not json",
    "[1, 2]",
    '"a string"',
    "{}",
    '{"message": "not a dict"}',
    '{"message": {}}',
    '{"message": {"role": ""}}',
    '{"message": {"role": 7}}',
    '{"message": {"role": "user", "content": "hi"}}',
    '{"message": {"role": "user", "content": ["a", {"type": "text", "text": "b"}, 1]}}',
    '{"message": {"role": "user", "content": {"nested": 1}}, "timestamp": 5, "model": 5}',
    '{"message": {"role": "user", "content": "session_01JABCDEFGHJKMNPQ"}}',
    '{"message": {"role": "u", "content": "x"}, "timestamp": "2026-08-30T00:00:00Z"}',
    '{"message": {"role": "u", "content": ' + "[" * 400 + "]" * 400 + "}}",
]


@pytest.mark.parametrize("line", _TRANSCRIPT_LINES)
def test_usage_line_to_turn_reference_skip_rules_match(line: str) -> None:
    """The harness's re-derived skip rules must agree with ``_line_to_turn`` itself.

    The harness restates the docstring's "blank / not JSON / not a dict / no message dict
    / no role" rules from ``json.loads`` rather than calling the function, so a widened
    accept path is caught. Same trade as the ``pty_login_scan`` predicates: a maintainer
    changing the contract on purpose should fail ``just check``, not learn about it days
    later from a Security-tab SARIF.
    """
    from clauster import usage

    harness = _load("usage_line_to_turn_fuzzer.py")
    assert harness._reference_is_renderable(line) == (usage._line_to_turn(line) is not None)


def test_usage_line_to_turn_oracles_fire_on_a_broken_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break ``_line_to_turn`` on purpose; the harness must notice.

    ``fuzz/README.md``: an oracle that has never fired is indistinguishable from no oracle
    at all. Fired separately per property, because they short-circuit in order — a single
    broken case would leave the redaction assertion, the one that actually encodes
    invariant 4 here, with no proof it can fail.
    """
    from clauster import usage

    harness = _load("usage_line_to_turn_fuzzer.py")
    leaky = '{"message": {"role": "user", "content": "session_01JABCDEFGHJKMNPQ"}}'
    harness.check(leaky)  # unbroken: passes

    monkeypatch.setattr(usage, "_line_to_turn", lambda line: None)
    with pytest.raises(AssertionError, match="^skip rules disagree"):
        harness.check(leaky)

    monkeypatch.setattr(
        usage,
        "_line_to_turn",
        lambda line: {
            "role": "user",
            "content": "session_01JABCDEFGHJKMNPQ",
            "model": None,
            "timestamp": None,
        },
    )
    with pytest.raises(AssertionError, match="^redaction leak in content"):
        harness.check(leaky)

    monkeypatch.setattr(usage, "_line_to_turn", lambda line: {"role": "user"})
    with pytest.raises(AssertionError, match="^turn shape changed"):
        harness.check(leaky)


# --- load_settings_json_obj_fuzzer ---------------------------------------------------


_SETTINGS_BODIES = [
    b"",
    b"   \t\r\n ",
    b"{}",
    b'{"a": 1}',
    b'{"a": NaN}',
    b"[1]",
    b'"str"',
    b"null",
    b"not json",
    b'{"a": ',
    b"\xff\xfe",
    b'{"a": "\xff"}',
    b'\xef\xbb\xbf{"a": 1}',
    b'{"a":' + b"[" * 400 + b"]" * 400 + b"}",
]


@pytest.mark.parametrize("raw", _SETTINGS_BODIES)
def test_load_settings_json_obj_reference_matches(raw: bytes) -> None:
    """The harness's ``json.loads``-derived contract must agree with the shipped guard."""
    from clauster import config_write

    harness = _load("load_settings_json_obj_fuzzer.py")
    expected_ok, expected = harness._reference(raw)
    try:
        out = config_write.load_settings_json_obj(raw)
    except config_write.InvalidCandidateError:
        assert not expected_ok, raw
        return
    assert expected_ok, raw
    assert harness._normalized(out) == harness._normalized(expected), raw


def test_load_settings_json_obj_oracles_fire_on_a_broken_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break the guard on purpose; the harness must notice in both directions."""
    from clauster import config_write

    harness = _load("load_settings_json_obj_fuzzer.py")
    harness.check(b'{"a": 1}')  # unbroken: passes

    # Accepts what the contract rejects — a non-object reaching a caller that merges it.
    monkeypatch.setattr(config_write, "load_settings_json_obj", lambda raw: {"injected": True})
    with pytest.raises(AssertionError, match="^accepted an input the contract rejects"):
        harness.check(b"[1]")
    with pytest.raises(AssertionError, match="^value differs"):
        harness.check(b'{"a": 1}')

    def _always_reject(raw: bytes) -> dict:
        raise config_write.InvalidCandidateError("nope")

    monkeypatch.setattr(config_write, "load_settings_json_obj", _always_reject)
    with pytest.raises(AssertionError, match="^rejected an input the contract accepts"):
        harness.check(b'{"a": 1}')


# --- parse_frontmatter_fuzzer --------------------------------------------------------


def test_parse_frontmatter_header_oracle_fires_on_a_broken_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the two parsers disagree on the header; the harness must notice.

    The header is the asserted half — it is where a subagent's ``tools`` and a skill's
    ``allowed-tools`` come from, so two parsers reading one file into two different grants
    is the divergence with consequences.
    """
    from clauster import config_write_skills

    harness = _load("parse_frontmatter_fuzzer.py")
    text = "---\nname: a\ndescription: d\n---\nbody\n"
    harness.check(text)  # unbroken: passes

    monkeypatch.setattr(
        config_write_skills,
        "parse_frontmatter",
        lambda content: ({"name": "SOMETHING ELSE"}, "body\n"),
    )
    with pytest.raises(AssertionError, match="^drift: headers differ"):
        harness.check(text)


def test_parse_frontmatter_bodies_no_longer_diverge() -> None:
    """The two parsers agree on the BODY for a fence with trailing whitespace (#1352).

    Was pinned here as an open finding: subagents' fence pattern ended ``---[ \\t]*\\r?\\n?``
    and skills' ended ``---\\r?\\n?``, so anything trailing the closing ``---`` was swallowed
    by one parser and handed back as the start of the body by the other (``'body\\n'`` vs
    ``' \\nbody\\n'``) — and a file with a trailing space was accepted on one surface of the
    write tier and 422'd on the other. Both modules now alias ONE pattern object, which is
    what this asserts: convergence by construction, not by two copies agreeing today.
    """
    from clauster import config_write, config_write_skills, config_write_subagents

    assert config_write_subagents._FRONTMATTER_RE is config_write.FRONTMATTER_RE
    assert config_write_skills._FRONTMATTER_RE is config_write.FRONTMATTER_RE

    text = "---\ndescription: d\n--- \nbody\n"
    assert config_write_subagents.parse_frontmatter(text) == ({"description": "d"}, "body\n")
    assert config_write_skills.parse_frontmatter(text) == ({"description": "d"}, "body\n")


def test_parse_frontmatter_body_oracle_fires_on_a_broken_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the two parsers disagree on the BODY; the harness must notice.

    The body oracle only became assertable once #1352 converged the fences, so it needs the
    same proof-it-can-fire the header oracle has. The broken body is a genuine suffix of
    the input, so the per-parser suffix assertion passes and the *drift* assertion is what
    fires.
    """
    from clauster import config_write_skills

    harness = _load("parse_frontmatter_fuzzer.py")
    text = "---\nname: a\ndescription: d\n---\nbody\n"
    harness.check(text)  # unbroken: passes

    monkeypatch.setattr(
        config_write_skills,
        "parse_frontmatter",
        lambda content: ({"name": "a", "description": "d"}, "ody\n"),
    )
    with pytest.raises(AssertionError, match="^drift: bodies differ"):
        harness.check(text)


# --- hosted_redact_obj_fuzzer --------------------------------------------------------


def test_hosted_redact_obj_oracles_fire_on_a_broken_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break the frame redactor on purpose; the harness must notice each property."""
    from clauster import hosted

    harness = _load("hosted_redact_obj_fuzzer.py")
    frame = {"content": [{"text": "bridge session_01JABCDEFGHJKMNPQ"}]}
    harness.check(frame)  # unbroken: passes

    # 1. Leaks — the invariant-4 property, on a redactor that redacts nothing.
    monkeypatch.setattr(hosted, "_redact_obj", lambda obj, _depth=0: obj)
    with pytest.raises(AssertionError, match="^redaction leak in a hosted frame"):
        harness.check(frame)

    # 2. Depth cap — the identity redactor from step 1 is still installed, so nothing
    #    replaces the over-deep container with the too-deep marker.
    deep: object = "leaf"
    for _ in range(hosted._REDACT_MAX_DEPTH + 5):
        deep = [deep]
    with pytest.raises(AssertionError, match="^depth cap breached"):
        harness.check(deep)

    # 3. Shape — a redactor that drops a key.
    monkeypatch.setattr(
        hosted, "_redact_obj", lambda obj, _depth=0: {} if isinstance(obj, dict) else obj
    )
    with pytest.raises(AssertionError, match="^redaction changed the frame's shape"):
        harness.check({"keep": "me"})


def test_hosted_redact_obj_depth_helpers_are_iterative() -> None:
    """The harness's own walkers must survive what it generates.

    A recursive checker would overflow on exactly the over-deep frames this harness exists
    to build, and the crash would be the harness's rather than the target's — identical in
    the Security tab, and a wild goose chase.
    """
    harness = _load("hosted_redact_obj_fuzzer.py")
    deep: object = "leaf"
    for _ in range(5000):
        deep = [deep]
    assert harness._depth(deep) == 5000
    assert sum(1 for _ in harness._values(deep)) == 5001


# --- hosted_instance_from_record_fuzzer ----------------------------------------------


def test_hosted_instance_from_record_round_trip_oracle_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break the record projection so it is no longer the mapper's inverse.

    ``_instance_from_record`` documents itself as the "inverse of ``_record``", and the
    harness holds it to that as a fixed point from the first pass onward. Breaking
    ``_record`` so it emits a fresh value each call makes the second pass disagree.
    """
    from clauster import hosted

    harness = _load("hosted_instance_from_record_fuzzer.py")
    record = {"project": "p", "label": "L", "daemon_last_seq": 4, "instance_id": "keep-me"}
    harness.check(record)  # unbroken: passes

    original = hosted.HostedManager._record
    counter = iter(range(1000))

    def _drifting(instance):
        out = original(instance)
        out["daemon_last_seq"] = next(counter)
        return out

    monkeypatch.setattr(hosted.HostedManager, "_record", staticmethod(_drifting))
    with pytest.raises(AssertionError, match="^record round trip is not stable"):
        harness.check(record)


def test_hosted_instance_from_record_seq_oracle_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative replay cursor must fail the harness.

    ``_coerce_seq`` clamps to 0 so ``_note_replay_gap`` cannot report an eviction range for
    frames that never existed; this proves the assertion guarding that can fire.
    """
    from clauster import hosted

    harness = _load("hosted_instance_from_record_fuzzer.py")
    monkeypatch.setattr(hosted, "_coerce_seq", lambda value: -5)
    with pytest.raises(AssertionError, match="^negative replay cursor"):
        harness.check({"daemon_last_seq": -5})


def test_hosted_instance_from_record_generator_reaches_the_iso_branch() -> None:
    """The composed timestamps must actually parse, or the accept branch is never fuzzed.

    ``datetime.fromisoformat`` is implemented in C, so this shows up as no ``cov:`` gain at
    all — which is exactly why it is asserted here rather than inferred from edge counts.
    """
    import datetime

    import atheris

    harness = _load("hosted_instance_from_record_fuzzer.py")
    parsed = 0
    for seed in range(300):
        fdp = atheris.FuzzedDataProvider(seed.to_bytes(2, "big") * 60)
        stamp = harness._timestamp(fdp)
        try:
            datetime.datetime.fromisoformat(stamp)
        except ValueError:
            continue
        parsed += 1
    assert parsed > 0, "no generated timestamp ever parsed — the accept branch is dead"


# --- is_session_not_found_fuzzer -----------------------------------------------------


_NOT_FOUND_BODIES = [
    b"",
    b"not json",
    b"[]",
    b"{}",
    b'{"error": {"type": "not_found_error", "resource_type": "session"}}',
    b'{"type": "not_found_error", "resource_type": "session"}',
    b'{"error": {"type": "not_found_error", "resource_type": "environment"}}',
    b'{"error": {"type": "invalid_request_error", "message": "session gone"}}',
    b"<!DOCTYPE html>404",
    b"\xff\xfe",
    b'{"a":' + b"[" * 400 + b"]" * 400 + b"}",
]


@pytest.mark.parametrize("raw", _NOT_FOUND_BODIES)
def test_is_session_not_found_is_total_and_fails_closed(raw: bytes) -> None:
    """Never raises, always a bool, and a ``True`` is always evidenced by the body."""
    harness = _load("is_session_not_found_fuzzer.py")
    harness.check(raw)


def test_is_session_not_found_oracle_fires_on_a_broken_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the check answer ``True`` for a route-level 404; the harness must notice.

    That is the direction with consequences: a ``True`` clears a healthy bridge pointer,
    and the whole reason this function requires BOTH signals is to keep a moved endpoint
    from doing it across every project at once.
    """
    from clauster import code_sessions

    harness = _load("is_session_not_found_fuzzer.py")
    route_404 = b'{"error": {"type": "invalid_request_error", "message": "session gone"}}'
    harness.check(route_404)  # unbroken: passes

    monkeypatch.setattr(code_sessions, "_is_session_not_found", lambda raw: True)
    with pytest.raises(AssertionError, match="^said not-found with no not_found_error"):
        harness.check(route_404)
    with pytest.raises(AssertionError, match="^said not-found for a body it could not parse"):
        harness.check(b"not json")


# --- pty_screen_feed_fuzzer ----------------------------------------------------------


def test_pty_screen_feed_invariance_oracle_fires_on_a_broken_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break the OSC 8 chunk carry; the harness must notice.

    The oracle is a differential between two runs of the same code under different
    chunkings, so it can only fire if the code is made chunk-DEPENDENT. Dropping the carry
    does exactly that: a hyperlink split across two feeds is then lost, while the same
    bytes fed whole are still found.
    """
    pty_screen = pytest.importorskip("clauster.pty_screen")
    pytest.importorskip("pyte")

    harness = _load("pty_screen_feed_fuzzer.py")
    url = b"https://claude.com/cai/oauth/authorize?client_id=abc"
    # The real shape `claude setup-token` emits, closing sequence included. That closer is
    # a second `\x1b]8`, which an earlier opener-COUNT skip in the harness excluded — the bug
    # that made this oracle vacuous. No exclusion survives (#1356 removed the last one), so
    # the invariance assertion really does apply to the shape production produces.
    stream = b"\x1b]8;;" + url + b"\x07label\x1b]8;;\x07"
    mid = [len(stream) // 2 * 256 // len(stream)]
    harness.check(stream, mid, 80, 24, True)  # unbroken: passes

    def _no_carry(self, data: bytes) -> None:
        for found in pty_screen.extract_osc8_hyperlinks(data):
            if found.startswith("https://") and found not in self._osc8_seen:
                self._osc8_seen.add(found)
                self._osc8_urls.append(found)

    monkeypatch.setattr(pty_screen.PtyScreen, "_scan_osc8", _no_carry)
    with pytest.raises(AssertionError, match="^chunk-boundary divergence in 'retained'"):
        harness.check(stream, mid, 80, 24, True)


def test_pty_screen_feed_leak_oracle_fires_on_a_broken_redactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break screen redaction; the harness must notice the bare identifier."""
    pytest.importorskip("pyte")
    from clauster import redact

    harness = _load("pty_screen_feed_fuzzer.py")
    row = b"bridge cse_01JABCDEFGHJKMNPQ\r\n"
    harness.check(row, [], 80, 24, False)  # unbroken: passes

    monkeypatch.setattr(redact, "redact_screen_text", lambda rows: list(rows))
    with pytest.raises(AssertionError, match="^redaction leak in a rendered row"):
        harness.check(row, [], 80, 24, False)


def test_pty_screen_feed_raises_on_ordinary_escape_sequences() -> None:
    """PIN: ``PtyScreen.feed`` raises on sequences a real terminal emits.

    ``pty_screen_feed_fuzzer`` catches these because both production call sites already do
    ("a render hiccup must never kill the reader") — but the guard is not free: the
    ``pty_keeper`` handler disables the live view AND the pyte connect-URL scrape for the
    rest of the session. Reported as an open finding; pinned here so a ``pyte`` upgrade
    that fixes either defect fails ``just check`` and prompts narrowing the harness's
    ``except`` rather than leaving it swallowing nothing.
    """
    pty_screen = pytest.importorskip("clauster.pty_screen")
    pytest.importorskip("pyte")

    with pytest.raises(TypeError):  # CSI arity: a modified cursor key
        pty_screen.PtyScreen(cols=80, rows=24).feed(b"\x1b[1;2C")
    with pytest.raises(UnboundLocalError):  # out-of-range erase-in-display
        pty_screen.PtyScreen(cols=80, rows=24).feed(b"\x1b[4J")


def test_pty_screen_display_readers_raise_on_a_half_overwritten_wide_char() -> None:
    """PIN: the screen READERS raise, and that path is not guarded in production.

    ``login_shepherd`` calls ``flow.screen.find_oauth_token()`` and ``find_authorize_url()``
    unwrapped, so this reaches the caller on the credential path. Reported as an open
    finding and caught in the harness so it can still assert everything else; pinned here
    so the fix flips the suite red rather than passing unnoticed.
    """
    pty_screen = pytest.importorskip("clauster.pty_screen")

    pytest.importorskip("pyte")
    screen = pty_screen.PtyScreen(cols=40, rows=6)
    screen.feed(b"\x1bH\xad\x80\xe6\x80\xa0\x1b[H\xad\x80\xae")
    with pytest.raises(IndexError):
        screen.find_authorize_url()


def test_pty_screen_frame_width_refit_cannot_expose_an_identifier() -> None:
    """Regression (#1359): the width re-fit must not shear an identifier into a frame.

    Driven through ``PtyScreen.frame()`` itself, deliberately: an earlier version of this
    test hand-built the row and sliced it, which meant a fix inside ``frame`` would have left
    it green either way — it proved nothing about the delivered frame.

    Mechanism: masking ``session_ABCDEF`` *lengthens* the row by 4 characters, so trimming
    back to ``cols`` shears the ``_zzz`` off ``cse_ABCDEFGH_zzz`` and manufactures the word
    boundary ``redact._ID_RE`` needs. The id was correctly not masked while it ran on; the
    trim is what exposed it (safety invariant 4). ``frame`` now redacts the fitted row again,
    so the delivered row carries the mask instead — and ``pty_screen_feed_fuzzer`` asserts the
    leak property on every delivered row rather than exempting the shortened ones.
    """
    import re

    pty_screen = pytest.importorskip("clauster.pty_screen")
    pytest.importorskip("pyte")

    cols = 40
    row = "session_ABCDEF yyyyyyyy cse_ABCDEFGH_zzz"
    assert len(row) == cols, "the row must fill the screen exactly for the shear to happen"
    screen = pty_screen.PtyScreen(cols=cols, rows=3)
    screen.feed(row.encode())

    delivered = screen.frame()["rows"][0]
    leak = re.compile(r"\b(env|session|cse)_[A-Za-z0-9]{6,}\b")
    assert not leak.search(delivered), f"frame() exposed a bare identifier: {delivered!r}"
    assert "session_<redacted>" in delivered, "the lengthening mask is what shears the row"
    assert len(delivered) == cols, "the row must still be exactly one screen wide"
    # The second pass masked the sheared tail, and the final trim cut inside that mask —
    # `<` where `_ID_RE` needs an alphanumeric, which is why no third pass is needed.
    assert delivered.endswith("cse_<redacte"), delivered


def test_usage_line_to_turn_timestamp_is_still_unsanitized() -> None:
    """PIN: ``_line_to_turn`` returns ``timestamp`` unredacted, against its own docstring.

    The docstring promises ``{role, content, model, timestamp}`` "with every free-text
    field passed through :func:`redact.sanitize_line`", but the timestamp is returned as-is
    — so an identifier in that field reaches the browser. ``usage_line_to_turn_fuzzer``
    therefore asserts its leak property over ``_SANITIZED_FIELDS`` only. Reported as an open
    finding rather than accepted; this pin fails the moment the field is wrapped, which is
    the signal to add ``timestamp`` back to that tuple.
    """
    from clauster import usage

    ident = "session_01JABCDEFGHJKMNPQ"
    turn = usage._line_to_turn(
        '{"message": {"role": "user", "content": "' + ident + '"}, "timestamp": "' + ident + '"}'
    )
    assert turn is not None
    assert turn["content"] == "session_<redacted>", "content redaction changed"
    assert turn["timestamp"] == ident, "timestamp is now sanitized — widen _SANITIZED_FIELDS"


def test_yaml_tags_are_inside_the_frontmatter_contract() -> None:
    """Four explicit YAML tags used to raise non-``YAMLError`` out of both parsers (#1354).

    ``!!int``/``!!float`` raise ``ValueError``, ``!!bool`` a ``KeyError`` and
    ``!!timestamp`` an ``AttributeError`` (``construct_yaml_timestamp`` calls
    ``.groupdict()`` on an unchecked ``re.match``) — none of them a ``YAMLError``, so each
    escaped both parsers' documented "only ``InvalidCandidateError``" contract and produced
    a 500 on the code-executing write tier for a file that arrived with a cloned repo.
    Same class the parser-contract work closed for ``RecursionError``.

    ``parse_frontmatter_fuzzer`` used to *skip* inputs carrying one; now that the escape is
    closed it asserts on them like any other input, so this checks both halves — the
    parsers reject cleanly, and the harness no longer carries the exclusion.
    """
    from clauster import config_write, config_write_skills, config_write_subagents

    expected = {
        "!!int": ValueError,
        "!!float": ValueError,
        "!!bool": KeyError,
        "!!timestamp": AttributeError,
    }
    harness = _load("parse_frontmatter_fuzzer.py")
    assert not hasattr(harness, "_YAML_UNCONTRACTED_TAGS"), (
        "the harness exclusion is back — these tags are contracted, so it would hide the "
        "next escape class instead of the known one"
    )

    for tag, cause in expected.items():
        text = f"---\nname: {tag} x\ndescription: d\n---\nbody\n"
        for parser in (
            config_write_subagents.parse_frontmatter,
            config_write_skills.parse_frontmatter,
        ):
            with pytest.raises(config_write.InvalidCandidateError) as excinfo:
                parser(text)
            assert isinstance(excinfo.value.__cause__, cause)
        harness.check(text)  # asserted on now, not skipped


def test_pyte_render_is_still_chunk_dependent() -> None:
    """PIN: pyte drops the rest of a read after a character it cannot place.

    ``pty_screen_feed_fuzzer`` asserts chunk-boundary invariance only over the OSC 8
    reassembly (``_INVARIANT_KEYS``), because the pyte-rendered readers are not invariant.
    That carve-out is the widest in the harness, so the three triggers behind it are pinned
    here — if pyte fixes any of them, this fails and the reader set should be widened.

    Not cosmetic: the last case is an authorize URL that a single-read delivery loses
    entirely and a split delivery finds.
    """
    pty_screen = pytest.importorskip("clauster.pty_screen")
    pytest.importorskip("pyte")

    def render(chunks: list[bytes]) -> str:
        screen = pty_screen.PtyScreen(cols=80, rows=6, capture_osc8=True)
        for chunk in chunks:
            screen.feed(chunk)
        return screen._screen.display[0].rstrip()

    for label, payload in (
        ("C0 control", b"\x03."),
        ("C1 control", b"\xc2\x93."),
        ("combining mark", b"\xde\xa7A"),
    ):
        whole = render([payload])
        split = render([payload[:-1], payload[-1:]])
        assert whole != split, f"{label}: pyte is now chunk-invariant — widen _INVARIANT_KEYS"

    url = b"https://claude.com/cai/oauth/authorize?client_id=abc"
    payload = b"see \x03" + url + b"\r\n"

    def authorize(chunks: list[bytes]) -> str | None:
        screen = pty_screen.PtyScreen(cols=200, rows=6, capture_osc8=True)
        for chunk in chunks:
            screen.feed(chunk)
        return screen.find_authorize_url()

    assert authorize([payload]) is None, "the single-read loss is fixed — widen the harness"
    assert authorize([payload[i : i + 1] for i in range(len(payload))]) == url.decode()


def test_osc8_regex_does_not_swallow_a_later_opener() -> None:
    """Regression (#1356): a stray opener must not eat the real hyperlink that follows it.

    ``_OSC8_RE``'s parameter run was ``[^;]*``, which excludes ``;`` but admits ESC, so an
    unterminated ``ESC]8;`` swallowed the next opener into its own parameters and matched the
    URI one character early. Fed as one read the real hyperlink was then dropped — the
    extracted URI gained a leading ``;`` and failed ``_scan_osc8``'s ``https://`` filter, so
    the operator was shown no link — while fed byte by byte the carry restarted at the last
    opener and the URL came back. On the Windows/ConPTY path the OSC 8 target is the only
    recoverable copy of the authorize URL, so a login worked or died on where a ``read()``
    landed. The parameter run now excludes ESC/BEL/CR/LF, ending a stray opener at the next
    escape; this is the exact reproducer from the issue, and the chunk-invariance it broke.
    """
    pty_screen = pytest.importorskip("clauster.pty_screen")
    pytest.importorskip("pyte")

    url = "https://claude.com/cai/oauth/authorize?client_id=abc"
    stream = b"\x1b]8;junk" + b"\x1b]8;;" + url.encode() + b"\x07label\x1b]8;;\x07"

    assert pty_screen.extract_osc8_hyperlinks(stream) == [url], "the stray opener still swallows"

    def retained(chunks: list[bytes]) -> list[str]:
        screen = pty_screen.PtyScreen(cols=200, rows=6, capture_osc8=True)
        for chunk in chunks:
            screen.feed(chunk)
        return list(screen._osc8_urls)

    # The property the harness's invariance oracle asserts: same bytes, any chunking, same
    # answer. Both drives now find the real URL; before the fix the whole-read drive found
    # nothing, which is why these inputs had to be exempted from that oracle.
    assert retained([stream]) == [url]
    assert retained([stream[i : i + 1] for i in range(len(stream))]) == [url]


def test_parse_frontmatter_handles_a_fence_leading_header() -> None:
    """A header whose first line itself starts with ``---`` reaches the tag, and rejects.

    While the tag escape was open the harness had to *skip* such inputs, and the skip's
    first implementation missed this one: a ``text.partition("\\n---")`` head truncated to
    the opening fence, so the ``!!int`` slipped past and raised the very ``ValueError`` the
    exclusion existed to keep out of the Security tab. #1354 removed the exclusion
    entirely, so the input is now parsed like any other — and rejected inside the contract.
    Kept as a regression because it is the shape that defeated the heuristic.
    """
    from clauster import config_write, config_write_skills, config_write_subagents

    harness = _load("parse_frontmatter_fuzzer.py")
    tricky = "---\n---x: 1\nk: !!int z\n---\nbody\n"

    assert "!!int" not in tricky.partition("\n---")[0], "the old heuristic's hole is gone"
    for parser in (
        config_write_subagents.parse_frontmatter,
        config_write_skills.parse_frontmatter,
    ):
        with pytest.raises(config_write.InvalidCandidateError):
            parser(tricky)
    harness.check(tricky)  # asserted on, not skipped — and nothing escapes


def test_pty_screen_feed_seeds_decode_to_a_non_empty_payload() -> None:
    """Every seed must survive `TestOneInput`'s envelope with a payload left over.

    A seed for this harness is not a raw pty capture: one leading byte is consumed as
    ``capture_osc8`` and a trailer feeds the geometry and cut draws. A capture pasted in
    unwrapped loses its leading ``ESC`` and quietly stops being an OSC 8 seed. This asserts
    the framing still holds for every file in the corpus.
    """
    import atheris

    harness = _load("pty_screen_feed_fuzzer.py")
    seeds = sorted((_FUZZ_DIR / "seeds" / "pty_screen_feed_fuzzer").iterdir())
    assert seeds, "the pty_screen_feed seed corpus went missing"

    for seed in seeds:
        raw = seed.read_bytes()
        fdp = atheris.FuzzedDataProvider(raw)
        fdp.ConsumeBool()
        fdp.ConsumeIntInRange(0, len(harness._GEOMETRIES) - 1)
        reserve = min(fdp.remaining_bytes(), harness._MAX_CUTS)
        payload = fdp.ConsumeBytes(
            min(max(fdp.remaining_bytes() - reserve, 0), harness._MAX_BYTES)
        )
        if seed.name == "empty":
            assert payload == b"", "the empty seed is deliberately empty"
            continue
        assert payload, f"{seed.name} decodes to an empty payload — check its envelope"
        assert len(payload) >= len(raw) - 10, (
            f"{seed.name} lost more than the envelope: {len(raw)} bytes in, {len(payload)} out"
        )
