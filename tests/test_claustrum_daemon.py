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
import logging
import os
import shutil
import signal
import stat
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import psutil
import pytest

from clauster.claustrum_client import AuthRejected, DaemonUnreachable
from clauster.claustrum_daemon import ClaustrumDaemon, DaemonSpawnError
from clauster.config import ClausterConfig

# The daemon lifecycle runs on Windows too: the fake binary detaches (DETACHED_PROCESS
# instead of fork) and serves a named pipe there, advertised via rpc.pipe. Only the
# POSIX file-mode assertions below stay Unix-only — Windows has no 0o600/0o700 bits.
_POSIX_MODE_BITS = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file-mode bits (0o600/0o700) are not enforced on Windows",
)

# On Windows CreateProcess/shutil.which can't run the extensionless .py directly, so
# the daemon binary points at the same-named .cmd wrapper (mirrors fake_claude).
FAKE_BIN = (
    Path(__file__).resolve().parent
    / "fixtures"
    / ("fake_claustrum_bin.cmd" if sys.platform == "win32" else "fake_claustrum_bin.py")
)


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
                except (ProcessLookupError, OSError):
                    # POSIX raises ProcessLookupError for a dead PID; Windows os.kill
                    # (TerminateProcess) raises OSError when the process is already gone.
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


@_POSIX_MODE_BITS
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
    # `status()` must carry the REASON, not just running:false. This is the most likely
    # real failure (claustrum simply not installed), and it was one of two raise sites
    # that never set `_error` — so /healthz reported `running:false, error:null` and the
    # cause survived only in the lifespan log, where a dashboard user never sees it.
    status = daemon.status()
    assert status["running"] is False
    assert status["error"] and "not found" in status["error"]


async def test_resolve_binary_falls_back_to_managed_install(make_daemon, monkeypatch):
    """When the DEFAULT binary isn't on PATH, `_resolve_binary` uses the managed deps/bin one."""
    from pathlib import Path

    from clauster import deps

    monkeypatch.setattr("clauster.deps.shutil.which", lambda name: None)  # PATH miss
    monkeypatch.setattr(deps.sys, "platform", "linux")  # deterministic variant -> dest "claustrum"
    monkeypatch.setattr(deps.platform, "machine", lambda: "x86_64")
    daemon = make_daemon(binary="claustrum")  # fixture's short state_dir (AF_UNIX path limit)
    state = Path(daemon._config.state_dir)
    # No managed binary yet -> the fallback also misses -> a clear "run deps install" error.
    with pytest.raises(DaemonSpawnError, match="deps install claustrum"):
        daemon._resolve_binary()
    # Place a managed binary; now _resolve_binary returns it.
    managed = deps.managed_bin_dir(state) / "claustrum"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"\x7fELF")
    assert daemon._resolve_binary() == str(managed)


async def test_resolve_binary_prefers_path_over_managed(make_daemon, monkeypatch):
    """An explicit/PATH-resolved binary wins over a managed install (operator control)."""
    monkeypatch.setattr("clauster.deps.shutil.which", lambda name: "/usr/local/bin/claustrum")
    daemon = make_daemon(binary="claustrum")
    assert daemon._resolve_binary() == "/usr/local/bin/claustrum"


async def test_resolve_binary_explicit_missing_binary_does_not_fall_back(make_daemon, monkeypatch):
    """A non-default binary that doesn't resolve raises — never silently runs the managed one.

    An operator who set `claustrum.binary: /opt/claustrum-v2` must see it's missing, not get a
    different version substituted from the managed install.
    """
    from pathlib import Path

    from clauster import deps

    monkeypatch.setattr("clauster.deps.shutil.which", lambda name: None)
    monkeypatch.setattr(deps.sys, "platform", "linux")
    monkeypatch.setattr(deps.platform, "machine", lambda: "x86_64")
    daemon = make_daemon(binary="/opt/claustrum-v2")
    # A managed binary IS installed, but the explicit config must NOT fall back to it.
    managed = deps.managed_bin_dir(Path(daemon._config.state_dir)) / "claustrum"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"\x7fELF")
    with pytest.raises(DaemonSpawnError, match="explicit claustrum.binary must resolve"):
        daemon._resolve_binary()


