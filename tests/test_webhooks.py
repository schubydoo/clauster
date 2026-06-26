"""Unit tests for the outbound lifecycle webhook emitter (#371)."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from clauster import webhooks
from clauster.config import WebhooksConfig
from clauster.webhooks import WebhookEmitter, _valid_webhook_url


def _cfg(**kw) -> WebhooksConfig:
    return WebhooksConfig(**kw)


class _FakeResponse:
    """Minimal httpx.Response stand-in; raise_for_status raises on a >=400 status."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"status {self.status_code}")


class _RecordingClient:
    """A fake httpx.AsyncClient that records POSTs into a shared list."""

    posted: list[tuple[str, dict]] = []
    status_code: int = 200

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        type(self).posted.append((url, json))
        return _FakeResponse(type(self).status_code)


def _patch_client(monkeypatch, *, status_code: int = 200) -> type[_RecordingClient]:
    _RecordingClient.posted = []
    _RecordingClient.status_code = status_code
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _RecordingClient)
    return _RecordingClient


# ----- url validation ------------------------------------------------------


def test_valid_webhook_url():
    assert _valid_webhook_url("https://example.com/hook")
    assert _valid_webhook_url("http://10.0.0.1:9000/h")
    assert not _valid_webhook_url("ftp://example.com")  # wrong scheme
    assert not _valid_webhook_url("file:///etc/passwd")
    assert not _valid_webhook_url("https://")  # no host
    assert not _valid_webhook_url("http://:80/h")  # host-less authority (netloc truthy, no host)
    assert not _valid_webhook_url("http://[::1")  # urlparse raises ValueError → rejected
    assert not _valid_webhook_url("not a url")
    assert not _valid_webhook_url(123)
    assert not _valid_webhook_url(None)


# ----- active gating -------------------------------------------------------


def test_disabled_is_inactive():
    assert WebhookEmitter(_cfg(enabled=False, urls=["https://a.test/h"])).active is False


def test_enabled_without_urls_is_inactive():
    assert WebhookEmitter(_cfg(enabled=True, urls=[])).active is False


def test_enabled_with_only_bad_urls_is_inactive():
    assert WebhookEmitter(_cfg(enabled=True, urls=["ftp://x", "nope"])).active is False


def test_enabled_with_good_url_is_active_and_filters_bad():
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://ok.test/h", "ftp://bad"]))
    assert em.active is True
    assert em._urls == ["https://ok.test/h"]  # the bad one was dropped


# ----- SSRF deny-list (block_private_targets, #474) ------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "[::1]",
        "[fc00::1]",  # IPv6 ULA
        "[::ffff:169.254.169.254]",  # IPv4-mapped IPv6 must not bypass the check
        "2130706433",  # decimal-integer 127.0.0.1 (ipaddress rejects; getaddrinfo dials)
        "0x7f000001",  # hex 127.0.0.1
        "127.1",  # short-form 127.0.0.1
        "2852039166",  # decimal-integer 169.254.169.254 (metadata)
        "0.0.0.0",  # unspecified -> localhost on Linux
        "[::]",  # IPv6 unspecified
        "100.64.0.1",  # CGNAT — caught by the imported _EXTRA_PRIVATE_NETS
        "198.18.0.1",  # RFC2544 benchmarking — extra-nets only (is_private False <3.11)
        "192.0.0.1",  # RFC6890 IETF protocol — extra-nets only
    ],
)
def test_block_private_targets_filters_private_literals(host):
    em = WebhookEmitter(_cfg(enabled=True, block_private_targets=True, urls=[f"http://{host}/h"]))
    assert em._urls == []  # the private/loopback/link-local target was dropped
    assert em.active is False


def test_block_private_targets_off_keeps_lan_receiver():
    # Default OFF: behaviour is byte-identical — a LAN/private receiver stays usable.
    em = WebhookEmitter(_cfg(enabled=True, urls=["http://10.0.0.1:9000/h"]))
    assert em.active is True
    assert em._urls == ["http://10.0.0.1:9000/h"]


