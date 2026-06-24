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
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from .config import WebhooksConfig
from .provisioning import _EXTRA_PRIVATE_NETS

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


def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an IP address is internal / non-routable (the shared SSRF classifier).

    Classifies the same internal ranges as the clone SSRF guard
    (``provisioning._ip_blocked``): loopback, link-local (incl. the 169.254.169.254
    metadata IP), RFC1918 private, unspecified (``0.0.0.0`` / ``::``), reserved,
    multicast, IPv6 ULA, and the carrier/benchmark nets imported from
    ``provisioning._EXTRA_PRIVATE_NETS`` (CGNAT ``100.64/10``). An IPv4-mapped IPv6
    address is normalized to its IPv4 form first.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_unspecified
        or ip.is_reserved
        or ip.is_multicast
        or any(ip in net for net in _EXTRA_PRIVATE_NETS)
    )


def _is_private_host(host: str) -> bool:
    """Whether ``host`` is an internal / non-routable IP *literal* (any encoding).

    It catches the *non-canonical* IPv4 encodings ``getaddrinfo`` (and so httpx) still
    dials but ``ipaddress`` rejects — decimal-integer (``2130706433``), hex
    (``0x7f000001``), short (``127.1``) — via ``socket.inet_aton``, so they can't slip
    through to ``127.0.0.1`` / the metadata IP.

    A genuine DNS hostname is not an IP literal and returns False here — it is resolved
    separately by :func:`_host_resolves_private`. Exotic IPv6 embeddings (NAT64,
    IPv4-compatible) are not normalized — out of scope for this literal-IP seam.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Non-canonical IPv4 the resolver still honors (decimal-int / hex / octal / short
        # form). ``ipaddress`` rejects these, but glibc/getaddrinfo — which httpx dials —
        # accepts them, so classify via the same inet_aton or they bypass.
        try:
            ip = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return False  # genuine non-IP (a DNS hostname)
    return _ip_is_private(ip)


def _host_resolves_private(host: str) -> bool:
    """Whether a DNS hostname resolves to any internal IP (best-effort, check-time).

    Closes the simple bypass where a hostname points straight at a private IP. The
    lookup is at config-filter time; a rebinding domain that re-resolves to a private IP
    when httpx dials is an acknowledged TOCTOU residual (same class as the clone-URL
    guard), out of scope here. An unresolvable host returns False — httpx can't dial it
    either, so nothing egresses, and the guard never becomes a DNS-availability dependency.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _ip_is_private(ip):
            return True
    return False


def _target_allowed(url: str, *, block_private: bool) -> bool:
    """Whether ``url`` survives the opt-in SSRF guard.

    With ``block_private`` off this is always True (byte-identical to the historical
    behaviour). With it on, a URL whose host is a private/loopback/link-local IP literal
    — or a DNS name that resolves to one — is rejected; public IPs and public-resolving
    hostnames pass.
    """
    if not block_private:
        return True
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if _is_private_host(host):
        return False
    # Resolve a DNS hostname so a name pointing at a private IP can't bypass; an IP literal
    # resolves to itself (already classified above), so this stays a no-op for it.
    return not _host_resolves_private(host)


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
            if not _valid_webhook_url(url):
                # Don't log the URL itself — it can carry a token/secret.
                _log.warning("webhooks: rejected a non-http(s)/malformed url (redacted); skipping")
            elif not _target_allowed(url, block_private=config.block_private_targets):
                # SSRF guard (block_private_targets): a private/loopback/link-local IP
                # literal target is dropped. Redact — the URL can carry a secret.
                _log.warning(
                    "webhooks: rejected a private/loopback/link-local target "
                    "(redacted, block_private_targets on); skipping"
                )
            else:
                self._urls.append(url)
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
