"""Tests for the claustrum daemon connect-or-spawn lifecycle (CL-2).

Driven against ``fake_claustrum_bin.py`` (a spawnable, self-daemonizing fake
daemon binary) for the spawn/reuse/failure paths, and the in-process
``FakeClaustrum`` fixture for the auth-rejection path. Every spawned daemon is
reaped on teardown via the ``<socket>.pid`` file the fake binary drops next to
its socket, so no detached child leaks past a test.

POSIX-only: the daemon uses ``AF_UNIX`` + ``setsid`` and is skipped on Windows.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from clauster.claustrum_client import AuthRejected, DaemonUnreachable
from clauster.claustrum_daemon import ClaustrumDaemon, DaemonSpawnError
from clauster.config import ClausterConfig

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="claustrum daemon is POSIX-only (AF_UNIX + setsid)"
)

FAKE_BIN = Path(__file__).resolve().parent / "fixtures" / "fake_claustrum_bin.py"


@pytest.fixture
async def make_daemon(tmp_path: Path) -> AsyncIterator[Callable[..., ClaustrumDaemon]]:
    """Factory for :class:`ClaustrumDaemon` instances wired to the fake binary.

    Uses a short ``mkdtemp`` ``state_dir`` (so the derived ``AF_UNIX`` path stays
    under the ~108-char kernel limit). On teardown it closes every connection and
    SIGTERMs each detached fake daemon via its PID file.
    """
    FAKE_BIN.chmod(0o755)
    created: list[ClaustrumDaemon] = []
    roots: list[Path] = []

    def _make(
        *,
        binary: str = str(FAKE_BIN),
        state_dir: Path | None = None,
        socket_path: str | None = None,
        token: str | None = None,
        **claustrum_kwargs: object,
    ) -> ClaustrumDaemon:
        root = state_dir if state_dir is not None else Path(tempfile.mkdtemp(prefix="cld-"))
        if state_dir is None:
            roots.append(root)
        cl: dict[str, object] = {"enabled": True, "binary": binary, **claustrum_kwargs}
        if socket_path is not None:
            cl["socket_path"] = socket_path
        config = ClausterConfig(projects_root=tmp_path, state_dir=root, claustrum=cl)
        daemon = ClaustrumDaemon(config)
        if token is not None:  # pre-seed the persisted token (reuse/auth tests)
            cdir = root / "claustrum"
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / "token").write_text(token, encoding="utf-8")
        created.append(daemon)
        return daemon

    try:
        yield _make
    finally:
        for daemon in created:
            await daemon.aclose()
        for root in roots:
            for pidf in (root / "claustrum").glob("*.pid"):
                try:
                    pid = int(pidf.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            shutil.rmtree(root, ignore_errors=True)


async def test_spawn_connects_and_reports_status(make_daemon):
    """ensure() spawns a daemon, returns a live client, and status() is healthy."""
    daemon = make_daemon()
    client = await daemon.ensure()

    assert (await client.ping())["pong"] is True
    status = daemon.status()
    assert status == {
        "enabled": True,
        "running": True,
        "socket": str(daemon.socket_path),
        "version": "fake-claustrum-bin-0",
        "error": None,
    }

    await daemon.aclose()
    assert daemon.client is None


async def test_ensure_is_idempotent(make_daemon):
    """A second ensure() returns the same client without respawning."""
    daemon = make_daemon()
    first = await daemon.ensure()
    second = await daemon.ensure()
    assert first is second


async def test_token_and_dir_permissions(make_daemon):
    """The state dir is 0700 and the token file is 0600."""
    daemon = make_daemon()
    await daemon.ensure()

    cdir = daemon.socket_path.parent
    token_file = cdir / "token"
    assert token_file.exists()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(cdir.stat().st_mode) == 0o700
    assert len(token_file.read_text(encoding="utf-8")) == 64  # secrets.token_hex(32)


async def test_reuses_running_daemon_without_respawn(make_daemon):
    """A fresh daemon object connects to the already-running one, no spawn.

    The second object is given a bogus binary: if ensure() tried to spawn it
    would raise DaemonSpawnError, so a clean connect proves the connect-first
    path and that the persisted token authenticates against the live daemon.
    """
    first = make_daemon()
    await first.ensure()
    root = first.socket_path.parent.parent
    token = (root / "claustrum" / "token").read_text(encoding="utf-8")

    second = make_daemon(state_dir=root, binary="/nonexistent/claustrum-binary")
    client = await second.ensure()
    assert (await client.ping())["pong"] is True
    # Same persisted token was reused (never regenerated under a live daemon).
    assert (root / "claustrum" / "token").read_text(encoding="utf-8") == token


async def test_auth_rejected_by_running_daemon(make_daemon, fake_claustrum):
    """A running daemon that rejects the persisted token raises AuthRejected."""
    fake = await fake_claustrum(token="the-real-token")
    daemon = make_daemon(socket_path=fake.socket_path, token="a-different-token")

    with pytest.raises(AuthRejected):
        await daemon.ensure()
    assert daemon.client is None
    assert "rejected" in (daemon.status()["error"] or "")


async def test_spawn_launcher_failure(make_daemon, monkeypatch):
    """A launcher that exits non-zero surfaces as DaemonSpawnError."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_EXIT", "3")
    daemon = make_daemon()

    with pytest.raises(DaemonSpawnError) as excinfo:
        await daemon.ensure()
    assert "exited 3" in str(excinfo.value)
    assert daemon.status()["error"]


