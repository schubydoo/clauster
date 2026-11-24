"""Check that a call does not blow up, without a wall-clock bound on one run (TASK-552).

A fixed time limit fails on a slow or loaded runner and says nothing on a fast one. There
are two replacements, and the kind of work decides which one fits:

* :func:`count_ops` counts the Python lines a call runs. The count does not depend on the
  speed or the load of the machine (it can move a little with the Python and PyYAML
  versions, or with a first-use regex compile), so it fits a walk written in Python, such
  as a YAML alias pyramid. It cannot see work inside a C builtin.
* :func:`assert_linear_time` compares one run over a whole input with a batch of runs over
  equal parts of it. Both sides do the same work if the call is linear, so a slow machine
  slows both. It fits work in a regex or another C builtin.
"""

from __future__ import annotations

import gc
import sys
import time
from collections.abc import Callable
from types import FrameType
from typing import Any

import pytest


def count_ops(call: Callable[[], object], *, budget: int | None = None) -> int:
    """Return how many Python lines ``call`` runs, and fail the test past ``budget``.

    The failure is raised from inside the traced call, so a runaway walk stops at the budget
    and does not run to the suite timeout. It is a ``BaseException``, so a seam that maps
    ``Exception`` to a refusal cannot turn it into a pass, and the count is checked again
    after the call for a seam that swallows even that. Only this thread is traced. The
    failure carries no traceback or exception context, because a report that renders them
    can expand the same hostile input that the budget stopped (a pydantic error in the chain
    prints its input).
    """
    count = 0
    message = f"the call ran past its budget of {budget} Python lines"

    def trace(_frame: FrameType, event: str, _arg: Any) -> Any:
        nonlocal count
        if event == "line":
            count += 1
            if budget is not None and count > budget:
                raise pytest.fail.Exception(message, pytrace=False) from None
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        call()
    finally:
        sys.settrace(previous)
    if budget is not None and count > budget:
        pytest.fail(message, pytrace=False)
    return count


def assert_linear_time(
    make: Callable[[int], Callable[[], object]],
    *,
    unit: int,
    pieces: int = 16,
    limit: float = 4.0,
    tries: int = 3,
) -> None:
    """Fail unless ``pieces * unit`` of input costs about what ``pieces`` runs of ``unit`` cost.

    ``make(n)`` builds the input for size ``n`` and returns the call to time. A linear call
    costs about 1x, and a quadratic one costs about ``pieces`` times more. The two sides
    alternate, so a change of load hits both, and the least time of each side counts.

    One run of a part comes first, untimed, so that first-use work (a regex compile, a lazy
    import) is not timed and the coverage tracer records the path. Then the garbage
    collector and a ``sys.settrace`` line tracer (coverage's CTracer on Python 3.13 and
    lower) are off while the clock runs. A collection can land on one side only, and the
    tracer slows each Python step enough to hide a quadratic behind its linear part.
    Coverage on ``sys.monitoring`` (Python 3.14) stays on, at a small cost.
    """
    whole = make(unit * pieces)
    part = make(unit)
    part()
    best_whole = best_parts = float("inf")
    gc_was_enabled = gc.isenabled()
    previous = sys.gettrace()
    gc.disable()
    sys.settrace(None)
    try:
        for _ in range(tries):
            start = time.perf_counter()
            whole()
            best_whole = min(best_whole, time.perf_counter() - start)
            start = time.perf_counter()
            for _ in range(pieces):
                part()
            best_parts = min(best_parts, time.perf_counter() - start)
            if best_whole < limit * best_parts:
                return
    finally:
        sys.settrace(previous)
        if gc_was_enabled:
            gc.enable()
    pytest.fail(
        f"{pieces}x the input cost {best_whole / max(best_parts, 1e-9):.1f}x its {pieces} "
        f"parts on {tries} tries, so the call is not linear"
    )