async def test_spawn_then_never_listens_times_out(make_daemon, monkeypatch):
    """A daemon that detaches but never binds the socket → ensure() fails closed."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_NO_LISTEN", "1")
    # Windows process spawn (the fake's .cmd → python → detached-child Popen) can take ~1s
    # under CI load; 0.5s is plenty on POSIX. The generous win32 budget lets the launcher detach
    # so the connect poll runs and hits the intended never-accepted timeout in the normal case.
    daemon = make_daemon(spawn_timeout_seconds=3.0 if sys.platform == "win32" else 0.5)

    # The launcher and the connect poll share one budget (see _connect_or_spawn), so which
    # timeout fires depends only on whether the fake detaches before the budget runs out.
    # A fast detach surfaces the connect poll's "never accepted" (DaemonUnreachable); a slow
    # one — seen on loaded Windows AND macOS runners — trips the launcher's own "did not detach"
    # (DaemonSpawnError) first. Both are the intended fail-closed outcome for a daemon that never
    # binds, so pin THAT contract on every platform, not which timeout won the race: a fixed
    # budget can't make that deterministic, but the two recognized failure messages can.
    with pytest.raises((DaemonUnreachable, DaemonSpawnError)):
        await daemon.ensure()
    error = daemon.status()["error"] or ""
    assert "never accepted" in error or "did not detach" in error


async def test_spawn_launcher_hang_times_out(make_daemon, monkeypatch):
    """A launcher that never detaches is killed and surfaces DaemonSpawnError."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_HANG_LAUNCHER", "1")
    daemon = make_daemon(spawn_timeout_seconds=0.5)

    with pytest.raises(DaemonSpawnError) as excinfo:
        await daemon.ensure()
    assert "did not detach" in str(excinfo.value)


async def test_spawn_launcher_hang_reaps_the_tree_on_windows(make_daemon, monkeypatch):
    """A timed-out launcher is reaped as a TREE on Windows, not just the pid we hold.

    The launcher is a `.cmd`/shim → python → detached-child chain there, and `kill()`
    reaps only its argument — so a launcher killed mid-detach would leave that chain
    running with our log file open.
    """
    monkeypatch.setenv("FAKE_CLAUSTRUM_HANG_LAUNCHER", "1")
    monkeypatch.setattr("clauster.claustrum_daemon.procutil.is_windows", lambda: True)
    killed: list[int] = []
    monkeypatch.setattr(
        "clauster.claustrum_daemon.procutil.force_kill_tree", lambda pid: killed.append(pid)
    )
    daemon = make_daemon(spawn_timeout_seconds=0.5)

    with pytest.raises(DaemonSpawnError):
        await daemon.ensure()
    assert killed, "the launcher's tree must be reaped on Windows"


async def test_spawn_launcher_hang_tree_kill_failure_still_raises(make_daemon, monkeypatch):
    """A failing reap must not mask the DaemonSpawnError this path exists to raise."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_HANG_LAUNCHER", "1")
    monkeypatch.setattr("clauster.claustrum_daemon.procutil.is_windows", lambda: True)

    def _boom(pid: int) -> None:
        # psutil's error family descends from Exception, NOT OSError — which is exactly why
        # the production guard is `except Exception`. An OSError here would pass even
        # against a too-narrow guard.
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr("clauster.claustrum_daemon.procutil.force_kill_tree", _boom)
    daemon = make_daemon(spawn_timeout_seconds=0.5)

    with pytest.raises(DaemonSpawnError) as excinfo:
        await daemon.ensure()
    assert "did not detach" in str(excinfo.value)


async def test_spawn_launcher_hang_does_not_tree_kill_on_posix(make_daemon, monkeypatch):
    """POSIX keeps the plain `kill()` — the launcher we hold is the process to stop."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_HANG_LAUNCHER", "1")
    monkeypatch.setattr("clauster.claustrum_daemon.procutil.is_windows", lambda: False)
    killed: list[int] = []
    monkeypatch.setattr(
        "clauster.claustrum_daemon.procutil.force_kill_tree", lambda pid: killed.append(pid)
    )
    daemon = make_daemon(spawn_timeout_seconds=0.5)

    with pytest.raises(DaemonSpawnError):
        await daemon.ensure()
    assert killed == [], "POSIX must not force-kill the tree"