async def test_binary_not_found(make_daemon):
    """A missing binary surfaces as DaemonSpawnError after the connect attempt."""
    daemon = make_daemon(binary="/nonexistent/claustrum-binary")

    with pytest.raises(DaemonSpawnError) as excinfo:
        await daemon.ensure()
    assert "not found" in str(excinfo.value)
    assert daemon.client is None


async def test_spawn_then_never_listens_times_out(make_daemon, monkeypatch):
    """A daemon that detaches but never binds the socket → DaemonUnreachable."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_NO_LISTEN", "1")
    daemon = make_daemon(spawn_timeout_seconds=0.5)

    with pytest.raises(DaemonUnreachable):
        await daemon.ensure()
    assert "never accepted" in (daemon.status()["error"] or "")


async def test_spawn_launcher_hang_times_out(make_daemon, monkeypatch):
    """A launcher that never detaches is killed and surfaces DaemonSpawnError."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_HANG_LAUNCHER", "1")
    daemon = make_daemon(spawn_timeout_seconds=0.5)

    with pytest.raises(DaemonSpawnError) as excinfo:
        await daemon.ensure()
    assert "did not detach" in str(excinfo.value)


async def test_spawned_daemon_rejects_token(make_daemon, monkeypatch):
    """A spawned daemon that comes up with the wrong token → AuthRejected."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_BAD_TOKEN", "1")
    daemon = make_daemon(spawn_timeout_seconds=2.0)

    with pytest.raises(AuthRejected):
        await daemon.ensure()
    assert "rejected our token" in (daemon.status()["error"] or "")


async def test_empty_token_file_is_regenerated(make_daemon):
    """An empty/blank persisted token is treated as absent and regenerated."""
    daemon = make_daemon(token="")  # seed a blank token file
    await daemon.ensure()
    assert len((daemon.socket_path.parent / "token").read_text(encoding="utf-8")) == 64


async def test_custom_version_surfaced(make_daemon, monkeypatch):
    """server.version is reflected into the daemon status."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_VERSION", "fake-9.9.9")
    daemon = make_daemon()
    await daemon.ensure()
    assert daemon.status()["version"] == "fake-9.9.9"


async def test_ensure_revalidates_and_recovers_dead_client(make_daemon):
    """A cached-but-dead connection is dropped and re-established on ensure()."""
    daemon = make_daemon()
    first = await daemon.ensure()
    await first.close()  # connection dies, but the daemon process stays up

    second = await daemon.ensure()  # revalidation ping fails → reconnect
    assert second is not first
    assert (await second.ping())["pong"] is True


