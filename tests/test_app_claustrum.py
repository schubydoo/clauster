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