async def test_spawned_daemon_rejects_token(make_daemon, monkeypatch):
    """A spawned daemon that comes up with the wrong token → AuthRejected."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_BAD_TOKEN", "1")
    daemon = make_daemon(spawn_timeout_seconds=2.0)

    with pytest.raises(AuthRejected):
        await daemon.ensure()
    assert "rejected our token" in (daemon.status()["error"] or "")


async def test_empty_token_file_is_regenerated(make_daemon, monkeypatch):
    """An empty/blank persisted token is treated as absent and regenerated."""
    # A seeded-blank file now hits the O_EXCL-loser wait-out-the-winner poll (it must
    # confirm the blank isn't a winner mid-write before replacing); shrink the window
    # so this test doesn't pay the full ~1s.
    monkeypatch.setattr("clauster.claustrum_daemon._TOKEN_READ_ATTEMPTS", 3)
    monkeypatch.setattr("clauster.claustrum_daemon._TOKEN_READ_DELAY", 0.0)
    daemon = make_daemon(token="")  # seed a blank token file
    await daemon.ensure()
    assert len((daemon.socket_path.parent / "token").read_text(encoding="utf-8")) == 64


@_POSIX_MODE_BITS
def test_read_or_create_token_creates_with_o_excl(make_daemon):
    """Item-4 (#408): absent token → a fresh 64-char hex created at 0600."""
    daemon = make_daemon()
    daemon._prepare_dir()
    token = daemon._read_or_create_token()
    assert len(token) == 64
    assert daemon._token_path.read_text(encoding="utf-8") == token
    assert stat.S_IMODE(daemon._token_path.stat().st_mode) == 0o600


def test_read_or_create_token_adopts_existing_never_clobbers(make_daemon):
    """An already-persisted token is returned verbatim — never truncated/rewritten.

    The old exists()-then-O_TRUNC path could clobber a live daemon's token on a
    two-instance/restart race. The O_EXCL path reads the winner instead, so a
    pre-existing token is adopted byte-for-byte.
    """
    pre = "a" * 64
    daemon = make_daemon(token=pre)  # fixture pre-seeds <state>/claustrum/token
    daemon._prepare_dir()
    assert daemon._read_or_create_token() == pre
    assert daemon._token_path.read_text(encoding="utf-8") == pre  # not rewritten


def test_read_or_create_token_loser_adopts_race_winner(make_daemon):
    """A start that loses the O_EXCL race adopts the winner's already-written token."""
    daemon = make_daemon()
    daemon._prepare_dir()
    winner = "b" * 64
    daemon._token_path.write_text(winner, encoding="utf-8")  # winner already won + wrote
    assert daemon._read_or_create_token() == winner  # adopted, not clobbered
    assert daemon._token_path.read_text(encoding="utf-8") == winner


def test_read_or_create_token_waits_out_midwrite_winner_never_clobbers(make_daemon, monkeypatch):
    """The blank-window race: a winner created the file but hasn't written yet.

    This is the bug the O_EXCL alone did not fix — a loser that reads the blank file
    ONCE and immediately replaces it clobbers the winner's freshly-created token.
    The fix waits out the winner (poll loop) so a momentarily-blank file is treated
    as 'winner mid-write', and the loser adopts the winner's token once it lands.
    """
    daemon = make_daemon()
    daemon._prepare_dir()
    winner = "c" * 64
    # The winner created the file EMPTY (O_EXCL) but its os.write hasn't landed yet.
    daemon._token_path.write_text("", encoding="utf-8")

    real_read = daemon._read_existing_token
    polls = {"n": 0}

    def _winner_writes_after_two_polls() -> str | None:
        # First two reads still see the blank mid-write file; on the third the winner's
        # bytes have landed. A single-read-then-replace implementation would have
        # clobbered after read #1 — this asserts we wait and adopt instead.
        polls["n"] += 1
        if polls["n"] >= 3:
            daemon._token_path.write_text(winner, encoding="utf-8")
        return real_read()

    monkeypatch.setattr(daemon, "_read_existing_token", _winner_writes_after_two_polls)
    assert daemon._read_or_create_token() == winner  # adopted the winner, never replaced
    assert daemon._token_path.read_text(encoding="utf-8") == winner


@_POSIX_MODE_BITS
def test_read_or_create_token_replaces_truly_abandoned_blank(make_daemon, monkeypatch):
    """A file that stays blank for the whole wait is abandoned → atomically replaced."""
    # Shrink the wait so the test doesn't sleep ~1s.
    monkeypatch.setattr("clauster.claustrum_daemon._TOKEN_READ_ATTEMPTS", 3)
    monkeypatch.setattr("clauster.claustrum_daemon._TOKEN_READ_DELAY", 0.0)
    daemon = make_daemon()
    daemon._prepare_dir()
    daemon._token_path.write_text("", encoding="utf-8")  # stale, never filled
    token = daemon._read_or_create_token()
    assert len(token) == 64  # a fresh token was generated
    assert daemon._token_path.read_text(encoding="utf-8") == token  # replaced in place
    assert stat.S_IMODE(daemon._token_path.stat().st_mode) == 0o600
    # No orphaned temp file left behind.
    assert not list(daemon._token_path.parent.glob("token.*.tmp"))


def test_replace_blank_token_adopts_last_moment_winner(make_daemon):
    """`_replace_blank_token` re-checks for a winner that landed during the wait."""
    daemon = make_daemon()
    daemon._prepare_dir()
    winner = "d" * 64
    daemon._token_path.write_text(winner, encoding="utf-8")  # a winner already there
    # Called directly: the last-moment re-read finds the winner and adopts it rather
    # than overwriting, never touching a temp file.
    assert daemon._replace_blank_token("e" * 64) == winner
    assert daemon._token_path.read_text(encoding="utf-8") == winner
    assert not list(daemon._token_path.parent.glob("token.*.tmp"))


def test_replace_blank_token_cleans_up_temp_on_replace_failure(make_daemon, monkeypatch):
    """A failure during the temp-write/replace must not orphan a `.tmp` file."""
    daemon = make_daemon()
    daemon._prepare_dir()
    daemon._token_path.write_text("", encoding="utf-8")  # blank → no winner to adopt

    def _boom(*_a, **_k):
        raise OSError("replace failed")

    monkeypatch.setattr("clauster.claustrum_daemon.os.replace", _boom)
    with pytest.raises(OSError, match="replace failed"):
        daemon._replace_blank_token("f" * 64)
    # The orphaned temp file was cleaned up despite the failure.
    assert not list(daemon._token_path.parent.glob("token.*.tmp"))


async def test_custom_version_surfaced(make_daemon, monkeypatch):
    """server.capabilities' version is reflected into the daemon status."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_VERSION", "fake-9.9.9")
    daemon = make_daemon()
    await daemon.ensure()
    assert daemon.status()["version"] == "fake-9.9.9"


async def test_connect_against_legacy_daemon(make_daemon, monkeypatch):
    """Connect succeeds against a pre-v1.10 daemon that still serves server.version."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_VERSION", "fake-1.9.0")
    monkeypatch.setenv("FAKE_CLAUSTRUM_LEGACY_VERSION", "1")
    daemon = make_daemon()
    client = await daemon.ensure()
    # clauster probes via capabilities, so the connection is identical whether or
    # not the daemon still exposes the removed server.version method.
    assert daemon.status()["version"] == "fake-1.9.0"
    assert (await client.ping())["pong"] is True


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


