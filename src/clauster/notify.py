"""Outbound notifications on bridge lifecycle events, via Apprise (optional extra).

Apprise is an **optional** dependency (the ``notify`` extra). The notifier imports
it lazily and degrades to a no-op when it's absent or disabled, so importing this
module never requires Apprise. Sends are **best-effort and fail-closed**: a send
error is logged and swallowed — a broken notification endpoint must never affect a
bridge's lifecycle. The actual send is synchronous (network I/O), so callers use
:meth:`Notifier.anotify` to run it off the event loop.
"""

from __future__ import annotations

import asyncio
import logging

from .config import NotificationsConfig

_log = logging.getLogger("clauster.notify")


class Notifier:
    """Sends Apprise notifications when enabled, configured, and Apprise is installed.

    ``active`` is True only when notifications are enabled, at least one URL was
    accepted, and Apprise imported — callers can cheaply skip building a message
    otherwise. Construction never raises.
    """

    def __init__(self, config: NotificationsConfig) -> None:
        """Build the Apprise targets from config (never raises)."""
        self._config = config
        self._apprise = None
        if config.enabled and config.urls:
            self._build()
        elif config.enabled and not config.urls:
            _log.warning("notifications enabled but no urls configured; sending nothing")

    def _build(self) -> None:
        """Build the Apprise sink from the configured URLs; stay disabled on any failure."""
        try:
            import apprise  # type: ignore[import-not-found]  # optional `notify` extra
        except ImportError:
            _log.warning(
                "notifications enabled but Apprise is not installed — install the extra "
                "(pip install 'clauster[notify]'). Notifications disabled."
            )
            return
        try:
            ap = apprise.Apprise()
            accepted = 0
            for url in self._config.urls:
                # ap.add returns False on a malformed/unsupported URL. Don't log the URL
                # itself — it can carry a token/secret.
                if ap.add(url):
                    accepted += 1
                else:
                    _log.warning("notifications: Apprise rejected a url (redacted); skipping it")
        except Exception:  # noqa: BLE001 - construction must never raise into bridge startup
            # Honours the "Construction never raises" contract: any unexpected Apprise
            # error degrades to inactive (self._apprise stays None) instead of taking the
            # bridge down at startup.
            _log.exception("notifications: building the Apprise client failed; disabling")
            return
        if accepted:
            self._apprise = ap
        else:
            _log.warning("notifications enabled but no usable urls; sending nothing")

    @property
    def active(self) -> bool:
        """Whether sends will actually be attempted (enabled + usable + Apprise present)."""
        return self._apprise is not None

    def _send(self, title: str, body: str) -> None:
        """Send synchronously (best-effort); log and swallow any error (fail-closed)."""
        if self._apprise is None:
            return
        try:
            self._apprise.notify(title=title, body=body)
        except Exception:  # noqa: BLE001 - a notify error must never reach the lifecycle
            _log.exception("notification send failed")

    async def anotify(self, title: str, body: str) -> None:
        """Send off the event loop. Never raises (the underlying send is fail-closed)."""
        if self._apprise is None:
            return
        await asyncio.to_thread(self._send, title, body)
