"""Headless write CLI commands over the shared engine facade (#775, Slice B).

``start`` and ``stop`` drive :class:`~clauster.engine.ClausterEngine` to spawn and
stop bridges with **no web server running** — the same
:meth:`~clauster.runner.SessionRunner.spawn_detailed` / ``stop`` the dashboard
routes call, so a headless launch gets the identical per-mode policy, singleton
cap, and option validation. Each builds a private engine (context-managed so its
runner's persistence is disposed), first performs the **read-only** ``hydrate``
reattach so the in-memory registry is current (spawn's idempotency check and
stop's id-resolve both read it), prints the resulting instance (or ``--json``) to
**stdout**, sends diagnostics to **stderr**, and returns an exit code.

Exit codes mirror the read commands and the route's HTTP mapping: ``0`` ok, ``2``
for a request that can't be attempted (unknown project, untrusted directory, bad
option — the caller must change the request), ``1`` for a spawn clauster attempted
but could not complete (capacity cap, launch failure). ``send`` (a hosted turn) is
not here — see the module docstring of :mod:`clauster.engine`. Lazy-imported by
``__main__`` so the hot ``run`` path never pays for it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

from .engine import ClausterEngine
from .runner import (
    InvalidSpawnOption,
    NotTrusted,
    PermissionModeNotAllowed,
    SpawnError,
    UnknownProject,
)

if TYPE_CHECKING:
    from .config import ClausterConfig, PermissionMode, ResumeMode, SandboxMode, SpawnMode
    from .models import RemoteControlInstance
    from .runner import SpawnOutcome


def _print_json(obj: Any) -> None:
    """Dump ``obj`` as indented JSON to stdout (``Path``/enum coerced via ``str``)."""
    print(json.dumps(obj, indent=2, default=str))


def cmd_start(
    config: ClausterConfig,
    project: str,
    *,
    spawn_mode: SpawnMode | None,
    permission_mode: PermissionMode | None,
    resume_mode: ResumeMode | None,
    custom_name: str | None,
    sandbox: SandboxMode | None,
    trust: bool,
    as_json: bool,
) -> int:
    """Start (or hand back an already-live) bridge for ``project``; print the instance."""

    async def _go() -> SpawnOutcome:
        with ClausterEngine(config) as engine:
            await engine.hydrate()  # read-only reattach so the idempotency check sees live bridges
            return await engine.start(
                project,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                custom_name=custom_name,
                sandbox=sandbox,
                trust=trust,
            )

    try:
        outcome = asyncio.run(_go())
    except NotTrusted as exc:
        # A precondition the operator can fix — point them at --trust rather than
        # trusting implicitly (the dashboard makes trust an explicit action too).
        print(f"clauster: {exc} Pass --trust to accept it first.", file=sys.stderr)
        return 2
    except (UnknownProject, InvalidSpawnOption, PermissionModeNotAllowed) as exc:
        # Unknown target / bad option / forbidden permission mode — the request itself
        # is wrong (the route's 404/422/403), so nothing was attempted.
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    except SpawnError as exc:
        # clauster tried and could not: the bridge cap (CapacityExceeded) or a genuine
        # launch failure — both SpawnError subclasses that aren't a bad-request above.
        print(f"clauster: could not start bridge: {exc}", file=sys.stderr)
        return 1

    instance = outcome.instance
    if as_json:
        _print_json(
            {
                **instance.model_dump(mode="json"),
                "created": outcome.created,
                "reason": outcome.reason,
                "warnings": outcome.warnings,
            }
        )
        return 0
    if outcome.created:
        print(
            f"started {instance.instance_id[:8]} ({instance.project}) "
            f"mode={instance.resume_mode} status={instance.status.value}"
        )
    else:
        # Nothing launched — an already-live instance came back (singleton cap /
        # idempotent resume). Say so, with the runner's reason, so it isn't mistaken
        # for a fresh start.
        print(
            f"already running: {instance.instance_id[:8]} ({instance.project}) "
            f"status={instance.status.value}" + (f" — {outcome.reason}" if outcome.reason else "")
        )
    for warning in outcome.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def cmd_stop(config: ClausterConfig, identity: str, *, as_json: bool) -> int:
    """Stop the bridge resolved from ``identity``; print the stopped instance."""

    async def _go() -> RemoteControlInstance | None:
        with ClausterEngine(config) as engine:
            await engine.hydrate()  # populate the registry so the identity resolves
            return await engine.stop(identity)

    try:
        instance = asyncio.run(_go())
    except UnknownProject as exc:
        # The instance resolved but its project has since vanished (the route's 404).
        print(f"clauster: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # Signalling the bridge / reaping its keeper failed (dead or reused pid, revoked
        # perms). Surface it and exit 1 (tried but could not) rather than leaking a
        # traceback past the documented failure path.
        print(f"clauster: could not stop {identity!r}: {exc}", file=sys.stderr)
        return 1
    if instance is None:
        print(f"clauster: no managed instance: {identity!r}", file=sys.stderr)
        return 2
    if as_json:
        _print_json(instance.model_dump(mode="json"))
        return 0
    print(
        f"stopped {instance.instance_id[:8]} ({instance.project}) status={instance.status.value}"
    )
    return 0
