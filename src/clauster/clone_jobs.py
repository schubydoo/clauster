"""In-memory registry for async git-clone jobs and their live progress streams.

A clone runs in a worker thread (``provisioning.clone_project``); its progress
lines are parsed into ``{phase, percent}`` and fanned out to a watching
WebSocket via the job's :class:`asyncio.Queue`. Mirrors the runner's contract:
the registry is mutated only on the event loop, and the worker thread feeds
progress back through ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

JobStatus = Literal["running", "done", "error"]

# A trailing "42%" anywhere in a git progress line.
_PCT_RE = re.compile(r"(\d{1,3})%")
# The phase label that precedes the colon (local + ``remote:`` variants).
_PHASE_RE = re.compile(
    r"^(?:remote:\s*)?"
    r"(Enumerating|Counting|Compressing|Receiving|Resolving|Total|Updating)"
    r"[ A-Za-z]*"
)


def parse_progress(line: str) -> tuple[str | None, int | None]:
    """Extract a ``(phase, percent)`` pair from one git ``--progress`` line.

    Either element may be ``None`` when the line doesn't carry it; ``percent`` is
    clamped to ``0..100`` so a malformed line can't drive the bar out of range.
    """
    pct_match = _PCT_RE.search(line)
    percent = min(int(pct_match.group(1)), 100) if pct_match else None
    phase_match = _PHASE_RE.match(line.strip())
    phase = phase_match.group(1) if phase_match else None
    return phase, percent


@dataclass
class CloneJob:
    """One in-flight (or finished) clone, broadcasting progress to live watchers."""

    id: str
    name: str
    status: JobStatus = "running"
    phase: str = ""
    percent: int | None = None
    error_detail: str | None = None
    # One queue per live watcher; events fan out to all so two tabs watching the
    # same clone each get the full stream (a single shared queue would let them
    # steal each other's frames).
    _subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a watcher and return its private event queue."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Drop a watcher's queue (called when its WebSocket closes)."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def broadcast(self, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every current watcher; never blocks the caller."""
        for queue in self._subscribers:
            queue.put_nowait(event)  # unbounded queue: short-lived, small frames

    def progress_event(self) -> dict[str, Any]:
        """Return the current progress as a WS ``progress`` frame."""
        return {"type": "progress", "phase": self.phase, "percent": self.percent}

    def terminal_event(self) -> dict[str, Any]:
        """Return the final WS frame describing how the job ended."""
        return {"type": "done", "status": self.status, "error": self.error_detail}


class CloneJobManager:
    """Registry of clone jobs keyed by id (event-loop-only, like the runner)."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._jobs: dict[str, CloneJob] = {}

    def create(self, name: str) -> CloneJob:
        """Register a new running job for project ``name`` and return it."""
        job = CloneJob(id=uuid.uuid4().hex, name=name)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> CloneJob | None:
        """Return the job with ``job_id``, or ``None`` if unknown/already pruned."""
        return self._jobs.get(job_id)

    def push_progress(self, job: CloneJob, line: str) -> None:
        """Parse a git progress ``line`` into the job and enqueue a progress frame.

        Called on the event loop (via ``call_soon_threadsafe``) from the clone
        worker thread.
        """
        phase, percent = parse_progress(line)
        if phase:
            job.phase = phase
        if percent is not None:
            job.percent = percent
        job.broadcast(job.progress_event())

    def finish(self, job: CloneJob, *, error: str | None = None) -> None:
        """Mark ``job`` done (or errored) and broadcast its terminal frame."""
        job.status = "error" if error else "done"
        job.error_detail = error
        job.broadcast(job.terminal_event())

    def discard(self, job_id: str) -> None:
        """Drop a job from the registry (no-op if already gone)."""
        self._jobs.pop(job_id, None)