def test_block_private_targets_keeps_public_and_hostnames(monkeypatch):
    # ON: public IPs and public-resolving DNS hostnames pass; only the private literal is
    # dropped. getaddrinfo is stubbed so the test never depends on real DNS.
    import socket as _socket

    def _gai(host, *args, **kwargs):
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("clauster.webhooks.socket.getaddrinfo", _gai)
    em = WebhookEmitter(
        _cfg(
            enabled=True,
            block_private_targets=True,
            urls=["http://10.0.0.1/h", "https://example.com/h", "http://internal.lan/h"],
        )
    )
    assert em.active is True
    assert em._urls == ["https://example.com/h", "http://internal.lan/h"]


def test_is_private_host_predicate():
    from clauster.webhooks import _is_private_host

    blocked = (
        "127.0.0.1",
        "10.0.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "2130706433",
        "0x7f000001",
        "127.1",
        "2852039166",  # non-canonical IPv4 forms
        "0.0.0.0",
        "::",  # unspecified
        "100.64.0.1",
        "198.18.0.1",
        "192.0.0.1",  # CGNAT + benchmarking + IETF-protocol (extra-nets ranges)
    )
    for h in blocked:
        assert _is_private_host(h) is True, h
    for h in ("8.8.8.8", "example.com", "internal.lan", "1.1.1.1", "12.34.56.78"):
        assert _is_private_host(h) is False, h


def test_target_allowed_rejects_malformed_when_blocking():
    # A URL whose authority can't be parsed (bad IPv6) is rejected when the guard is on,
    # and allowed when off (no parse attempted). Covers the urlparse-raises branch.
    from clauster.webhooks import _target_allowed

    assert _target_allowed("http://[::1", block_private=True) is False
    assert _target_allowed("http://[::1", block_private=False) is True


def test_target_allowed_rejects_empty_host():
    # A URL that parses cleanly but has no host (authority-less) can't be classified,
    # so it's rejected when the guard is on. Covers the `if not host` fail-closed branch.
    from clauster.webhooks import _target_allowed

    assert _target_allowed("http:///x", block_private=True) is False
    assert _target_allowed("file:///x", block_private=True) is False


def test_block_private_targets_resolves_dns_hostnames(monkeypatch):
    # #549: a DNS hostname is now resolved at check time — a name pointing at a private IP is
    # dropped, a name resolving public passes, and an unresolvable name is left for httpx
    # (kept here; it can't reach a private host anyway, and the guard isn't a DNS dependency).
    import socket as _socket

    resolved = {"public.example": "93.184.216.34", "evil.test": "127.0.0.1"}

    def _gai(host, *args, **kwargs):
        if host in resolved:
            return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", (resolved[host], 0))]
        raise _socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr("clauster.webhooks.socket.getaddrinfo", _gai)
    em = WebhookEmitter(
        _cfg(
            enabled=True,
            block_private_targets=True,
            urls=[
                "https://public.example/h",  # resolves public -> kept
                "http://evil.test/h",  # resolves to 127.0.0.1 -> dropped
                "http://nxdomain.invalid/h",  # unresolvable -> kept (httpx will fail to dial)
            ],
        )
    )
    assert em._urls == ["https://public.example/h", "http://nxdomain.invalid/h"]


def test_is_private_host_literal_only_for_hostnames():
    # _is_private_host stays a pure literal classifier — a bare hostname is not an IP, so it
    # returns False here; DNS resolution lives in _target_allowed via _host_resolves_private.
    from clauster.webhooks import _is_private_host

    assert _is_private_host("localhost") is False


def test_wants_respects_events_map():
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://ok.test/h"], events={"crash": False}))
    assert em.wants("ready") is True  # absent key defaults to enabled
    assert em.wants("crash") is False  # explicitly disabled


def test_config_rejects_unknown_event_key():
    # A typo'd event key fails loudly at load, not silently (CodeRabbit #371).
    with pytest.raises(ValidationError, match="unsupported key"):
        WebhooksConfig(events={"spwan": False})
    # The valid keys still validate.
    assert WebhooksConfig(events={"spawn": False, "crash": True}).events == {
        "spawn": False,
        "crash": True,
    }


def test_config_accepts_new_extended_event_keys():
    # The #432 keys are part of the allowlist, so they validate.
    cfg = WebhooksConfig(
        events={"bg-settled": True, "permission-needed": True, "clone-done": False}
    )
    assert cfg.events == {"bg-settled": True, "permission-needed": True, "clone-done": False}


