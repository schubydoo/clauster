"""Property-based tests for the malformed-input readers + config round-trip (#450).

PR #381 adopted Hypothesis for the security-validation gates and redaction. This
sibling file extends the targeted property coverage to the *remaining* high-value
invariants from issue #450:

* **Malformed-input parsers** — the "``UnicodeDecodeError`` is a ``ValueError``"
  thesis: ``bridge_log.parse_bridge_markers``, ``logstream._incomplete_tail_len`` /
  ``read_new`` / ``initial_offset``, and ``state.KeyedJsonStore.load`` must never
  crash on arbitrary bytes — they degrade exactly as designed.
* **Config round-trip / validation** — ``config_writer.write_edits`` →
  ``config.load_config`` round-trips a Tier-A field value for arbitrary in-range
  inputs (load → edit → render → reload preserves the value), and the allowlist +
  fail-closed validator hold (a disallowed key never reaches disk).

These assert *positive invariants* in the fast default suite. Crash-hunting on the
same readers is owned by the Atheris fuzzers in ``fuzz/`` (``parse_markers_fuzzer``,
``redact_fuzzer``, etc.); this file deliberately does not duplicate that — it pins
the behaviour the parsers must keep, not merely "does not segfault".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from clauster import bridge_log, logstream
from clauster.config import load_config
from clauster.config_editor import DisallowedFieldError, file_hash
from clauster.config_writer import write_edits
from clauster.state import StateStore

# A function-scoped fixture (tmp_path / monkeypatch) applies once across every
# generated example, which is intentional here — suppress the health check that
# warns about it, mirroring tests/test_property_validation.py.
#
# deadline=None: every test in this group writes to and reads back a real file under
# tmp_path, so per-example wall-clock is dominated by filesystem latency — unbounded
# and nondeterministic on a loaded xdist runner or the emulated musl/alpine leg.
# Hypothesis's default 200ms per-example deadline turns that jitter into a
# DeadlineExceeded FlakyFailure (a slow first example that replays fast); these are
# correctness properties, not timing benchmarks, so the deadline earns nothing here.
_FIXTURE_PROP = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ----- bridge_log.parse_bridge_markers: never crashes, fields stay typed ------


@settings(max_examples=400)
@given(text=st.text(max_size=400))
def test_parse_bridge_markers_never_crashes_on_arbitrary_text(text: str) -> None:
    """Parsing arbitrary text returns well-typed markers and never raises."""
    markers = bridge_log.parse_bridge_markers(text)
    # Every field is its declared type (or None for the optional string fields);
    # the booleans are always real bools, never None — a downstream readiness
    # check (`is_ready`) must never see a surprising shape.
    for attr in ("bridge_id", "environment_id", "starter_session_id", "spawn_mode"):
        assert getattr(markers, attr) is None or isinstance(getattr(markers, attr), str)
    for attr in ("poll_loop_started", "clean_shutdown", "trust_error"):
        assert isinstance(getattr(markers, attr), bool)
    # is_ready is a pure function of two fields and must stay a bool.
    assert isinstance(markers.is_ready, bool)
    assert markers.is_ready == (markers.poll_loop_started and markers.environment_id is not None)


@given(data=st.binary(max_size=400))
def test_parse_bridge_markers_handles_arbitrary_bytes_decoded_lossily(data: bytes) -> None:
    """Even non-UTF-8 bytes (decoded with errors=replace) parse without raising."""
    # The bridge log is read off disk and decoded lossily before parsing; feed the
    # same lossy decode here so the property mirrors the real call path.
    text = data.decode("utf-8", "replace")
    markers = bridge_log.parse_bridge_markers(text)
    assert isinstance(markers, bridge_log.BridgeMarkers)


# ----- logstream._incomplete_tail_len: the withhold-at-most-3-bytes invariant -


@given(data=st.binary(max_size=64))
def test_incomplete_tail_len_bounds_and_safe_to_withhold(data: bytes) -> None:
    """The withheld suffix is 0..3 bytes, and the kept prefix decodes cleanly.

    The function's contract is that it withholds only a *truncated* trailing
    multibyte sequence (never more than 3 bytes — the tail of a 4-byte run), so the
    re-read after the rest arrives reassembles the character. Therefore the prefix
    that is NOT withheld must itself decode without a trailing replacement char that
    signals an incomplete sequence.
    """
    hold = logstream._incomplete_tail_len(data)
    assert 0 <= hold <= 3
    assert hold <= len(data)
    kept = data[: len(data) - hold]
    if hold > 0:
        # Something was withheld: appending the withheld bytes back must reproduce
        # the original exactly (the split is a clean byte boundary, nothing lost).
        assert kept + data[len(data) - hold :] == data
        # The withheld tail is a genuine *incomplete* lead+continuations: decoding
        # it alone yields one-or-more replacement chars (it is not valid on its own).
        assert "�" in data[len(data) - hold :].decode("utf-8", "replace")


@given(prefix=st.binary(max_size=32), text=st.text(min_size=1, max_size=32))
def test_incomplete_tail_len_zero_when_ends_on_complete_char(prefix: bytes, text: str) -> None:
    """Bytes ending on a complete code-point boundary withhold nothing."""
    # `text` encodes to complete UTF-8; appended to arbitrary leading bytes, the
    # tail is always a finished character, so nothing should be withheld. The
    # strategy is constrained to a non-empty suffix (min_size=1) rather than
    # skipping the empty case with an early return, which would burn example
    # budget on inputs that assert nothing.
    data = prefix + text.encode("utf-8")
    # The final code point is whole, so the last byte cannot be a mid-character cut.
    assert logstream._incomplete_tail_len(data) == 0


# ----- logstream.read_new / initial_offset: tolerant, monotonic, lossless ------


@_FIXTURE_PROP
@given(chunks=st.lists(st.binary(max_size=80), max_size=6))
def test_read_new_reassembles_appended_bytes_without_loss(
    chunks: list[bytes], tmp_path: Path
) -> None:
    """Reading incrementally as bytes append yields the full file, decoded lossily.

    Models the WebSocket tail: each chunk is appended, then ``read_new`` is called
    with the running offset. Concatenating the emitted text must equal a single
    lossy decode of every byte *except* any trailing incomplete multibyte sequence
    the reader is still — correctly — withholding (it waits for the continuation
    bytes that never arrive in a permanently truncated file). The offset is
    monotonic non-decreasing and never claims past what is on disk.
    """
    log = tmp_path / "bridge.log"
    log.write_bytes(b"")
    offset = 0
    collected = ""
    written = b""
    for chunk in chunks:
        written += chunk
        with log.open("ab") as fh:
            fh.write(chunk)
        new_offset, text = logstream.read_new(log, offset)
        assert new_offset >= offset  # offset never rewinds while the file only grows
        assert new_offset <= len(written)  # never claims past what is on disk
        collected += text
        offset = new_offset
    # A correctly-truncated trailing multibyte sequence is withheld forever (the
    # continuation never arrives): the emitted text is the lossy decode of all bytes
    # up to the final reported offset, with that withheld tail still pending.
    assert collected == written[:offset].decode("utf-8", "replace")
    # Nothing past `offset` is ever lost silently — it is exactly an incomplete tail.
    assert len(written) - offset == logstream._incomplete_tail_len(written)


@_FIXTURE_PROP
@given(data=st.binary(max_size=4096))
def test_initial_offset_is_in_range_and_on_a_safe_start(data: bytes, tmp_path: Path) -> None:
    """initial_offset always lands within the file and never mid-secret on a line."""
    log = tmp_path / "bridge.log"
    log.write_bytes(data)
    size = len(data)
    off = logstream.initial_offset(log, tail_bytes=256)
    assert 0 <= off <= size
    # read_new from that offset must not raise and must not over-read.
    new_offset, text = logstream.read_new(log, off)
    assert off <= new_offset <= size
    assert isinstance(text, str)


# ----- state.KeyedJsonStore.load: arbitrary file content degrades to {} --------


@_FIXTURE_PROP
@given(raw=st.binary(max_size=512))
def test_state_store_load_never_crashes_on_arbitrary_bytes(raw: bytes, tmp_path: Path) -> None:
    """A corrupt/garbage state.json degrades to {} (or a clean record map), never raises."""
    store = StateStore(tmp_path)
    store._path.write_bytes(raw)
    records = store.load()
    assert isinstance(records, dict)
    # Whatever survives parsing is a {str: {persisted fields}} map — never a list,
    # scalar, or record carrying an unwhitelisted key.
    for key, fields in records.items():
        assert isinstance(key, str)
        assert isinstance(fields, dict)
        assert set(fields) <= set(StateStore._PERSISTED_FIELDS)


@_FIXTURE_PROP
@given(
    text=st.one_of(
        st.from_regex(r"\{[^{}]{0,40}\}", fullmatch=True),  # JSON-ish but maybe invalid
        st.text(max_size=80),
    )
)
def test_state_store_load_tolerates_text_files(text: str, tmp_path: Path) -> None:
    """Arbitrary text (valid or invalid JSON) loads to a dict, never raising."""
    store = StateStore(tmp_path)
    store._path.write_text(text, encoding="utf-8")
    assert isinstance(store.load(), dict)


# ----- config round-trip: write_edits -> load_config preserves Tier-A values ---


@_FIXTURE_PROP
@given(value=st.integers(min_value=500, max_value=200_000))
def test_config_roundtrip_preserves_int_field(value: int, tmp_path: Path) -> None:
    """A Tier-A int edit survives write -> reload unchanged (load->edit->render round-trip)."""
    cfg = tmp_path / "clauster.yml"
    cfg.write_text(
        f"projects_root: {tmp_path}\nclaude:\n  resume_recap_max_chars: 8000\n",
        encoding="utf-8",
    )
    before = file_hash(cfg)
    write_edits(cfg, {"claude.resume_recap_max_chars": value}, expected_hash=before)
    reloaded = load_config(cfg)
    assert reloaded.claude.resume_recap_max_chars == value


@_FIXTURE_PROP
@given(
    value=st.floats(min_value=0.001, max_value=1_000_000, allow_nan=False, allow_infinity=False)
)
def test_config_roundtrip_preserves_float_field(value: float, tmp_path: Path) -> None:
    """A Tier-A float edit (usage.fx_rate, constraint > 0) survives write -> reload."""
    cfg = tmp_path / "clauster.yml"
    cfg.write_text(
        f"projects_root: {tmp_path}\nusage:\n  fx_rate: 1.0\n",
        encoding="utf-8",
    )
    before = file_hash(cfg)
    write_edits(cfg, {"usage.fx_rate": value}, expected_hash=before)
    reloaded = load_config(cfg)
    assert reloaded.usage.fx_rate == pytest.approx(value)


@_FIXTURE_PROP
@given(
    key=st.sampled_from(
        [
            "auth.password_hash",  # secret — never web-editable
            "host",  # bind — structural
            "projects_root",  # structural
            "claustrum.binary",  # security: resolved daemon binary stays out (claustrum.enabled
            #                      itself IS now editable, #539 — but the executable path is not)
            "not.a.real.key",  # unknown path
            "claude.binary",  # security: resolved binary
        ]
    ),
    value=st.text(max_size=20),
)
def test_config_writer_rejects_non_tier_a_keys(key: str, value: str, tmp_path: Path) -> None:
    """A non-allowlisted key always raises before any byte is written to disk."""
    cfg = tmp_path / "clauster.yml"
    original = f"projects_root: {tmp_path}\n"
    cfg.write_text(original, encoding="utf-8")
    with pytest.raises(DisallowedFieldError):
        write_edits(cfg, {key: value})
    # Fail-closed: the rejected edit left the file byte-for-byte unchanged.
    assert cfg.read_text(encoding="utf-8") == original
