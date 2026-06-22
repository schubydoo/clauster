"""Shared test helpers for the outbound webhook emitter wiring (#432).

The bg-settled / clone-done emission tests live in different files
(``test_app_routes.py`` and ``test_clone_pipeline.py``) but exercise the same
``runner._webhooks`` seam, so the recording stand-in and the poll helper live here
to keep one surface to patch when the ``WebhookEmitter`` protocol changes.
"""

from __future__ import annotations

import time


class RecordingEmitter:
    """Stand-in for ``runner._webhooks``: records (event, payload); always wants().

    Mirrors the ``WebhookEmitter`` surface the runner calls — ``active``, ``wants``,
    ``aemit`` — so swapping it in lets a test assert exactly what would egress without
    any real HTTP. ``wants`` is unconditionally True so the test drives the gate via
    which events it sends, not via config.
    """

    def __init__(self) -> None:
        """Start active with an empty call log."""
        self.active = True
        self.calls: list[tuple[str, dict]] = []

    def wants(self, event: str) -> bool:
        """Accept every event (the test controls what gets emitted)."""
        return True

    async def aemit(self, event: str, payload: dict) -> None:
        """Record one emitted (event, payload) pair."""
        self.calls.append((event, payload))


def wait_for_calls(rec: RecordingEmitter, *, timeout: float = 2.0) -> list[tuple[str, dict]]:
    """Poll ``rec.calls`` until non-empty or ``timeout`` elapses, then return it.

    The emit task runs on the app's event-loop thread (the TestClient drives a separate
    loop), so a sync test polls rather than awaits. Returns the (possibly still empty)
    list so the caller asserts on it directly.
    """
    deadline = time.monotonic() + timeout
    while not rec.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    return rec.calls


def assert_stays_empty(seq: list, *, window: float = 2.0) -> None:
    """Poll ``seq`` for ``window`` seconds, failing fast the instant it becomes non-empty.

    For a NEGATIVE assertion ("this must never emit"): a fixed ``sleep`` either flakes
    (too short under load) or wastes time (too long). This short-circuits with a clear
    failure the moment an unexpected item appears, and otherwise confirms silence across
    the whole window — robust on a loaded CI runner without a magic sleep.
    """
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        assert not seq, f"expected no items, got {seq!r}"
        time.sleep(0.01)
    assert not seq, f"expected no items, got {seq!r}"
