"""Filesystem-only prune/log-retention helpers for the bridge lifecycle (part of #1157).

:class:`BridgePrune` is the second collaborator extracted from
:class:`~clauster.runner.SessionRunner` (issue #1157). It applies the bridge-log
retention policy and garbage-collects Clauster's own long-dead
``bridge-pointer.json`` files. Like :class:`~clauster.bridge_launch.BridgeLaunch`
it reads configuration and paths ONLY — it holds no bridge registry
(``_instances`` / ``_procs``), no locks, and no lifecycle state, so
``SessionRunner`` keeps sole ownership of those. This matters because
``BridgePrune`` is extracted BEFORE the shared ``RunnerState`` (a later PR of
#1157): a collaborator that grabbed the registry now would have to give it back.

Any registry-derived input is therefore PASSED IN, never held:

- :meth:`_prune_logs` takes ``protected`` — the set of log-set keys belonging to
  live instances — which the runner snapshots from ``_instances`` on the event
  loop and hands down, exactly as before the extraction.
- :meth:`_prune_stale_pointers` and :meth:`_prune_one_pointer` read no registry
  at all: staleness is decided from config, the ``~/.claude.json`` trust file,
  discovery, and on-disk pointer mtimes only.

``_log_set_key`` stays on the runner (its non-prune callers — spawn's ``protected``
snapshot and the pty keeper log path — still use it) and is INJECTED here as
``log_set_key`` so :meth:`_prune_logs` shares one source of truth for the
spawn-set stem, mirroring how ``BridgeLaunch`` receives ``stderr_path_for``.

The runner delegates the moved methods to a single instance held as
``self._prune`` (built once in ``SessionRunner.__init__``), preserving the exact
public signatures — and the async-ness of :meth:`_prune_stale_pointers` — that
the tests call directly on the runner.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from . import pointers
from .config import ClausterConfig
from .discovery import discover_projects_cached

_log = logging.getLogger("clauster.bridge_prune")

# #867 L4: nothing else prunes bridge-pointer.json, so a project accumulates a pointer that
# outlives its (server-reaped) environment. At startup, clear clauster's OWN pointers that
# are both non-live AND older than this — a live or recently-stopped-resumable session is
# never touched (its reattach is preserved).
_STALE_POINTER_TTL_SECONDS = 14 * 24 * 60 * 60


class BridgePrune:
    """Apply bridge-log retention and GC stale bridge-pointers (filesystem-only)."""

    def __init__(
        self,
        *,
        config: ClausterConfig,
        log_dir: Path,
        claude_json: Path,
        claude_projects_dir: Path,
        log_set_key: Callable[[str], str],
    ) -> None:
        """Bind to config, the log dir, the trust file, the pointer dir, and the set-key helper.

        ``log_set_key`` is :meth:`SessionRunner._log_set_key`, kept on the runner
        (its non-prune callers still use it) and injected here so :meth:`_prune_logs`
        has a single source of truth for the shared spawn-set stem.
        """
        self._config = config
        self._log_dir = log_dir
        self._claude_json = claude_json
        self._claude_projects_dir = claude_projects_dir
        self._log_set_key = log_set_key

    def _prune_logs(self, protected: set[str]) -> None:
        """Apply the ``logs.retention_*`` policy to the bridge-log dir (best-effort).

        Groups files into per-spawn sets (a ``.log`` and its ``.raw.log`` / ``.stderr.log``
        / ``.keeper.json`` / ``.keeper.log`` / ``.screen.json`` siblings share a stem) and
        deletes whole sets that exceed the configured age / count / total-size limits,
        oldest first. ``protected`` (the set keys of live instances' logs, snapshotted on
        the event loop by the caller) is never pruned. A ``0`` limit disables that
        dimension. Runs off the event loop (via ``to_thread``) on each spawn; a transient
        FS error is logged and never aborts the spawn.
        """
        logs = self._config.logs
        max_age_days, max_files, max_total_mb = (
            logs.retention_max_age_days,
            logs.retention_max_files,
            logs.retention_max_total_mb,
        )
        if not (max_age_days or max_files or max_total_mb):
            return
        try:
            entries = [p for p in self._log_dir.iterdir() if p.is_file()]
        except OSError as exc:
            _log.warning("bridge-log retention: could not list %s: %s", self._log_dir, exc)
            return

        sets: dict[str, list[Path]] = {}
        for p in entries:
            sets.setdefault(self._log_set_key(p.name), []).append(p)

        def _stat(paths: list[Path]) -> tuple[float, int]:
            """Return the newest mtime and total size across one log set, skipping unstattable."""
            mtime, size = 0.0, 0
            for p in paths:
                try:
                    st = p.stat()
                except OSError:
                    continue
                mtime, size = max(mtime, st.st_mtime), size + st.st_size
            return mtime, size

        info = {k: _stat(v) for k, v in sets.items()}
        ordered = sorted(sets, key=lambda k: info[k][0], reverse=True)  # newest first
        doomed: set[str] = set()
        if max_age_days:
            cutoff = time.time() - max_age_days * 86400
            # A set with no datable file (mtime stays 0.0 — every file failed to stat) is
            # never age-pruned: we don't delete what we can't date.
            doomed.update(k for k in ordered if info[k][0] and info[k][0] < cutoff)
        if max_files:
            survivors = [k for k in ordered if k not in doomed]
            doomed.update(survivors[max_files:])
        if max_total_mb:
            survivors = [k for k in ordered if k not in doomed]  # newest first
            total = sum(info[k][1] for k in survivors)
            for k in reversed(survivors):  # oldest first
                if total <= max_total_mb * 1024 * 1024:
                    break
                doomed.add(k)
                total -= info[k][1]

        doomed -= protected  # keep live bridges' log sets regardless of age/count/size
        for k in doomed:
            for p in sets[k]:
                try:
                    p.unlink()
                except OSError as exc:
                    _log.debug("bridge-log retention: could not delete %s: %s", p, exc)
        if doomed:
            _log.info("bridge-log retention pruned %d log set(s)", len(doomed))

    async def _prune_stale_pointers(self) -> None:
        """GC clauster's own long-dead ``bridge-pointer.json`` files at startup (#867 L4).

        Scoped to projects under ``projects_root`` (clauster's own data — never a pointer
        another tool wrote), and only a pointer that is BOTH non-live AND older than
        :data:`_STALE_POINTER_TTL_SECONDS`, so a live or recently-stopped-resumable session
        keeps its reattach. Best-effort: a listing/stat/delete error is logged, never fatal
        to startup. Runs AFTER :meth:`SessionRunner.rediscover` so any live bridge is already
        adopted.
        """
        try:
            projects = await asyncio.to_thread(
                discover_projects_cached, self._config.projects_root, self._claude_json
            )
        except OSError as exc:
            _log.warning("stale-pointer prune skipped: could not list projects: %s", exc)
            return
        cutoff = time.time() - _STALE_POINTER_TTL_SECONDS
        for proj in projects:
            try:
                await asyncio.to_thread(self._prune_one_pointer, proj.path, cutoff)
            except Exception:
                # Best-effort hygiene: one project's failure (e.g. a resolve() symlink loop)
                # must never abort the GC or startup — mirror the poll loop's resilience.
                _log.exception("stale-pointer prune failed for %s; continuing", proj.path)

    def _prune_one_pointer(self, project_path: Path, cutoff: float) -> None:
        """Clear one project's pointer if it's non-live and its file mtime predates ``cutoff``."""
        resolved = project_path.resolve()
        # Ownership guard: a symlink under projects_root can resolve OUTSIDE it; the canonical
        # target's pointer is not clauster's to GC, so never prune a path that escapes the root.
        if not resolved.is_relative_to(self._config.projects_root.resolve()):
            return
        path = pointers.pointer_path_for(resolved, self._claude_projects_dir)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return  # no pointer -> nothing to prune
        except OSError as exc:
            _log.warning("could not stat bridge-pointer for %s: %s", resolved, exc)
            return
        if mtime >= cutoff:
            return  # recent enough that a resume may still want it
        try:
            # backup=False: a 2-week-dead pointer isn't worth a .bak that would itself linger.
            if pointers.clear_pointer(
                resolved, claude_projects_dir=self._claude_projects_dir, backup=False
            ):
                _log.info("pruned stale non-live bridge-pointer for %s", resolved)
        except pointers.PointerStillLive:
            pass  # became live between the stat and the clear -> leave it
        except OSError as exc:
            _log.warning("could not prune bridge-pointer for %s: %s", resolved, exc)