# -- Windows -listen-pipe + spawn flags (#894) -----------------------------
# Patching `sys.platform` mutates the shared singleton, so the pre-spawn `_connect`
# also takes the client's win32 pipe branch → no `rpc.pipe` in the tmp state dir →
# DaemonUnreachable → falls through to `_spawn`, where the stub captures the call.


def _simulate_win32(monkeypatch) -> None:
    """Force the win32 spawn branch on a POSIX host.

    Also passes `shutil.which` through: under simulated win32 it applies PATHEXT/.exe
    resolution and can't find the extensionless fake binary (a test-only artifact — a
    real Windows deploy resolves `claustrum.exe`), which would fail _resolve_binary
    before the argv is built.
    """
    monkeypatch.setattr("clauster.claustrum_daemon.sys.platform", "win32")
    monkeypatch.setattr("clauster.deps.shutil.which", lambda name: name)


async def test_spawn_appends_listen_pipe_on_win32(make_daemon, monkeypatch):
    _simulate_win32(monkeypatch)
    argv = await _capture_spawn_argv(make_daemon, monkeypatch)
    assert "-listen-pipe" in argv  # the Windows client dials a pipe, not AF_UNIX


@pytest.mark.skipif(
    sys.platform == "win32", reason="asserts the POSIX _spawn branch, which Windows never takes"
)
async def test_spawn_omits_listen_pipe_on_posix(make_daemon, monkeypatch):
    # POSIX byte-identity guard: the AF_UNIX path must never grow -listen-pipe.
    argv = await _capture_spawn_argv(make_daemon, monkeypatch)
    assert "-listen-pipe" not in argv


