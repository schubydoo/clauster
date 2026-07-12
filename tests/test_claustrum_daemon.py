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


async def test_spawn_then_never_listens_times_out(make_daemon, monkeypatch):
    """A daemon that detaches but never binds the socket → ensure() fails closed."""
    monkeypatch.setenv("FAKE_CLAUSTRUM_NO_LISTEN", "1")
    # Windows process spawn (the fake's .cmd → python → detached-child Popen) can take ~1s
    # under CI load; 0.5s is plenty on POSIX. The generous win32 budget lets the launcher detach
    # so the connect poll runs and hits the intended never-accepted timeout in the normal case.
    daemon = make_daemon(spawn_timeout_seconds=3.0 if sys.platform == "win32" else 0.5)

    # On POSIX the launcher detaches instantly, so this deterministically hits the never-accepted
    # poll timeout (DaemonUnreachable). On Windows the launcher's own detach bounds the budget, so
    # an unusually slow detach can instead surface as DaemonSpawnError ("did not detach") — both
    # are the intended fail-closed outcome, so accept either there rather than depend on which
    # timeout fires first (a fixed budget can't make that deterministic; the exception tuple can).
    with pytest.raises((DaemonUnreachable, DaemonSpawnError)):
        await daemon.ensure()
    if sys.platform == "win32":
        assert daemon.status()["error"]
    else:
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
    monkeypatch.setattr("clauster.claustrum_daemon.shutil.which", lambda name: name)


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
