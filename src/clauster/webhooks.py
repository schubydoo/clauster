"""Outbound HTTP webhooks on bridge lifecycle transitions (#371) — the first seam.

When enabled, each configured URL receives a JSON ``POST`` on a lifecycle event
(``spawn`` / ``ready`` / ``stop`` / ``crash``). The emitter is **fail-open**: a slow
endpoint is bounded by the configured timeout, and any error is logged and swallowed
— a broken webhook must never affect a bridge's lifecycle. The POST is async I/O, so
callers fire it off the event loop and never await it on a lifecycle path.

URLs come only from config (an operator-trusted source) and are scheme-checked
(``http``/``https`` only) at construction, so a malformed URL disables that target
rather than failing a spawn.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx

from .config import WebhooksConfig

_log = logging.getLogger("clauster.webhooks")


def _valid_webhook_url(url: object) -> bool:
    """Whether ``url`` is a well-formed ``http``/``https`` URL with a host."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class WebhookEmitter:
    """POSTs a lifecycle event to each accepted URL; fail-open and off the loop.

    ``active`` is True only when webhooks are enabled and at least one URL passed the
    scheme check — callers can cheaply skip building a payload otherwise. Construction
    never raises.
    """

    def __init__(self, config: WebhooksConfig) -> None:
        """Filter the configured URLs to the valid http(s) ones (never raises)."""
        self._config = config
        self._urls: list[str] = []
        if not config.enabled:
            return
        if not config.urls:
            _log.warning("webhooks enabled but no urls configured; emitting nothing")
            return
        for url in config.urls:
            if _valid_webhook_url(url):
                self._urls.append(url)
            else:
                # Don't log the URL itself — it can carry a token/secret.
                _log.warning("webhooks: rejected a non-http(s)/malformed url (redacted); skipping")
        if not self._urls:
            _log.warning("webhooks enabled but no usable urls; emitting nothing")

    @property
    def active(self) -> bool:
        """Whether any POST will be attempted (enabled + at least one usable url)."""
        return bool(self._urls)

    def wants(self, event: str) -> bool:
        """Whether ``event`` should be emitted (active and not disabled in ``events``)."""
        return self.active and self._config.events.get(event, True)

    async def _post(self, client: httpx.AsyncClient, url: str, payload: dict) -> None:
        """POST once, best-effort; log and swallow any error (fail-open)."""
        try:
            await client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001 - a webhook error must never reach the lifecycle
            # Redact the url (it can carry a secret); the exception type/text is safe.
            _log.warning("webhook POST failed (url redacted): %s", exc)

    async def aemit(self, event: str, payload: dict) -> None:
        """Emit ``event`` to every usable url off the loop. Never raises (fail-open)."""
        if not self.wants(event):
            return
        body = {"event": event, **payload}
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                await asyncio.gather(
                    *(self._post(client, url, body) for url in self._urls),
                    return_exceptions=True,
                )
        except Exception as exc:  # noqa: BLE001 - client setup/teardown must not break a spawn
            _log.warning("webhook emit failed: %s", exc)