async def test_spawn_no_new_session_on_win32(make_daemon, monkeypatch):
    # start_new_session is a POSIX-only detach; omitted entirely on Windows (claustrum
    # self-daemonizes via DETACHED_PROCESS), so no POSIX-only kwarg reaches the spawn.
    _simulate_win32(monkeypatch)
    kwargs = (await _capture_spawn_call(make_daemon, monkeypatch))["kwargs"]
    assert "start_new_session" not in kwargs


@pytest.mark.skipif(
    sys.platform == "win32", reason="asserts the POSIX _spawn branch, which Windows never takes"
)
async def test_spawn_uses_start_new_session_on_posix(make_daemon, monkeypatch):
    # POSIX regression guard: the launcher detaches into its own session.
    kwargs = (await _capture_spawn_call(make_daemon, monkeypatch))["kwargs"]
    assert kwargs["start_new_session"] is True


async def test_spawn_uses_token_file_not_fd_on_win32(make_daemon, monkeypatch):
    # fd 0 is not a usable token handle for the Go daemon on Windows, so the token is
    # handed over via a read-then-unlinked -token-file, never -token-fd.
    _simulate_win32(monkeypatch)
    argv = await _capture_spawn_argv(make_daemon, monkeypatch)
    assert "-token-file" in argv
    assert "-token-fd" not in argv


async def test_spawn_win32_launcher_gets_no_stdin_pipe(make_daemon, monkeypatch):
    # With -token-file the launcher needs no stdin pipe, so it is spawned with DEVNULL.
    _simulate_win32(monkeypatch)
    kwargs = (await _capture_spawn_call(make_daemon, monkeypatch))["kwargs"]
    assert kwargs["stdin"] == asyncio.subprocess.DEVNULL


async def test_spawn_win32_token_file_write_failure_fails_closed(make_daemon, monkeypatch):
    # A failure writing the Windows token handoff must fail closed (DaemonSpawnError) and
    # leave no token fragment on disk — it must not escape as a raw OSError.
    _simulate_win32(monkeypatch)
    daemon = make_daemon()

    real_write = Path.write_text

    def _boom(self, *args, **kwargs):
        if self.name.startswith("token-handoff."):
            raise OSError("disk full")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _boom)

    with pytest.raises(DaemonSpawnError):
        await daemon.ensure()
    assert not list(daemon.socket_path.parent.glob("token-handoff.*.tmp"))


async def test_spawn_win32_success_skips_stdin_write(make_daemon, monkeypatch):
    # On win32 the token went via -token-file (stdin=DEVNULL), so a successful spawn must
    # skip the POSIX stdin block entirely — touching proc.stdin (None) would AttributeError.
    _simulate_win32(monkeypatch)

    class _FakeProc:
        stdin = None  # DEVNULL yields no writer; the win32 path must never touch it

        async def wait(self):
            return 0

        def kill(self):  # pragma: no cover - not reached on the success path
            pass

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    daemon = make_daemon()
    daemon._prepare_dir()
    # Returns cleanly (launcher "exited" 0) without dereferencing proc.stdin.
    await daemon._spawn("t" * 64, asyncio.get_running_loop().time() + 5.0)


@pytest.mark.skipif(
    sys.platform == "win32", reason="asserts the POSIX _spawn branch, which Windows never takes"
)
async def test_spawn_uses_token_fd_over_stdin_on_posix(make_daemon, monkeypatch):
    # POSIX regression guard: the token goes over stdin (-token-fd 0), nothing on disk.
    call = await _capture_spawn_call(make_daemon, monkeypatch)
    assert "-token-fd" in call["argv"]
    assert "-token-file" not in call["argv"]
    assert call["kwargs"]["stdin"] == asyncio.subprocess.PIPE


