"""Outbound HTTP webhooks on Clauster lifecycle transitions (#371, #432) — the seam.

When enabled, each configured URL receives a JSON ``POST`` on a lifecycle event. The
original four are bridge events (``spawn`` / ``ready`` / ``stop`` / ``crash``); #432
adds ``bg-settled``, ``permission-needed``, and ``clone-done``. The emitter is
**fail-open**: a slow endpoint is bounded by the configured timeout, and any error is
logged and swallowed — a broken webhook must never affect a lifecycle transition. The
POST is async I/O, so callers fire it off the event loop and never await it on a
lifecycle path.

Per-event default policy lives in :meth:`WebhooksConfig.event_enabled`: an absent
bridge-event key defaults to enabled (the historical contract), an absent #432 key
defaults to disabled (opt in explicitly), so a sensitive "come look" signal never
starts egressing on upgrade alone.

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
    """Whether ``url`` is a well-formed ``http``/``https`` URL with a real host."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        # `hostname` (not `netloc`) rejects a host-less authority like `http://:80/h`
        # whose netloc is the truthy `:80`. Accessing it can also raise on a bad IPv6.
        host = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(host)


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
        """Whether ``event`` should be emitted (active and enabled per the default policy).

        Delegates the absent-key default to :meth:`WebhooksConfig.event_enabled` — a
        bridge event defaults on when unconfigured, a #432 event defaults off.
        """
        return self.active and self._config.event_enabled(event)

    async def _post(self, client: httpx.AsyncClient, url: str, payload: dict) -> None:
        """POST once, best-effort; log and swallow any error (fail-open).

        httpx does not raise on a 4xx/5xx by default, so call ``raise_for_status`` to
        surface a rejecting endpoint in the log instead of silently treating it as
        delivered — still swallowed, so it can't reach the lifecycle.
        """
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
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
