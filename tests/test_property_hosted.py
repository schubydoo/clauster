"""Property-based tests for the hosted-channel ring buffer + fan-out (#450).

The hosted live view keeps a bounded ring of redacted events and replays it to
each browser subscriber. Two safety invariants must hold for *any* event volume
and *any* reconnect cursor — they are what keep a reconnecting viewer's history
honest and bounded:

* **Replay never exceeds capacity.** A first-view subscriber receives at most
  ``ring_size`` real events plus at most one leading ``gap`` marker, regardless of
  how many events were emitted.
* **A gap marker is present exactly when the ring evicted past the cursor.** If the
  oldest retained event is newer than ``after_seq + 1``, the subscriber sees a
  ``gap`` first (so dropped history is reported, never silently lost); if nothing
  was evicted past the cursor, there is no spurious gap.

``_emit`` and ``subscribe`` are pure in-memory operations that never touch the
claustrum client, so these tests drive them directly on an unstarted
``HostedSession`` — fully synchronous, offline, and deterministic.
"""

from __future__ import annotations

from typing import Any, cast

from hypothesis import given
from hypothesis import strategies as st

from clauster.claustrum_client import ClaustrumClient
from clauster.hosted import HostedSession

# No platform gate (#914): `_emit`/`subscribe` are pure in-memory ops that never touch the
# claustrum client or any AF_UNIX socket, so the fan-out invariants run on every OS.

_PID = "01HOSTEDPROPTEST000000000"
_BIN = "/usr/bin/claude"


def _make_session(*, ring_size: int) -> HostedSession:
    """Build an unstarted session; _emit/subscribe never use the client, so None is safe."""
    return HostedSession(cast(ClaustrumClient, None), _PID, _BIN, ring_size=ring_size)


def _drain(queue: Any) -> list[dict]:
    """Synchronously drain every event currently queued (no event loop needed)."""
    out: list[dict] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


@given(
    ring_size=st.integers(min_value=1, max_value=32),
    n_events=st.integers(min_value=0, max_value=200),
    after_seq=st.integers(min_value=0, max_value=250),
)
def test_first_view_replay_is_bounded_by_ring_size(
    ring_size: int, n_events: int, after_seq: int
) -> None:
    """A late subscriber's replay is at most ring_size events plus one gap marker."""
    session = _make_session(ring_size=ring_size)
    for i in range(n_events):
        session._emit({"type": "frame", "n": i})
    queue = session.subscribe(after_seq=after_seq)
    replayed = _drain(queue)
    gaps = [e for e in replayed if e.get("type") == "gap"]
    non_gap = [e for e in replayed if e.get("type") != "gap"]
    assert len(gaps) <= 1  # at most one leading gap marker
    assert len(non_gap) <= ring_size  # replay can never exceed the ring capacity
    # Replayed real events are exactly the retained ones whose seq is past the cursor.
    expected = [e for e in session._ring if e["event_seq"] > after_seq]
    assert non_gap == expected


@given(
    ring_size=st.integers(min_value=1, max_value=32),
    n_events=st.integers(min_value=0, max_value=200),
    after_seq=st.integers(min_value=0, max_value=250),
)
def test_gap_marker_present_iff_ring_evicted_past_cursor(
    ring_size: int, n_events: int, after_seq: int
) -> None:
    """A gap marker appears exactly when the oldest retained event is past after_seq+1."""
    session = _make_session(ring_size=ring_size)
    for i in range(n_events):
        session._emit({"type": "frame", "n": i})
    queue = session.subscribe(after_seq=after_seq)
    replayed = _drain(queue)
    gaps = [e for e in replayed if e.get("type") == "gap"]
    evicted_past_cursor = bool(session._ring) and session._ring[0]["event_seq"] > after_seq + 1
    assert bool(gaps) == evicted_past_cursor
    if gaps:
        gap = gaps[0]
        assert replayed[0] is gap  # the gap is the FIRST thing the subscriber sees
        assert gap["from_seq"] == after_seq
        assert gap["to_seq"] == session._ring[0]["event_seq"]


@given(
    ring_size=st.integers(min_value=1, max_value=16),
    n_events=st.integers(min_value=0, max_value=120),
)
def test_ring_retains_only_the_newest_capacity_events(ring_size: int, n_events: int) -> None:
    """The ring holds at most ring_size events, and they are the most recent ones."""
    session = _make_session(ring_size=ring_size)
    for i in range(n_events):
        session._emit({"type": "frame", "n": i})
    ring = list(session._ring)
    assert len(ring) <= ring_size
    # event_seq is a strictly increasing 1-based stamp.
    seqs = [e["event_seq"] for e in ring]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    if n_events:
        assert ring[-1]["event_seq"] == n_events  # newest event is always retained
        assert ring[-1]["n"] == n_events - 1


@given(
    ring_size=st.integers(min_value=1, max_value=16),
    n_events=st.integers(min_value=1, max_value=120),
)
def test_first_view_replay_includes_the_newest_event(ring_size: int, n_events: int) -> None:
    """A first-view (after_seq=0) subscriber always gets the freshest retained event.

    The queue is sized to ``ring_size + 1`` precisely so a full, already-evicted
    ring replays in full (gap + every retained event) without the offer() overflow
    path dropping the newest event (#422). This pins that property.
    """
    session = _make_session(ring_size=ring_size)
    for i in range(n_events):
        session._emit({"type": "frame", "n": i})
    queue = session.subscribe(after_seq=0)
    replayed = _drain(queue)
    frames = [e for e in replayed if e.get("type") == "frame"]
    assert frames, "first-view replay must include at least one retained frame"
    assert frames[-1]["event_seq"] == session._ring[-1]["event_seq"]
    assert frames[-1]["n"] == n_events - 1  # the freshest event is never dropped
