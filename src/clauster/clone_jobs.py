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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .claustrum_client import _Subscriber
from .redact import redact_for_disk

JobStatus = Literal["running", "done", "error", "cancelled"]

# Per-watcher queue depth. A clone emits at most a handful of progress phases, each a
# tiny frame, so this is generous headroom; the bound exists only so a wedged/slow
# WebSocket consumer can't make ``broadcast`` grow a queue without limit. On overflow a
# progress frame is dropped and the watcher gets an honest ``overflow`` marker; the
# terminal ``done`` frame is force-delivered (evicting an old frame if needed) so the
# consumer's read loop always sees it and exits rather than hanging forever.
_CLONE_QUEUE_MAXSIZE = 256

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
    # One bounded watcher per live viewer; events fan out to all so two tabs watching
    # the same clone each get the full stream (a single shared queue would let them
    # steal each other's frames). Bounded so a slow/wedged consumer can't grow a queue
    # without limit; overflow drops a progress frame and marks the gap.
    _subscribers: list[_Subscriber] = field(default_factory=list)
    # Cancellation (#573). The worker thread registers `_terminate` (the git
    # subprocess's terminate) once it spawns; cancelling sets `cancel_requested` AND
    # terminates the process so the transfer stops and its partial dir is cleaned up.
    # `repr`/`compare` off so the callable never lands in a log line or breaks equality.
    cancel_requested: bool = False
    _terminate: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def register_terminate(self, fn: Callable[[], None]) -> None:
        """Register the running clone's terminate hook (called by the worker on spawn).

        If a cancel already arrived before the process spawned, terminate immediately so
        the just-started transfer is stopped at once (closes the pre-spawn cancel race).
        """
        self._terminate = fn
        if self.cancel_requested:
            fn()

    def request_cancel(self) -> bool:
        """Request cancellation of an in-flight clone; return False if not cancellable.

        Sets ``cancel_requested`` and terminates the git subprocess if it has spawned
        (a terminated clone exits non-zero, so the worker tears down its partial temp
        dir). A no-op returning ``False`` once the job is terminal (done/error/cancelled).
        """
        if self.status != "running":
            return False
        self.cancel_requested = True
        if self._terminate is not None:
            self._terminate()
        return True

    def subscribe(self) -> asyncio.Queue[Mapping[str, Any]]:
        """Register a watcher and return its private (bounded) event queue."""
        sub = _Subscriber(queue=asyncio.Queue(maxsize=_CLONE_QUEUE_MAXSIZE))
        self._subscribers.append(sub)
        return sub.queue

    def unsubscribe(self, queue: asyncio.Queue[Mapping[str, Any]]) -> None:
        """Drop a watcher's queue (called when its WebSocket closes)."""
        self._subscribers = [s for s in self._subscribers if s.queue is not queue]

    def broadcast(self, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every current watcher; never blocks the caller.

        Progress frames are offered through the bounded drop-and-mark path. The
        terminal ``done`` frame is force-delivered — the consumer's read loop exits
        only on ``done``, so dropping it would hang the watcher forever; if a queue
        is full we evict its oldest frame to make room.
        """
        terminal = event.get("type") == "done"
        for sub in self._subscribers:
            if terminal:
                self._force_deliver(sub, event)
            else:
                sub.offer(event)  # bounded: drops + marks on overflow

    @staticmethod
    def _force_deliver(sub: _Subscriber, event: dict[str, Any]) -> None:
        """Enqueue ``event`` even on a full queue by evicting the oldest frame."""
        while True:
            try:
                sub.queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    sub.queue.get_nowait()  # drop the oldest to make room
                    sub.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - full-then-empty race
                    # Queue drained out from under us — retry the put_nowait, which now
                    # succeeds. `continue` (not `return`) so the terminal `done` frame
                    # is never silently dropped (that would hang the watcher's read
                    # loop, which exits only on `done`). Unreachable single-threaded.
                    continue

    def progress_event(self) -> dict[str, Any]:
        """Return the current progress as a WS ``progress`` frame."""
        return {"type": "progress", "phase": self.phase, "percent": self.percent}

    def status_snapshot(self) -> dict[str, Any]:
        """Return a small, safe summary of this job for the cross-tab status poll (#659).

        A second tab opened mid-clone reads this to discover the in-flight job's id and
        current progress, then reattaches to ``/ws/clone-progress/{id}`` for the live
        stream. The URL is deliberately omitted — it can carry credentials and must never
        leave the box; only ``name`` + ``phase`` + ``percent`` are surfaced.
        """
        return {
            "job_id": self.id,
            "name": self.name,
            "phase": self.phase,
            "percent": self.percent,
        }

    def terminal_event(self) -> dict[str, Any]:
        """Return the final WS frame describing how the job ended.

        ``error`` (a tail of git stderr) is run through :func:`redact_for_disk`, the
        same redaction the clone-done webhook applies (#432), so the progress-WS egress
        is symmetric — a secret-shaped token / id never leaves here raw. Redacting here
        (rather than at each call site) covers every consumer of this frame (#574).
        """
        # The None-guard is required, not cosmetic: redact_for_disk is typed `str` and
        # raises on None, and a successful clone's terminal frame has error_detail=None.
        error = redact_for_disk(self.error_detail) if self.error_detail else None
        return {"type": "done", "status": self.status, "error": error}


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

    def active_jobs(self) -> list[CloneJob]:
        """Return every still-running clone job (cross-tab reattach source, #659).

        Terminal jobs (done/error/cancelled) linger in the registry for ``_CLONE_JOB_TTL``
        so a reconnecting watcher can read their final frame, but a fresh tab must not
        treat a finished clone as "in progress" — so only ``running`` jobs are listed.
        """
        return [job for job in self._jobs.values() if job.status == "running"]

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

    def cancel(self, job: CloneJob) -> None:
        """Mark ``job`` cancelled and broadcast its terminal frame (#573).

        Called by the clone worker when its git subprocess was terminated by a cancel
        request, so the outcome reads as a clean ``cancelled`` rather than an ``error``.
        """
        job.status = "cancelled"
        job.error_detail = None
        job.broadcast(job.terminal_event())

    def discard(self, job_id: str) -> None:
        """Drop a job from the registry (no-op if already gone)."""
        self._jobs.pop(job_id, None)
