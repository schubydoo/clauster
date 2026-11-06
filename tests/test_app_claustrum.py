"""App-lifespan + /healthz integration for the claustrum hosted channel (CL-2).

These use a stub :class:`ClaustrumDaemon` so no real daemon process is spawned:
the goal is the wiring — the lifespan connect-or-spawn branch (including the
fail-closed path) and the ``/healthz`` status surfacing.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import clauster.app as app_module
from clauster.app import create_app
from clauster.claustrum_client import DaemonUnreachable
from clauster.config import load_config


class _StubDaemon:
    """Minimal stand-in for ClaustrumDaemon (no socket, no subprocess)."""

    def __init__(self, config) -> None:  # noqa: ANN001 - mirrors the real signature
        self.ensured = False
        self.closed = False
        self.client = None  # no live client → lifespan skips hosted reattach (CL-6)
        self.hooks: dict | None = None

    def set_connection_hooks(self, *, on_reconnect, on_unreachable) -> None:
        """Record the reconnect hooks the lifespan wires (TASK-518)."""
        self.hooks = {"on_reconnect": on_reconnect, "on_unreachable": on_unreachable}

    async def ensure(self):
        """Record that the lifespan asked us to come up."""
        self.ensured = True
        return object()

    def status(self) -> dict:
        """Return a healthy status block."""
        return {
            "enabled": True,
            "running": True,
            "socket": "/tmp/fake.sock",
            "version": "stub-1",
            "error": None,
        }

    async def probe(self) -> dict:
        """Live health probe (no real socket) — mirror status()."""
        return self.status()

    async def aclose(self) -> None:
        """Record shutdown."""
        self.closed = True


def _enabled_config(write_config, tmp_path: Path):
    extra = f"state_dir: {tmp_path / 'clstate'}\nclaustrum:\n  enabled: true\n"
    return load_config(write_config(extra))


def test_healthz_surfaces_claustrum_when_enabled(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "ClaustrumDaemon", _StubDaemon)
    config = _enabled_config(write_config, tmp_path)
    with TestClient(create_app(config)) as client:
        body = client.get("/healthz").json()
    assert body["claustrum"]["running"] is True
    assert body["claustrum"]["version"] == "stub-1"


def test_lifespan_fail_closed_on_daemon_error(write_config, tmp_path, monkeypatch):
    class _FailDaemon(_StubDaemon):
        async def ensure(self):
            raise DaemonUnreachable("daemon down")

        def status(self) -> dict:
            return {"enabled": True, "running": False, "socket": "", "version": None, "error": "x"}

    monkeypatch.setattr(app_module, "ClaustrumDaemon", _FailDaemon)
    config = _enabled_config(write_config, tmp_path)
    # Startup must not raise even though the daemon is unreachable (fail-closed).
    with TestClient(create_app(config)) as client:
        body = client.get("/healthz").json()
    assert body["claustrum"]["running"] is False


def test_healthz_omits_claustrum_when_disabled(write_config, tmp_path):
    extra = f"state_dir: {tmp_path / 'clstate'}\n"
    config = load_config(write_config(extra))
    with TestClient(create_app(config)) as client:
        body = client.get("/healthz").json()
    assert "claustrum" not in body


def test_lifespan_wires_the_reconnect_hooks_to_the_hosted_manager(
    write_config, tmp_path, monkeypatch
):
    # TASK-518: a reconnect must reach the LIVE manager's rebind_all / fail_unreachable.
    made: list[_StubDaemon] = []

    class _Recording(_StubDaemon):
        def __init__(self, config) -> None:  # noqa: ANN001 - mirrors the real signature
            super().__init__(config)
            made.append(self)

    monkeypatch.setattr(app_module, "ClaustrumDaemon", _Recording)
    config = _enabled_config(write_config, tmp_path)
    app = create_app(config)
    calls: list[tuple[str, object]] = []

    async def rebind_all(client):
        calls.append(("rebind_all", client))

    async def reattach_all(client, *, history_for):
        calls.append(("reattach_all", client))
        assert callable(history_for)

    async def fail_unreachable(reason):
        calls.append(("fail_unreachable", reason))

    with TestClient(app) as client:
        hosted = app.state.hosted
        monkeypatch.setattr(hosted, "rebind_all", rebind_all)
        monkeypatch.setattr(hosted, "reattach_all", reattach_all)
        monkeypatch.setattr(hosted, "fail_unreachable", fail_unreachable)
        hooks = made[0].hooks
        assert hooks is not None
        sentinel = object()
        # The stub daemon had no client, so no startup reattach ran: the first
        # connection reattaches the stored sessions; a later one rebinds them.
        assert hosted.restored is False
        client.portal.call(hooks["on_reconnect"], sentinel)
        monkeypatch.setattr(hosted, "_restored", True)
        client.portal.call(hooks["on_reconnect"], sentinel)
        client.portal.call(hooks["on_unreachable"], "gone")
    assert calls == [
        ("reattach_all", sentinel),
        ("rebind_all", sentinel),
        ("fail_unreachable", "gone"),
    ]
