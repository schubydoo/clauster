"""Apprise notifier: lazy/optional import, fail-closed sends, off-loop delivery.

A fake ``apprise`` module is injected via ``sys.modules`` so these run without the
optional ``notify`` extra installed and never hit the network.
"""

from __future__ import annotations

import sys

from clauster.config import NotificationsConfig
from clauster.notify import Notifier


class _FakeApprise:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    def add(self, url: str) -> bool:
        if "bad" in url:  # simulate a malformed/unsupported url
            return False
        self.added.append(url)
        return True

    def notify(self, title: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("send boom")
        self.sent.append((title, body))


class _FakeAppriseModule:
    Apprise = _FakeApprise


def _install_fake(monkeypatch):
    monkeypatch.setitem(sys.modules, "apprise", _FakeAppriseModule())


def test_disabled_notifier_is_inactive():
    assert Notifier(NotificationsConfig(enabled=False, urls=["slack://x"])).active is False


def test_enabled_without_urls_is_inactive(caplog):
    assert Notifier(NotificationsConfig(enabled=True, urls=[])).active is False
    assert any("no urls" in r.message for r in caplog.records)


def test_missing_apprise_is_inactive(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "apprise", None)  # `import apprise` -> ImportError
    n = Notifier(NotificationsConfig(enabled=True, urls=["slack://x"]))
    assert n.active is False
    assert any("Apprise is not installed" in r.message for r in caplog.records)


def test_active_with_apprise(monkeypatch):
    _install_fake(monkeypatch)
    n = Notifier(NotificationsConfig(enabled=True, urls=["slack://x"]))
    assert n.active is True
    assert n._apprise.added == ["slack://x"]


def test_all_urls_rejected_is_inactive(monkeypatch, caplog):
    _install_fake(monkeypatch)
    n = Notifier(NotificationsConfig(enabled=True, urls=["bad://nope"]))
    assert n.active is False
    assert any("no usable urls" in r.message for r in caplog.records)


async def test_anotify_sends(monkeypatch):
    _install_fake(monkeypatch)
    n = Notifier(NotificationsConfig(enabled=True, urls=["slack://x"]))
    await n.anotify("the title", "the body")
    assert n._apprise.sent == [("the title", "the body")]


async def test_anotify_swallows_send_error(monkeypatch, caplog):
    _install_fake(monkeypatch)
    n = Notifier(NotificationsConfig(enabled=True, urls=["slack://x"]))
    n._apprise.fail = True
    await n.anotify("t", "b")  # fail-closed: must not raise
    assert any("notification send failed" in r.message for r in caplog.records)


async def test_anotify_noop_when_inactive():
    # No apprise, disabled — anotify is a no-op and never raises.
    await Notifier(NotificationsConfig(enabled=False)).anotify("t", "b")


def test_build_degrades_when_apprise_raises(monkeypatch, caplog):
    # Fail-closed construction: a non-ImportError from Apprise (a future API change, an
    # internal error) must degrade to inactive, not propagate out of __init__ into bridge
    # startup — the module's "Construction never raises" contract.
    class _BoomApprise:
        def __init__(self) -> None:
            raise RuntimeError("apprise blew up")

    class _BoomModule:
        Apprise = _BoomApprise

    monkeypatch.setitem(sys.modules, "apprise", _BoomModule())
    n = Notifier(NotificationsConfig(enabled=True, urls=["slack://x"]))  # must not raise
    assert n.active is False
    assert any("building the Apprise client failed" in r.message for r in caplog.records)


def test_send_is_noop_without_apprise():
    # _send guards on its own (defence-in-depth, not relying on anotify's check): a
    # direct call with no apprise is a safe no-op and never raises — the fail-closed
    # contract holds even if a future caller reaches _send without going via anotify.
    n = Notifier(NotificationsConfig(enabled=False))
    assert n._apprise is None
    n._send("t", "b")
