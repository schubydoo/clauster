"""Keep the frozen binary's unpacked files alive, and report when some are gone (#1663).

The standalone binary is a PyInstaller one-file build: at start it unpacks its templates,
static assets, migrations and libraries into a ``_MEI…`` directory under the system temp
dir. An age-based temp cleanup (systemd's stock ``tmpfiles.d`` rule deletes ``/tmp`` entries
untouched for 10 days) removes those files under a long-running service, and every page
that renders a template then answers 500 while the process itself stays up.

:class:`BundleGuard` records every path in the unpack directory at startup and re-stamps
each one on a timer, so an age-based cleanup never sees them as old. The same pass counts
the paths that no longer exist, which ``/healthz`` and the dashboard doctor panel report —
a cleanup that is not age-based (or an operator's ``rm``) must not hide behind a healthy
probe. A source, PyPI or Docker install unpacks nothing, so the guard is inert there.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from . import deps

logger = logging.getLogger(__name__)

# How often to re-stamp the unpacked files. Far below the shortest age-based cleanup
# window we know of (days), and it bounds how stale the missing-file count can be.
REFRESH_INTERVAL_SECONDS = 3600.0

# A symlink has timestamps of its own, so stamp the link rather than its target (the
# target is in the walk too). Windows cannot, and falls back to following the link.
_FOLLOW_SYMLINKS = os.utime not in os.supports_follow_symlinks


def unpack_dir() -> Path | None:
    """Return the one-file build's unpack directory, or ``None`` when not frozen."""
    if not deps.is_frozen():
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


class BundleGuard:
    """Re-stamp the unpacked files on a timer and count the ones that went missing."""

    def __init__(self, root: Path | None) -> None:
        """Record every path under ``root`` now, while the fresh unpack is still whole."""
        self.root = root
        self.missing = 0
        self._paths: tuple[str, ...] = ()
        if root is not None:
            paths = [str(root)]
            for dirpath, dirnames, filenames in os.walk(root):
                paths.extend(os.path.join(dirpath, name) for name in dirnames + filenames)
            self._paths = tuple(paths)

    def refresh(self) -> int:
        """Stamp every recorded path with the current time; return how many are gone."""
        missing = 0
        for path in self._paths:
            try:
                os.utime(path, follow_symlinks=_FOLLOW_SYMLINKS)
            except FileNotFoundError:
                missing += 1
            except OSError as exc:
                # Present but not stampable: not missing, yet it will age out, so say so.
                logger.warning("cannot refresh unpacked file %s: %s", path, exc)
        if missing:
            logger.error(
                "%d of %d unpacked files are missing from %s; restart Clauster to unpack "
                "a fresh copy",
                missing,
                len(self._paths),
                self.root,
            )
        self.missing = missing
        return missing

    async def run(self, interval: float = REFRESH_INTERVAL_SECONDS) -> None:
        """Refresh forever, off the event loop, until cancelled."""
        while True:
            await asyncio.to_thread(self.refresh)
            await asyncio.sleep(interval)