@pytest.mark.parametrize("event", ["spawn", "ready", "stop", "crash"])
def test_bridge_events_default_on_when_absent(event):
    # The original four keep the historical absent-key-enabled contract.
    assert WebhooksConfig().event_enabled(event) is True


@pytest.mark.parametrize("event", ["bg-settled", "permission-needed", "clone-done"])
def test_extended_events_default_off_when_absent(event):
    # The #432 events must NOT egress unless the operator opts in explicitly.
    assert WebhooksConfig().event_enabled(event) is False


@pytest.mark.parametrize("event", ["bg-settled", "permission-needed", "clone-done"])
def test_extended_events_enable_when_set(event):
    # An explicit True turns the opt-in event on.
    assert WebhooksConfig(events={event: True}).event_enabled(event) is True


def test_explicit_false_overrides_bridge_default_on():
    # An explicit key always wins over the default, in both directions.
    assert WebhooksConfig(events={"crash": False}).event_enabled("crash") is False


def test_wants_new_event_defaults_off():
    # An active emitter still won't emit an absent #432 event (default off)…
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://ok.test/h"]))
    assert em.wants("permission-needed") is False
    assert em.wants("spawn") is True  # …but a bridge event stays on.


def test_wants_new_event_when_explicitly_enabled():
    em = WebhookEmitter(
        _cfg(enabled=True, urls=["https://ok.test/h"], events={"clone-done": True})
    )
    assert em.wants("clone-done") is True


# ----- emit ----------------------------------------------------------------


async def test_aemit_posts_event_to_each_url(monkeypatch):
    client = _patch_client(monkeypatch)
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h", "https://b.test/h"]))
    await em.aemit("ready", {"project": "alpha"})
    assert len(client.posted) == 2  # one POST per url, no more
    assert {u for u, _ in client.posted} == {"https://a.test/h", "https://b.test/h"}
    for _, body in client.posted:
        assert body == {"event": "ready", "project": "alpha"}  # event merged into the payload


async def test_aemit_logs_and_swallows_http_error_status(monkeypatch):
    # httpx doesn't raise on 4xx/5xx; raise_for_status surfaces it, but it's still swallowed.
    client = _patch_client(monkeypatch, status_code=500)
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h"]))
    await em.aemit("crash", {})  # must not raise
    assert len(client.posted) == 1  # the POST was attempted


async def test_aemit_noop_when_event_disabled(monkeypatch):
    client = _patch_client(monkeypatch)
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h"], events={"stop": False}))
    await em.aemit("stop", {"project": "alpha"})
    assert client.posted == []


async def test_aemit_noop_when_extended_event_default_off(monkeypatch):
    # A #432 event with no explicit key does NOT POST (default off), even when active.
    client = _patch_client(monkeypatch)
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h"]))
    await em.aemit("permission-needed", {"event_type": "permission-needed", "process_id": "p1"})
    assert client.posted == []


async def test_aemit_posts_extended_event_with_discriminator(monkeypatch):
    # When opted in, the body carries both `event` and the `event_type` discriminator.
    client = _patch_client(monkeypatch)
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h"], events={"clone-done": True}))
    await em.aemit(
        "clone-done", {"event_type": "clone-done", "project": "alpha", "status": "done"}
    )
    assert len(client.posted) == 1
    _, body = client.posted[0]
    assert body == {
        "event": "clone-done",
        "event_type": "clone-done",
        "project": "alpha",
        "status": "done",
    }


async def test_aemit_noop_when_inactive(monkeypatch):
    client = _patch_client(monkeypatch)
    await WebhookEmitter(_cfg(enabled=False)).aemit("ready", {})
    assert client.posted == []


async def test_aemit_is_fail_open_on_post_error(monkeypatch):
    class _BoomClient(_RecordingClient):
        async def post(self, url, json=None):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _BoomClient)
    # Must not raise — a broken endpoint can't reach the lifecycle.
    await WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h"])).aemit("crash", {})


async def test_aemit_is_fail_open_on_client_setup_error(monkeypatch):
    def _boom(timeout=None):
        raise RuntimeError("no client")

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _boom)
    await WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h"])).aemit("ready", {})
