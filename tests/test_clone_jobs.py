"""Clone-job registry + git-progress parsing (async clone feature)."""

from __future__ import annotations

import asyncio

import pytest

from clauster.clone_jobs import _CLONE_QUEUE_MAXSIZE, CloneJobManager, parse_progress


@pytest.mark.parametrize(
    ("line", "phase", "percent"),
    [
        ("Receiving objects:  42% (123/456), 1.2 MiB | 3.4 MiB/s", "Receiving", 42),
        ("Resolving deltas: 100% (10/10), done.", "Resolving", 100),
        ("remote: Compressing objects:   7% (1/14)", "Compressing", 7),
        ("Cloning into '/tmp/x'...", None, None),  # no phase, no percent
        ("Receiving objects: 250% (oops)", "Receiving", 100),  # clamped
    ],
)
def test_parse_progress(line, phase, percent):
    assert parse_progress(line) == (phase, percent)


def test_manager_lifecycle_and_queue():
    async def _run():
        mgr = CloneJobManager()
        job = mgr.create("proj")
        assert mgr.get(job.id) is job and job.status == "running"
        queue = job.subscribe()

        mgr.push_progress(job, "Receiving objects: 50% (5/10)")
        evt = await asyncio.wait_for(queue.get(), timeout=1)
        assert evt == {"type": "progress", "phase": "Receiving", "percent": 50}
        assert job.percent == 50  # snapshot updated on the job too

        mgr.finish(job)
        done = await asyncio.wait_for(queue.get(), timeout=1)
        assert done == {"type": "done", "status": "done", "error": None}
        assert job.status == "done"

        mgr.discard(job.id)
        assert mgr.get(job.id) is None

    asyncio.run(_run())


def test_manager_finish_error():
    async def _run():
        from clauster.redact import redact_for_disk

        mgr = CloneJobManager()
        job = mgr.create("proj")
        queue = job.subscribe()
        error_msg = "git clone failed: boom"
        mgr.finish(job, error=error_msg)
        evt = await asyncio.wait_for(queue.get(), timeout=1)
        # The WS frame carries the redacted form (a no-op for this secret-free literal); the
        # raw value stays on job.error_detail. Assert against redact_for_disk so a future
        # secret-bearing test string can't yield a false-positive (#574, per Greptile).
        assert evt == {"type": "done", "status": "error", "error": redact_for_disk(error_msg)}
        assert job.status == "error" and job.error_detail == error_msg

    asyncio.run(_run())


def test_terminal_event_redacts_error_detail():
    # #574: a failed clone's error_detail (git stderr tail) is redacted on the WS egress to
    # match the redacted clone-done webhook path (#432) — a secret-shaped token / id never
    # reaches the progress WS raw. Redacting in terminal_event() covers every consumer.
    from clauster.redact import redact_for_disk

    mgr = CloneJobManager()
    job = mgr.create("proj")
    job.status = "error"
    # A secret-shaped session id AND a bare UUID — both must be stripped from the frame.
    job.error_detail = (
        "git clone failed for session_0123456789abcdef0123456789abcdef "
        "(ref 3f2504e0-4f89-41d3-9a0c-0305e82c3301) denied"
    )
    frame = job.terminal_event()
    # The frame carries the redacted form, never the raw value.
    assert frame["error"] == redact_for_disk(job.error_detail)
    assert frame["error"] != job.error_detail  # redaction fired
    assert "session_0123456789abcdef0123456789abcdef" not in frame["error"]
    assert "3f2504e0-4f89-41d3-9a0c-0305e82c3301" not in frame["error"]


def test_broadcast_reaches_every_subscriber():
    async def _run():
        mgr = CloneJobManager()
        job = mgr.create("proj")
        q1, q2 = job.subscribe(), job.subscribe()  # two tabs watching the same clone
        mgr.push_progress(job, "Receiving objects: 10%")
        mgr.finish(job)
        for q in (q1, q2):  # both get the FULL stream, neither steals from the other
            prog = await asyncio.wait_for(q.get(), timeout=1)
            assert prog["type"] == "progress" and prog["percent"] == 10
            done = await asyncio.wait_for(q.get(), timeout=1)
            assert done["type"] == "done"
        job.unsubscribe(q1)
        mgr.push_progress(job, "Receiving objects: 99%")  # only q2 still subscribed
        with pytest.raises(asyncio.TimeoutError):  # q1 receives nothing within the window
            await asyncio.wait_for(q1.get(), timeout=0.1)
        assert (await asyncio.wait_for(q2.get(), timeout=1))["percent"] == 99

    asyncio.run(_run())


def test_progress_overflow_drops_and_marks_then_stays_bounded():
    # Item-7 (#408): a wedged consumer must not let broadcast grow a queue without
    # limit. Once the bounded queue fills, further progress frames are dropped; when the
    # consumer next makes room, the watcher gets an honest "overflow" marker carrying
    # the dropped count.
    async def _run():
        mgr = CloneJobManager()
        job = mgr.create("proj")
        queue = job.subscribe()
        # Flood far past the bound WITHOUT draining → the queue caps, excess dropped.
        for i in range(_CLONE_QUEUE_MAXSIZE + 50):
            mgr.push_progress(job, f"Receiving objects: {i % 100}%")
        assert queue.qsize() <= _CLONE_QUEUE_MAXSIZE  # never unbounded — the bound held
        # The consumer finally reads one frame (making room), then a new frame arrives:
        # offer() prepends the overflow marker honestly reporting how many were lost.
        queue.get_nowait()  # free one slot
        mgr.push_progress(job, "Receiving objects: 100%")
        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait())
        overflow = [e for e in drained if e.get("type") == "overflow"]
        assert overflow, "no overflow marker emitted after dropping frames"
        assert overflow[0]["dropped"] >= 1

    asyncio.run(_run())


def test_terminal_done_force_delivered_even_when_queue_full():
    # The consumer's read loop exits only on "done"; dropping it would hang the
    # watcher forever. So even a full queue must receive the terminal frame (an old
    # progress frame is evicted to make room).
    async def _run():
        mgr = CloneJobManager()
        job = mgr.create("proj")
        queue = job.subscribe()
        for i in range(_CLONE_QUEUE_MAXSIZE + 10):  # fill + overflow, never drained
            mgr.push_progress(job, f"Receiving objects: {i % 100}%")
        mgr.finish(job)  # terminal frame must still land
        # Scan everything the queue holds; a "done" frame MUST be present.
        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())
        assert any(f.get("type") == "done" for f in frames), "terminal frame was dropped"

    asyncio.run(_run())


def test_unsubscribe_unknown_queue_is_a_noop():
    async def _run():
        mgr = CloneJobManager()
        job = mgr.create("proj")
        live = job.subscribe()
        stray: asyncio.Queue = asyncio.Queue()  # never registered with this job
        job.unsubscribe(stray)  # not in _subscribers -> silently ignored, no raise
        # The genuinely-subscribed queue is untouched and still receives events.
        mgr.push_progress(job, "Receiving objects: 33%")
        assert (await asyncio.wait_for(live.get(), timeout=1))["percent"] == 33

    asyncio.run(_run())