async def test_prepare_dir_swallows_chmod_failure_and_warns(make_daemon, monkeypatch, caplog):
    # Best-effort tightening: a chmod failure on the state dir is logged, never raised, so a
    # daemon on an odd filesystem still starts (the dir is created with 0700-intent regardless).
    import pathlib

    def _boom(self, *_a, **_k):
        raise OSError("chmod denied")

    monkeypatch.setattr(pathlib.Path, "chmod", _boom)
    daemon = make_daemon()
    with caplog.at_level(logging.WARNING, logger="clauster.claustrum_daemon"):
        daemon._prepare_dir()  # must not raise despite the chmod OSError
    assert any(
        "could not chmod" in r.getMessage() and "0700" in r.getMessage() for r in caplog.records
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the POSIX _spawn stdin branch, which Windows never takes",
)
async def test_spawn_posix_no_stdin_pipe_fails_closed(make_daemon, monkeypatch):
    # POSIX: if the launcher subprocess has no stdin writer, the token can't be handed over —
    # fail closed with DaemonSpawnError rather than silently spawning an unauthenticated daemon.
    class _FakeProc:
        stdin = None  # PIPE should always yield a writer; a None writer must fail closed

        async def wait(self):  # pragma: no cover - the None-stdin guard raises before wait()
            return 0

        def kill(self):  # pragma: no cover - not reached
            pass

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    daemon = make_daemon()
    daemon._prepare_dir()
    with pytest.raises(DaemonSpawnError, match="no stdin pipe"):
        await daemon._spawn("t" * 64, asyncio.get_running_loop().time() + 5.0)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts the POSIX _spawn stdin branch, which Windows never takes",
)
async def test_spawn_posix_stdin_write_failure_is_swallowed(make_daemon, monkeypatch, caplog):
    # A write/drain failure to the launcher's stdin is non-fatal (the launcher may have already
    # read its token and closed fd 0): _spawn swallows it, logs a debug line, and lets the
    # returncode decide. Here wait()==0, so a clean spawn returns without propagating the OSError.
    class _FakeStdin:
        def write(self, _data):
            raise OSError("broken pipe: launcher already closed fd 0")

        async def drain(self):  # pragma: no cover - write() raises first
            pass

        def close(self):  # pragma: no cover - unreached after write() raises
            pass

    class _FakeProc:
        stdin = _FakeStdin()

        async def wait(self):
            return 0

        def kill(self):  # pragma: no cover - not reached on the exit-0 path
            pass

    async def fake_exec(*_args, **_kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    daemon = make_daemon()
    daemon._prepare_dir()
    with caplog.at_level(logging.DEBUG, logger="clauster.claustrum_daemon"):
        await daemon._spawn("t" * 64, asyncio.get_running_loop().time() + 5.0)  # must not raise
    assert any("writing token to launcher stdin failed" in r.getMessage() for r in caplog.records)


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


def test_af_unix_in_use_matches_os_name():
    # The seam mirrors os.name so the length gate is testable via a patchable boundary
    # instead of monkeypatching the global os.name (which crashes pathlib on Windows).
    import os as _os

    from clauster.claustrum_daemon import _af_unix_in_use

    assert _af_unix_in_use() == (_os.name == "posix")


def test_check_unix_socket_path_rejects_too_long(monkeypatch):
    # A socket path over the AF_UNIX sun_path limit fails early with a clear error (#914).
    # Force the AF_UNIX branch via the seam (NOT os.name) so the raise-branch runs on a
    # Windows CI runner too — patching os.name="posix" there makes pathlib uninstantiable.
    from clauster import claustrum_daemon as cd

    monkeypatch.setattr(cd, "_af_unix_in_use", lambda: True)
    too_long = Path("/tmp") / ("x" * cd._SUN_PATH_MAX) / "d.sock"
    with pytest.raises(cd.ClaustrumError, match="AF_UNIX limit"):
        cd._check_unix_socket_path(too_long)


def test_check_unix_socket_path_ok_when_short(monkeypatch):
    # Force the AF_UNIX branch and use a guaranteed-short literal path — a real macOS tmp_path
    # (/private/var/folders/…) is itself over 104 bytes and would spuriously trip the gate.
    from clauster import claustrum_daemon as cd

    monkeypatch.setattr(cd, "_af_unix_in_use", lambda: True)
    cd._check_unix_socket_path(Path("/tmp/d.sock"))  # comfortably short — must not raise


def test_check_unix_socket_path_skipped_off_posix(monkeypatch):
    # Windows dials a named pipe (no sun_path limit), so the check is a no-op there.
    from clauster import claustrum_daemon as cd

    monkeypatch.setattr(cd, "_af_unix_in_use", lambda: False)
    long_sock = Path("/tmp") / ("x" * 300) / "d.sock"
    cd._check_unix_socket_path(long_sock)  # non-posix → no-op, must not raise
