"""Read cgroup task-limit counters for ``clauster doctor`` and the crashed-bridge reason.

The kernel counts every fork that a cgroup's ``pids.max`` refuses, in the ``max`` line of
``pids.events``. That counter is the only record of a short burst, because the task count is
back to normal by the time anyone looks. This module only reads. Every function returns
``None`` when the data does not exist, for example on macOS or Windows, so a caller makes no
claim without evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import bridge_scope

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class TaskEventCount:
    """The refused-fork counters of one live bridge's cgroup and of its parent."""

    pid: int
    cgroup: Path
    own: int
    parent: int | None


def refused_forks(cgroup: Path) -> int | None:
    """Return the ``max`` counter of ``pids.events``, or ``None`` when it cannot be read."""
    try:
        text = (cgroup / "pids.events").read_text(encoding="ascii")
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "max" and parts[1].isascii() and parts[1].isdigit():
            return int(parts[1])
    return None


def cgroup_path(relative: str) -> Path | None:
    """Return the sysfs folder of a cgroup path, refusing one outside the cgroup root."""
    if not relative.startswith("/") or ".." in PurePosixPath(relative).parts:
        return None
    return _CGROUP_ROOT / relative.lstrip("/")


def cgroup_of(pid: int) -> Path | None:
    """Return the cgroup v2 folder of ``pid``, or ``None`` when the process has none."""
    try:
        text = (_PROC_ROOT / str(pid) / "cgroup").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return cgroup_path(line[3:])
    return None


def snapshot(pid: int) -> TaskEventCount | None:
    """Record the counters of a live bridge, as the baseline for a later crash."""
    cgroup = cgroup_of(pid)
    if cgroup is None:
        return None
    own = refused_forks(cgroup)
    if own is None:
        return None
    return TaskEventCount(pid, cgroup, own, refused_forks(cgroup.parent))


def crash_note(base: TaskEventCount) -> str | None:
    """Return the reason text when a limit refused forks after ``base`` was taken.

    The bridge's own cgroup gives a "likely" cause: a rise shows that its limit was hit near
    the crash, but does not prove the cause. A scope disappears when its last process exits,
    and then only the parent slice has a count. That count includes sibling scopes, so the
    text says only that the limit can belong to this bridge or to another one.
    """
    now = refused_forks(base.cgroup)
    if now is not None:
        refused = now - base.own
        ending = "shortly before this bridge exited. This is the likely cause."
    else:
        parent = refused_forks(base.cgroup.parent)
        if parent is None or base.parent is None:
            return None
        refused = parent - base.parent
        ending = (
            "shortly before this bridge exited. The limit can belong to this bridge or to "
            "another bridge, so this is a possible cause."
        )
    if refused <= 0:
        return None
    return (
        f"A task limit refused {refused} new process or thread "
        f"start{'' if refused == 1 else 's'} {ending}"
    )


def bridge_slice_refused() -> int | None:  # pragma: skip-on-win
    """Return the refused forks of all bridge scopes, which outlive any single scope."""
    uid = bridge_scope._uid()
    return refused_forks(
        _CGROUP_ROOT
        / "user.slice"
        / f"user-{uid}.slice"
        / f"user@{uid}.service"
        / bridge_scope.BRIDGE_SLICE
    )
