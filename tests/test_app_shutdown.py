"""App graceful-shutdown teardown — the clauster-owned lifespan ``finally`` block.

uvicorn owns the SIGINT/SIGTERM handlers; what *clauster* owns is the lifespan
shutdown that runs on the way down: cancel the runner poll loop, stop live hosted
sessions, and drop the claustrum daemon connection (leaving bridges + the daemon
itself running). Those three teardown steps were previously unasserted — the CLI
tests mock ``uvicorn.run`` away, and the existing signal tests target
bridges/keepers/the fake daemon, not the clauster process. A ``with
TestClient(...)`` block runs the real lifespan (startup *and* shutdown), so it is
the seam that exercises this path.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import clauster.app as app_module
from clauster.app import create_app
from clauster.config import load_config
from clauster.runner import SessionRunner


def test_shutdown_cancels_runner_poll_loop(write_config):
    """The lifespan teardown cancels the runner's background poll task."""
    config = load_config(write_config())
    runner = SessionRunner(config)
    # Inject the real runner so we can inspect its poll task after the lifespan
    # exits. Startup creates the task; shutdown must cancel it.
    with TestClient(create_app(config, runner=runner)) as client:
        client.get("/healthz")
        assert runner._poll_task is not None
        assert not runner._poll_task.done()  # alive while the app is up
        poll_task = runner._poll_task  # grab the ref before shutdown clears it
    # After the `with` block, FastAPI has run lifespan shutdown: the poll task is
    # cancelled and the runner's handle is reset (so a restart starts a fresh one).
    assert poll_task.cancelled()
    assert runner._poll_task is None


def test_shutdown_closes_hosted_manager(write_config, monkeypatch):
    """The lifespan teardown calls ``hosted.aclose()`` to stop live hosted sessions."""
    closed = {"hosted": False}
    real_aclose = app_module.HostedManager.aclose

    async def _spy_aclose(self):
        closed["hosted"] = True
        await real_aclose(self)

    monkeypatch.setattr(app_module.HostedManager, "aclose", _spy_aclose)
    config = load_config(write_config())
    with TestClient(create_app(config)) as client:
        client.get("/healthz")
        assert closed["hosted"] is False  # not yet — only on shutdown
    assert closed["hosted"] is True


def test_shutdown_drops_claustrum_daemon_connection(write_config, tmp_path, monkeypatch):
    """With claustrum enabled, the teardown calls ``daemon.aclose()`` (drop, don't kill).

    Startup connects-or-spawns the daemon; the lifespan ``finally`` must drop our
    connection on the way down. A stub daemon records both calls so we assert the
    connect-then-drop ordering across the full startup/shutdown cycle.
    """

    class _StubDaemon:
        """Connection-lifecycle stand-in (no socket, no subprocess)."""

        def __init__(self, config) -> None:  # noqa: ANN001 - mirrors the real signature
            self.ensured = False
            self.closed = False
            self.client = None  # no live client → lifespan skips hosted reattach

        async def ensure(self):
            """Record connect-or-spawn."""
            self.ensured = True
            return object()

        async def probe(self) -> dict:
            """Minimal health block for /healthz."""
            return {"enabled": True, "running": True}

        async def aclose(self) -> None:
            """Record the connection drop."""
            self.closed = True

    monkeypatch.setattr(app_module, "ClaustrumDaemon", _StubDaemon)
    extra = f"state_dir: {tmp_path / 'clstate'}\nclaustrum:\n  enabled: true\n"
    config = load_config(write_config(extra))
    app = create_app(config)
    with TestClient(app) as client:
        client.get("/healthz")
        daemon = app.state.claustrum_daemon
        assert daemon.ensured is True  # connected on startup
        assert daemon.closed is False  # still connected while up
    assert daemon.closed is True  # connection dropped on shutdown
