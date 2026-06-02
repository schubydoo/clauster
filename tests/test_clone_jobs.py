"""Clone-job registry + git-progress parsing (async clone feature)."""

from __future__ import annotations

import asyncio

import pytest

from clauster.clone_jobs import CloneJobManager, parse_progress


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
        mgr = CloneJobManager()
        job = mgr.create("proj")
        queue = job.subscribe()
        mgr.finish(job, error="git clone failed: boom")
        evt = await asyncio.wait_for(queue.get(), timeout=1)
        assert evt == {"type": "done", "status": "error", "error": "git clone failed: boom"}
        assert job.status == "error" and job.error_detail == "git clone failed: boom"

    asyncio.run(_run())


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
        assert q1.empty()
        assert (await asyncio.wait_for(q2.get(), timeout=1))["percent"] == 99

    asyncio.run(_run())