async def test_probe_reports_and_clears_dead_client(make_daemon):
    """probe() pings the cached client and clears it (running=False) if dead."""
    daemon = make_daemon()
    await daemon.ensure()
    assert (await daemon.probe())["running"] is True

    await daemon.client.close()  # connection dies under the daemon
    status = await daemon.probe()
    assert status["running"] is False
    assert "lost" in (status["error"] or "")
    assert daemon.client is None


async def test_concurrent_ensure_spawns_once(make_daemon):
    """The lifecycle lock makes concurrent ensure() calls share one daemon."""
    daemon = make_daemon(spawn_timeout_seconds=3.0)
    results = await asyncio.gather(*[daemon.ensure() for _ in range(5)])
    assert all(client is results[0] for client in results)
    assert (await results[0].ping())["pong"] is True


# -- -keep-children flag (CL-8) --------------------------------------------


async def _capture_spawn_argv(make_daemon, monkeypatch, **kwargs) -> tuple:
    """Run ensure() with create_subprocess_exec stubbed to capture the spawn argv."""
    captured: dict[str, tuple] = {}

    async def fake_exec(*args, **_kw):
        captured["argv"] = args
        raise OSError("short-circuit after capturing argv")  # → DaemonSpawnError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    daemon = make_daemon(**kwargs)
    with pytest.raises(DaemonSpawnError):
        await daemon.ensure()
    return captured["argv"]


async def test_spawn_includes_keep_children_by_default(make_daemon, monkeypatch):
    argv = await _capture_spawn_argv(make_daemon, monkeypatch)  # keep_children defaults True
    assert "-keep-children" in argv


async def test_spawn_omits_keep_children_when_disabled(make_daemon, monkeypatch):
    argv = await _capture_spawn_argv(make_daemon, monkeypatch, keep_children=False)
    assert "-keep-children" not in argv


# -- env-sentinel scrub (claustrum daemonize collision) --------------------


async def _capture_spawn_call(make_daemon, monkeypatch, **kwargs) -> dict:
    """Run ensure() with create_subprocess_exec stubbed to capture args + kwargs."""
    captured: dict = {}

    async def fake_exec(*args, **kw):
        captured["argv"] = args
        captured["kwargs"] = kw
        raise OSError("short-circuit after capturing call")  # → DaemonSpawnError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    daemon = make_daemon(**kwargs)
    with pytest.raises(DaemonSpawnError):
        await daemon.ensure()
    return captured


async def test_spawn_scrubs_ambient_daemon_sentinel(make_daemon, monkeypatch):
    """An ambient CLAUDE_SSH_DAEMON_CHILD must never reach the spawned daemon.

    The claude-ssh / ccd-cli daemon Clauster may run under exports that var to its
    descendants; claustrum reuses the same name as its re-exec sentinel, so leaking
    it makes the launcher skip its -token-fd read and exit 1. Unrelated env survives.
    """
    monkeypatch.setenv("CLAUDE_SSH_DAEMON_CHILD", "1")
    monkeypatch.setenv("CLAUSTRUM_DAEMON_CHILD", "1")
    monkeypatch.setenv("CLAUSTRUM_TOKEN_PIPE", "9")
    monkeypatch.setenv("CLAUSTRUM_KEEP_THIS", "ok")
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "must-not-leak-to-the-daemon")
    env = (await _capture_spawn_call(make_daemon, monkeypatch))["kwargs"]["env"]
    assert "CLAUDE_SSH_DAEMON_CHILD" not in env
    assert "CLAUSTRUM_DAEMON_CHILD" not in env
    assert "CLAUSTRUM_TOKEN_PIPE" not in env
    assert "CLAUSTER_SESSION_SECRET" not in env  # Clauster secret scrubbed (procutil.child_env)
    assert env.get("CLAUSTRUM_KEEP_THIS") == "ok"  # unrelated env preserved
