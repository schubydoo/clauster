"""Unit tests for the outbound lifecycle webhook emitter (#371)."""

from __future__ import annotations

import httpx

from clauster import webhooks
from clauster.config import WebhooksConfig
from clauster.webhooks import WebhookEmitter, _valid_webhook_url


def _cfg(**kw) -> WebhooksConfig:
    return WebhooksConfig(**kw)


class _RecordingClient:
    """A fake httpx.AsyncClient that records POSTs into a shared list."""

    posted: list[tuple[str, dict]] = []

    def __init__(self, timeout=None):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        type(self).posted.append((url, json))


def _patch_client(monkeypatch) -> type[_RecordingClient]:
    _RecordingClient.posted = []
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _RecordingClient)
    return _RecordingClient


# ----- url validation ------------------------------------------------------


def test_valid_webhook_url():
    assert _valid_webhook_url("https://example.com/hook")
    assert _valid_webhook_url("http://10.0.0.1:9000/h")
    assert not _valid_webhook_url("ftp://example.com")  # wrong scheme
    assert not _valid_webhook_url("file:///etc/passwd")
    assert not _valid_webhook_url("https://")  # no host
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


def test_wants_respects_events_map():
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://ok.test/h"], events={"crash": False}))
    assert em.wants("ready") is True  # absent key defaults to enabled
    assert em.wants("crash") is False  # explicitly disabled


# ----- emit ----------------------------------------------------------------


async def test_aemit_posts_event_to_each_url(monkeypatch):
    client = _patch_client(monkeypatch)
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h", "https://b.test/h"]))
    await em.aemit("ready", {"project": "alpha"})
    assert {u for u, _ in client.posted} == {"https://a.test/h", "https://b.test/h"}
    assert all(b["event"] == "ready" and b["project"] == "alpha" for _, b in client.posted)


async def test_aemit_noop_when_event_disabled(monkeypatch):
    client = _patch_client(monkeypatch)
    em = WebhookEmitter(_cfg(enabled=True, urls=["https://a.test/h"], events={"stop": False}))
    await em.aemit("stop", {"project": "alpha"})
    assert client.posted == []


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
