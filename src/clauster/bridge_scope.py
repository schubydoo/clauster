"""Start a bridge in its own systemd user scope with a task limit (opt-in).

Every bridge normally shares the cgroup of the Clauster service, so one runaway project can
use the whole service task limit and make forks fail for every other bridge. When
``instance_defaults.bridge_task_limit`` is set, each bridge starts through
``systemd-run --user --scope`` in its own scope, under one shared slice. ``systemd-run``
replaces itself with the bridge, so the PID that Clauster tracks does not change.

A scope needs a user manager that outlives a login session, so this module requires
``loginctl enable-linger``. Without it, the manager stops at logout and takes the scopes with
it. Before each start, a short test scope proves that the manager accepts scopes. If a
requirement is missing or the test fails, the bridge starts unscoped, the log has a warning,
and ``clauster doctor`` shows the reason. Clauster never reports containment that it does not
have.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import procutil, redact

_log = logging.getLogger("clauster.bridge_scope")

#: The one slice that holds every bridge scope, so its ``pids.events`` counts all refusals.
BRIDGE_SLICE = "clauster_bridges.slice"
_RUNTIME_ROOT = Path("/run/user")
_LINGER_DIR = Path("/var/lib/systemd/linger")
_PREFLIGHT_TIMEOUT = 10


@dataclass(frozen=True)
class ScopeSupport:
    """Whether this host can start a bridge in a user scope, and with which paths."""

    available: bool
    detail: str
    env_binary: str = ""
    systemd_run: str = ""
    runtime_dir: str = ""


def _uid() -> int:  # pragma: skip-on-win
    """Return the current user id. A separate seam because Windows has no ``os.getuid``."""
    return os.getuid()


def _user_name(uid: int) -> str:  # pragma: skip-on-win
    """Return the account name of ``uid``. ``$USER`` can differ, and linger is per account."""
    import pwd  # POSIX only, so not a module-level import

    return pwd.getpwuid(uid).pw_name


def _is_socket(path: Path) -> bool:
    """Return whether ``path`` is a socket, treating an unreadable path as absent."""
    try:
        return path.is_socket()
    except OSError:
        return False


def _preflight(
    env_binary: str, systemd_run: str, true_binary: str, runtime_dir: str
) -> str | None:
    """Start and end one empty scope. Return why it failed, or ``None`` when it worked.

    This call omits ``--quiet``, because ``--quiet`` hides why a scope job failed. The real
    launch needs ``--quiet`` (see :func:`wrap_argv`), so this test is where the reason appears.
    """
    argv = [
        env_binary,
        f"XDG_RUNTIME_DIR={runtime_dir}",
        systemd_run,
        "--user",
        "--scope",
        "--collect",
        f"--slice={BRIDGE_SLICE}",
        f"--unit=clauster-bridge-probe-{uuid.uuid4().hex[:12]}.scope",
        "-p",
        "TasksMax=16",
        true_binary,
    ]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_PREFLIGHT_TIMEOUT,
            env=procutil.child_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"test scope failed: {redact.redact_for_disk(str(exc))[:300]}"
    if done.returncode == 0:
        return None
    lines = done.stderr.strip().splitlines()
    reason = redact.redact_for_disk(lines[-1])[:300] if lines else f"exit {done.returncode}"
    return f"test scope failed: {reason}"


def probe_scope_support() -> ScopeSupport:
    """Check each requirement for a user scope, and start one empty test scope."""
    if sys.platform != "linux":
        return ScopeSupport(False, "needs Linux with systemd")
    systemd_run = shutil.which("systemd-run")
    env_binary = shutil.which("env")
    true_binary = shutil.which("true")
    if systemd_run is None or env_binary is None or true_binary is None:
        missing = next(
            name
            for name, path in (
                ("systemd-run", systemd_run),
                ("env", env_binary),
                ("true", true_binary),
            )
            if path is None
        )
        return ScopeSupport(False, f"{missing} is not on PATH")
    try:
        uid = _uid()
        user = _user_name(uid)
    except (OSError, KeyError) as exc:
        return ScopeSupport(False, f"cannot identify the current user: {exc}")
    runtime_dir = _RUNTIME_ROOT / str(uid)
    if not _is_socket(runtime_dir / "bus"):
        return ScopeSupport(False, f"no user bus at {runtime_dir / 'bus'}")
    try:
        lingering = (_LINGER_DIR / user).exists()
    except OSError as exc:
        return ScopeSupport(False, f"cannot read the linger state: {exc}")
    if not lingering:
        return ScopeSupport(False, f"linger is off; run `sudo loginctl enable-linger {user}`")
    failure = _preflight(env_binary, systemd_run, true_binary, str(runtime_dir))
    if failure is not None:
        return ScopeSupport(False, failure)
    return ScopeSupport(True, "user manager available", env_binary, systemd_run, str(runtime_dir))


def wrap_argv(
    argv: list[str],
    *,
    limit: int,
    project: str,
    support: ScopeSupport,
    bridge_runtime_dir: str | None,
) -> list[str]:
    """Return ``argv`` prefixed to run in a new scope with ``TasksMax=limit``.

    A system service has no ``XDG_RUNTIME_DIR``, and ``systemd-run --user`` needs it to find
    the user bus, so ``env`` sets it. A second ``env`` inside the scope gives the bridge the
    value it has without a scope (``bridge_runtime_dir``, or unset), because claude keeps its
    session socket under that folder. ``--quiet`` keeps the unit banner off the pty, because
    the keeper's login-screen check needs claude's output first. The project name is reduced
    to safe characters because it is part of a unit name.
    The random suffix keeps a resumed bridge from clashing with the old scope, which stays
    alive while an orphaned session runs in it.
    """
    safe = re.sub(r"[^A-Za-z0-9_]", "_", project)[:48] or "bridge"
    restore = (
        ["-u", "XDG_RUNTIME_DIR"]
        if bridge_runtime_dir is None
        else [f"XDG_RUNTIME_DIR={bridge_runtime_dir}"]
    )
    return [
        support.env_binary,
        f"XDG_RUNTIME_DIR={support.runtime_dir}",
        support.systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--slice={BRIDGE_SLICE}",
        f"--unit=clauster-bridge-{safe}-{uuid.uuid4().hex[:12]}.scope",
        "-p",
        f"TasksMax={limit}",
        support.env_binary,
        *restore,
        *argv,
    ]


def binary_problem(binary: str) -> str | None:
    """Return why this ``claude`` path cannot run in a scope, or ``None``.

    The inner ``env`` of :func:`wrap_argv` reads a program path with ``=`` as an assignment.
    """
    return "the claude path contains '='" if "=" in binary else None


def scoped_argv(
    argv: list[str],
    *,
    limit: int | None,
    project: str,
    bridge_runtime_dir: str | None,
    support: ScopeSupport | None,
) -> list[str]:
    """Wrap ``argv`` when a limit is configured and the host supports scopes.

    ``support`` is a recent probe result. With ``None``, this call probes the host.
    """
    if limit is None:
        return argv
    problem = binary_problem(argv[0])
    if problem is not None:
        _log.warning(
            "bridge_task_limit is set, but bridge %r starts without a task limit: %s",
            project,
            problem,
        )
        return argv
    support = support or probe_scope_support()
    if not support.available:
        _log.warning(
            "bridge_task_limit is set, but bridge %r starts without a task limit: %s",
            project,
            support.detail,
        )
        return argv
    return wrap_argv(
        argv,
        limit=limit,
        project=project,
        support=support,
        bridge_runtime_dir=bridge_runtime_dir,
    )
