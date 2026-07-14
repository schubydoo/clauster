"""Tests for the PTY keeper (true-resume sidecar). POSIX only — there is no `pty` on Windows."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty keeper is POSIX-only")

# A fake bridge: prints a connect URL (the keeper scrapes it off the PTY), then
# idles until signalled — the flag-form bridge's shape, minus the real network.
_FAKE_BRIDGE = """
import signal, sys, time
sys.stdout.write("Continue at https://claude.ai/code/session_01KEEPERTESTAAAAAAAAAA\\r\\n")
sys.stdout.flush()
stop = {"v": False}
signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("v", True))
signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("v", True))
for _ in range(2400):
    if stop["v"]:
        break
    time.sleep(0.05)
"""


def _read(sidecar: Path) -> dict:
    try:
        return json.loads(sidecar.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def test_keeper_publishes_pid_and_connect_url(tmp_path: Path) -> None:
    """The keeper runs the bridge under a PTY and writes a discoverable sidecar."""
    bridge = tmp_path / "fake_bridge.py"
    bridge.write_text(_FAKE_BRIDGE)
    sidecar = tmp_path / "k.json"
    keeper = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "clauster.pty_keeper",
            "--sidecar",
            str(sidecar),
            "--",
            sys.executable,
            str(bridge),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # mimic Clauster's detached launch
    )
    try:
        info: dict = {}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            info = _read(sidecar)
            if info.get("connect_url"):
                break
            time.sleep(0.1)

        assert info.get("state") == "ready"
        assert info.get("connect_url") == "https://claude.ai/code/session_01KEEPERTESTAAAAAAAAAA"
        assert info.get("session_id") == "session_01KEEPERTESTAAAAAAAAAA"
        assert isinstance(info.get("bridge_pid"), int)
        assert info["bridge_pid"] != keeper.pid  # the bridge is the keeper's child
        assert info.get("keeper_pid") == keeper.pid

        # Stopping the bridge (what Clauster does) makes the keeper self-exit.
        os.kill(info["bridge_pid"], signal.SIGINT)
        keeper.wait(timeout=10)
        assert _read(sidecar).get("state") == "exited"
    finally:
        if keeper.poll() is None:
            try:
                os.killpg(os.getpgid(keeper.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            keeper.wait(timeout=5)


def test_keeper_records_error_when_bridge_cannot_spawn(tmp_path: Path) -> None:
    """A bridge that fails to exec is recorded as state=error, not a hang."""
    sidecar = tmp_path / "k.json"
    rc = subprocess.run(
        [
            sys.executable,
            "-m",
            "clauster.pty_keeper",
            "--sidecar",
            str(sidecar),
            "--",
            str(tmp_path / "does-not-exist-binary"),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=15,
    )
    assert rc.returncode != 0
    info = _read(sidecar)
    assert info.get("state") == "error"
    assert info.get("error")


def test_keeper_main_requires_bridge_argv(tmp_path: Path) -> None:
    """Invoking the keeper with no bridge argv after `--` is a usage error."""
    rc = subprocess.run(
        [sys.executable, "-m", "clauster.pty_keeper", "--sidecar", str(tmp_path / "k.json")],
        capture_output=True,
        timeout=15,
    )
    assert rc.returncode != 0
    assert b"bridge argv" in rc.stderr


# ----- in-process (so the keeper module is covered, unlike the subprocess tests) -----


@pytest.fixture
def _restore_sighup():
    """run_keeper sets SIGHUP=SIG_IGN in-process; restore the runner's handler after."""
    old = signal.getsignal(signal.SIGHUP)
    yield
    signal.signal(signal.SIGHUP, old)


def test_run_keeper_inprocess_scrapes_url(tmp_path: Path, _restore_sighup) -> None:
    """run_keeper scrapes the connect URL off the PTY and records the bridge's exit."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    bridge = [
        sys.executable,
        "-c",
        "import sys,time;"
        "sys.stdout.write('at https://claude.ai/code/session_01INPROCAAAAAAAAA\\r\\n');"
        "sys.stdout.flush(); time.sleep(0.6)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path))
    assert rc == 0
    info = _read(sidecar)
    assert info["session_id"] == "session_01INPROCAAAAAAAAA"
    assert info["connect_url"].endswith("session_01INPROCAAAAAAAAA")
    assert info["state"] == "exited"
    assert isinstance(info["bridge_pid"], int)


def test_run_keeper_scrapes_url_from_screen_when_raw_fragments_it(
    tmp_path: Path, _restore_sighup
) -> None:
    """#665: with the live tap on (120x40 PTY), claude prints the connect URL via
    cursor-positioning escapes that fragment the raw byte stream — the raw regex (and a plain
    ANSI-strip) miss it. The keeper recovers it from the pyte-reassembled screen and publishes
    it to the discovery sidecar so the dashboard surfaces 'Open in Claude' instead of nothing."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    screen = tmp_path / "k.screen.json"
    # Emit the URL fragmented like the captured failure: "...cod" + a wrong char, then a
    # cursor-reposition (\x1b[22G = column 22) back over it, overwritten with "e/session_…".
    # Raw bytes read "...codX\x1b[22Ge/session_…" — regex misses, ANSI-strip leaves "codXe/…"
    # which also misses; only the emulator renders the whole "code/session_…" line.
    bridge = [
        sys.executable,
        "-c",
        "import sys,time;"
        "sys.stdout.buffer.write("
        "b'start\\r\\nhttps://claude.ai/codX\\x1b[22Ge/session_01SCREENSCRAPE665\\r\\n');"
        "sys.stdout.flush(); time.sleep(0.6)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path), screen_sidecar=screen)
    assert rc == 0
    info = _read(sidecar)
    assert info["session_id"] == "session_01SCREENSCRAPE665"
    assert info["connect_url"] == "https://claude.ai/code/session_01SCREENSCRAPE665"


def test_run_keeper_inprocess_spawn_failure(tmp_path: Path, _restore_sighup) -> None:
    """A bridge that can't be exec'd is recorded as an error, not raised."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    rc = pty_keeper.run_keeper([str(tmp_path / "nope")], sidecar, cwd=str(tmp_path))
    assert rc != 0
    assert _read(sidecar)["state"] == "error"


def test_main_strips_leading_separator(tmp_path: Path, _restore_sighup) -> None:
    """main() drops the `--` separator argparse keeps in REMAINDER before exec."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    rc = pty_keeper.main(["--sidecar", str(sidecar), "--", sys.executable, "-c", "pass"])
    assert rc == 0
    assert _read(sidecar)["state"] == "exited"


def test_main_requires_bridge_argv_inprocess(tmp_path: Path) -> None:
    """No bridge argv after the options is a usage error, not a hang."""
    from clauster import pty_keeper

    with pytest.raises(SystemExit):
        pty_keeper.main(["--sidecar", str(tmp_path / "k.json")])


def test_proc_start_returns_none_on_error(monkeypatch) -> None:
    """A failure resolving the bridge's start time degrades to None, never raises."""
    from clauster import pty_keeper

    def _boom(_pid):
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr("clauster.procutil.proc_create_time", _boom)
    assert pty_keeper._proc_start(1234) is None


def test_write_sidecar_swallows_oserror(tmp_path: Path) -> None:
    """A sidecar write into a missing directory must never take the bridge down."""
    from clauster import pty_keeper

    pty_keeper._write_sidecar(tmp_path / "no" / "such" / "k.json", {"a": 1})  # no raise


def test_run_keeper_openpty_failure(tmp_path: Path, monkeypatch, _restore_sighup) -> None:
    """A PTY allocation failure is recorded as an error and returned, not raised."""
    import pty as _pty

    from clauster import pty_keeper

    def _boom():
        raise OSError("no ptys available")

    monkeypatch.setattr(_pty, "openpty", _boom)
    sidecar = tmp_path / "k.json"
    rc = pty_keeper.run_keeper([sys.executable, "-c", "pass"], sidecar, cwd=str(tmp_path))
    assert rc == 70
    assert _read(sidecar)["state"] == "error"


def test_run_keeper_url_timeout(tmp_path: Path, monkeypatch, _restore_sighup) -> None:
    """If the bridge never prints a URL, the keeper stops buffering and still exits cleanly."""
    from clauster import pty_keeper

    monkeypatch.setattr(pty_keeper, "_URL_TIMEOUT", 0.2)
    sidecar = tmp_path / "k.json"
    rc = pty_keeper.run_keeper(
        [sys.executable, "-c", "import time; time.sleep(0.6)"], sidecar, cwd=str(tmp_path)
    )
    assert rc == 0
    info = _read(sidecar)
    assert info["connect_url"] is None
    assert info["state"] == "exited"


def test_run_keeper_swallows_oserror_closing_master_on_exit(
    tmp_path: Path, monkeypatch, _restore_sighup
) -> None:
    """The teardown ``os.close(master)`` is best-effort: an OSError there (a double-close /
    already-reaped fd) must be swallowed so the keeper still returns the bridge's rc, never
    raising out of run_keeper. Mirrors the atomicio double-close swallow."""
    import pty as _pty

    from clauster import pty_keeper

    real_openpty = _pty.openpty
    master_box: list[int] = []

    def _capture_openpty():
        master, slave = real_openpty()
        master_box[:] = [master]
        return master, slave

    real_close = os.close

    def _close(fd):  # raise only for the captured master fd's teardown close
        if master_box and fd == master_box[0]:
            master_box.clear()  # one-shot: don't re-fire if the fd number is reused before restore
            real_close(fd)  # actually close it so the fd does not leak in this process
            raise OSError("master already closed")
        return real_close(fd)

    monkeypatch.setattr(_pty, "openpty", _capture_openpty)
    monkeypatch.setattr(pty_keeper.os, "close", _close)

    sidecar = tmp_path / "k.json"
    # short-lived bridge: exits on its own so run_keeper reaches the teardown close
    rc = pty_keeper.run_keeper(
        [sys.executable, "-c", "import time; time.sleep(0.3)"], sidecar, cwd=str(tmp_path)
    )

    assert rc == 0  # the OSError on the master teardown close was swallowed, rc still returned
    assert _read(sidecar)["state"] == "exited"


def test_run_keeper_drains_output_after_url_found(tmp_path: Path, _restore_sighup) -> None:
    """Output arriving AFTER the connect URL is still drained (the post-URL chunk arm)."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    bridge = [
        sys.executable,
        "-c",
        "import sys,time;"
        "sys.stdout.write('at https://claude.ai/code/session_01POSTURLAAAAAAAA\\r\\n');"
        "sys.stdout.flush(); time.sleep(0.2);"
        "sys.stdout.write('more output after the url\\r\\n'); sys.stdout.flush(); time.sleep(0.2)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path))
    assert rc == 0
    info = _read(sidecar)
    assert info["session_id"] == "session_01POSTURLAAAAAAAA"  # URL scraped from the first chunk
    assert info["state"] == "exited"  # the later chunk was drained without re-scanning


def test_run_keeper_tolerates_empty_read(tmp_path: Path, monkeypatch, _restore_sighup) -> None:
    """An empty master read (EOF/POLLHUP while the bridge is briefly still alive) is a no-op."""
    import os as _os

    from clauster import pty_keeper

    real_read = _os.read
    state = {"first": True}

    def fake_read(fd: int, n: int) -> bytes:
        # Target only the master drain read (it uses a 65536 buffer); return one empty read
        # WITHOUT consuming, so the real bytes are still there for the next (real) read.
        if n == 65536 and state["first"]:
            state["first"] = False
            return b""
        return real_read(fd, n)

    monkeypatch.setattr(pty_keeper.os, "read", fake_read)
    sidecar = tmp_path / "k.json"
    bridge = [
        sys.executable,
        "-c",
        "import sys,time; sys.stdout.write('hi\\r\\n'); sys.stdout.flush(); time.sleep(0.3)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path))
    assert rc == 0  # the empty read was tolerated; the bridge ran to a clean exit
    assert _read(sidecar)["state"] == "exited"


def test_run_keeper_backs_off_on_a_blocking_read(
    tmp_path: Path, monkeypatch, _restore_sighup
) -> None:
    """A transient BlockingIOError from the master read backs off and retries, not crashes."""
    import os as _os

    from clauster import pty_keeper

    real_read = _os.read
    state = {"first": True}

    def fake_read(fd: int, n: int) -> bytes:
        # One transient BlockingIOError on the master drain read (65536 buffer), then real.
        # On Linux the nonblocking master raises this between chunks, covering the
        # except+backoff branch; macOS pty timing doesn't, so without this patch that branch
        # is Linux-only. Raise WITHOUT consuming so the real bytes remain for the retry.
        if n == 65536 and state["first"]:
            state["first"] = False
            raise BlockingIOError("resource temporarily unavailable")
        return real_read(fd, n)

    monkeypatch.setattr(pty_keeper.os, "read", fake_read)
    sidecar = tmp_path / "k.json"
    bridge = [
        sys.executable,
        "-c",
        "import sys,time; sys.stdout.write('hi\\r\\n'); sys.stdout.flush(); time.sleep(0.3)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path))
    assert rc == 0  # the transient error was tolerated; the bridge ran to a clean exit
    assert _read(sidecar)["state"] == "exited"


# ----- live-screen tap (#534) -----------------------------------------------


def test_run_keeper_writes_redacted_screen_sidecar(tmp_path: Path, _restore_sighup) -> None:
    """With a screen sidecar, the keeper republishes a redacted, cells-only frame (#534)."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    screen = tmp_path / "k.screen.json"
    bridge = [
        sys.executable,
        "-c",
        "import sys,time;"
        "sys.stdout.write('hello sk-abcdef0123456789 world\\r\\n');"
        "sys.stdout.flush(); time.sleep(0.6)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path), screen_sidecar=screen)
    assert rc == 0
    frame = _read(screen)
    assert frame["state"] == "exited"  # the final flush ran on bridge exit
    assert frame["seq"] >= 1
    joined = "".join(frame["screen"]["rows"])
    assert "hello" in joined and "world" in joined  # rendered cells, not raw bytes
    assert "sk-abcdef0123456789" not in joined and "<redacted>" in joined  # secret masked
    assert frame["screen"]["cols"] == 120 and frame["screen"]["rows_count"] == 40


def test_run_keeper_sets_pty_winsize_to_screen_geometry(tmp_path: Path, _restore_sighup) -> None:
    """With the tap on, the bridge's PTY is sized to the pyte geometry (#534): the TUI then
    redraws against the same rows the screen models, so its cursor-addressed footer can't
    leave stale duplicates. The bridge reports the size it actually sees on its PTY."""
    from clauster import pty_keeper

    sidecar = tmp_path / "k.json"
    screen = tmp_path / "k.screen.json"
    bridge = [
        sys.executable,
        "-c",
        "import os,sys,time;"
        "ts=os.get_terminal_size(sys.stdout.fileno());"
        "sys.stdout.write('SIZE %dx%d\\r\\n' % (ts.columns, ts.lines));"
        "sys.stdout.flush(); time.sleep(0.6)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path), screen_sidecar=screen)
    assert rc == 0
    joined = "".join(_read(screen)["screen"]["rows"])
    assert "SIZE 120x40" in joined  # bridge saw the fixed 120x40 PTY (matches SCREEN_COLS/ROWS)


def test_run_keeper_tolerates_winsize_failure(
    tmp_path: Path, monkeypatch, _restore_sighup
) -> None:
    """A failed PTY winsize ioctl is best-effort: it must be swallowed, never aborting the
    spawn (#534). Raise only on TIOCSWINSZ so the child's TIOCSCTTY still works."""
    import fcntl

    from clauster import pty_keeper

    real_ioctl = fcntl.ioctl

    def _ioctl(fd, request, *a, **k):
        import termios

        if request == termios.TIOCSWINSZ:
            raise OSError("winsize nope")
        return real_ioctl(fd, request, *a, **k)

    monkeypatch.setattr("fcntl.ioctl", _ioctl)
    sidecar = tmp_path / "k.json"
    screen = tmp_path / "k.screen.json"
    rc = pty_keeper.run_keeper(
        [sys.executable, "-c", "import time; time.sleep(0.3)"],
        sidecar,
        cwd=str(tmp_path),
        screen_sidecar=screen,
    )
    assert rc == 0  # the winsize failure was swallowed; the bridge ran normally


def test_run_keeper_screen_unavailable_without_pyte(
    tmp_path: Path, monkeypatch, _restore_sighup
) -> None:
    """Without the optional pyte extra the tap is dormant; the bridge runs normally (#534)."""
    from clauster import pty_keeper

    monkeypatch.setitem(sys.modules, "pyte", None)  # simulate pyte not installed
    sidecar = tmp_path / "k.json"
    screen = tmp_path / "k.screen.json"
    rc = pty_keeper.run_keeper(
        [sys.executable, "-c", "import time; time.sleep(0.3)"],
        sidecar,
        cwd=str(tmp_path),
        screen_sidecar=screen,
    )
    assert rc == 0  # the bridge is unaffected by the missing optional dependency
    info = _read(screen)
    assert info["state"] == "unavailable" and "clauster[pty]" in (info.get("error") or "")
    assert _read(sidecar)["state"] == "exited"  # discovery path unaffected


def test_make_screen_records_unavailable_without_pyte(tmp_path: Path, monkeypatch) -> None:
    """_make_screen returns None and records the reason on the sidecar when pyte is absent."""
    from clauster import pty_keeper

    monkeypatch.setitem(sys.modules, "pyte", None)
    screen = tmp_path / "s.json"
    assert pty_keeper._make_screen(screen) is None
    info = _read(screen)
    assert info["state"] == "unavailable" and "clauster[pty]" in info["error"]


def test_make_screen_none_when_no_live_view_requested(tmp_path: Path) -> None:
    """_make_screen returns None (and writes nothing) when no live-view sidecar was requested.

    With the live view off the keeper keeps the default winsize — where claude prints the
    connect URL contiguously — and scrapes it from the raw bytes, so no pyte screen is built.
    """
    from clauster import pty_keeper

    assert pty_keeper._make_screen(None) is None


def test_write_screen_frame_records_render_failure(tmp_path: Path) -> None:
    """A frame-render error is recorded (not raised) and the seq still advances."""
    from clauster import pty_keeper

    class _BoomScreen:
        def frame(self):  # noqa: ANN202 — test stub
            raise RuntimeError("render boom")

    path = tmp_path / "s.json"
    seq = pty_keeper._write_screen_frame(path, _BoomScreen(), 4, "live")
    assert seq == 5  # the seq advances even on a render failure
    info = _read(path)
    assert info["state"] == "error" and "render" in info["error"] and info["screen"] is None


def test_write_screen_frame_tolerates_unserializable_frame(tmp_path: Path) -> None:
    """A frame json can't serialize (a non-OSError from _write_sidecar) must never crash."""
    from clauster import pty_keeper

    class _BadFrame:
        def frame(self):  # noqa: ANN202 — test stub
            return {"rows": [object()]}  # not JSON-serializable -> json.dumps raises TypeError

    path = tmp_path / "s.json"
    # Must not raise even though _write_sidecar's json.dumps fails on a non-OSError.
    seq = pty_keeper._write_screen_frame(path, _BadFrame(), 0, "live")
    assert seq == 1  # the seq still advances; the failed write is swallowed


def test_run_keeper_screen_throttle_skips_within_interval(
    tmp_path: Path, monkeypatch, _restore_sighup
) -> None:
    """A second update within the flush interval is debounced (the throttle skip arm)."""
    from clauster import pty_keeper

    monkeypatch.setattr(pty_keeper, "_SCREEN_FLUSH_INTERVAL", 100.0)  # force the not-elapsed arm
    sidecar = tmp_path / "k.json"
    screen = tmp_path / "k.screen.json"
    bridge = [
        sys.executable,
        "-c",
        "import sys,time;"
        "sys.stdout.write('first\\r\\n'); sys.stdout.flush(); time.sleep(0.15);"
        "sys.stdout.write('second\\r\\n'); sys.stdout.flush(); time.sleep(0.3)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path), screen_sidecar=screen)
    assert rc == 0
    # The mid-loop second write is throttled (skipped) within the 100s interval, but the exit
    # frame still flushes the final state — so both lines are present in the terminal frame.
    frame = _read(screen)
    assert frame["state"] == "exited"
    joined = "".join(frame["screen"]["rows"])
    assert "first" in joined and "second" in joined


def test_make_screen_records_error_on_generic_failure(tmp_path: Path, monkeypatch) -> None:
    """A non-pyte setup failure is recorded as `error` (not raised, not `unavailable`)."""
    from clauster import pty_keeper

    def _boom():
        raise RuntimeError("emulator setup exploded")

    monkeypatch.setattr(pty_keeper, "PtyScreen", _boom)
    screen = tmp_path / "s.json"
    assert pty_keeper._make_screen(screen) is None
    info = _read(screen)
    assert info["state"] == "error" and "screen init" in info["error"]


def test_run_keeper_tap_failure_never_kills_bridge(
    tmp_path: Path, monkeypatch, _restore_sighup
) -> None:
    """A screen-tap feed() error disables the tap silently; the bridge runs to a clean exit."""
    from clauster import pty_keeper

    class _BoomFeed:
        def feed(self, data):  # noqa: ANN001, ANN202 — test stub
            raise RuntimeError("feed boom")

        def frame(self):  # noqa: ANN202 — test stub
            return {}

    monkeypatch.setattr(pty_keeper, "_make_screen", lambda screen_sidecar: _BoomFeed())
    sidecar = tmp_path / "k.json"
    screen = tmp_path / "k.screen.json"
    bridge = [
        sys.executable,
        "-c",
        "import sys,time; sys.stdout.write('hi\\r\\n'); sys.stdout.flush(); time.sleep(0.3)",
    ]
    rc = pty_keeper.run_keeper(bridge, sidecar, cwd=str(tmp_path), screen_sidecar=screen)
    assert rc == 0  # the bridge completed despite the tap blowing up
    assert _read(sidecar)["state"] == "exited"  # discovery path unaffected
    # The disable is recorded as a terminal `error` status, not a silently-frozen `live` frame.
    disabled = _read(screen)
    assert disabled["state"] == "error" and "feed" in disabled["error"]
    assert disabled["screen"] is None


def test_keeper_drain_feed_failure_without_tap_disables_screen(tmp_path: Path) -> None:
    """A feed() error with a screen but no live-view tap disables the screen and never writes."""
    from clauster import pty_keeper

    class _BoomFeed:
        def feed(self, data):  # noqa: ANN001, ANN202 — test stub
            raise RuntimeError("feed boom")

    base: dict[str, object] = {"state": "starting"}
    drain = pty_keeper._KeeperDrain(base, tmp_path / "k.json", _BoomFeed(), None)
    assert drain._tap is None  # no screen_sidecar => no tap, exercising the False branch

    drain.feed(b"not a url\r\n")

    assert drain._screen is None  # the raising screen was disabled
    assert not (tmp_path / "k.json").exists()  # no sidecar write on a non-URL chunk


# -- Windows ConPTY backend (#892) -----------------------------------------------------
# Driven on POSIX against a scriptable fake pywinpty PtyProcess injected through the
# _load_pty_process seam, so the ConPTY drain/scrape/exit path is covered on the Linux
# gate. The real ConPTY (pywinpty) is exercised on the Windows VM.


def _fake_conpty(
    *,
    chunks=(),
    exit_code=0,
    spawn_error=None,
    read_eof=False,
    close_raises=None,
    alive_reads=None,
    eof_at=(),
    oserror_at=(),
):
    """Build a fake pywinpty ``PtyProcess`` class scripted to emit ``chunks`` then exit.

    ``alive_reads`` models real pywinpty liveness — the OS process handle, independent of
    buffered bytes: ``isalive()`` returns False after that many ``read`` calls even while
    chunks remain, so the exit-with-buffered-tail race is representable. Left None, the
    process reports alive exactly while chunks remain (the simple case).
    """
    calls: list[dict] = []

    class _FakeConPty:
        def __init__(self) -> None:
            self._chunks = list(chunks)
            self.pid = 4321
            self._reads = 0

        @classmethod
        def spawn(cls, argv, cwd=None, env=None, dimensions=(24, 80)):
            calls.append({"argv": argv, "cwd": cwd, "env": env, "dimensions": dimensions})
            if spawn_error is not None:
                raise spawn_error
            return cls()

        def isalive(self) -> bool:
            if alive_reads is not None:
                return self._reads < alive_reads
            return bool(self._chunks)

        def read(self, size: int = 1024) -> str:
            self._reads += 1
            if self._reads in oserror_at:
                raise OSError("conpty channel broke")  # a genuine read-channel error, not idle
            if self._reads in eof_at:
                raise EOFError  # models an idle non-blocking read surfaced as EOFError
            if self._chunks:
                return self._chunks.pop(0)
            if read_eof:
                raise EOFError
            return ""  # non-blocking: no data available

        def terminate(self, force: bool = False) -> None:
            calls.append({"terminated": True})

        def wait(self):
            return exit_code

        def close(self) -> None:
            calls.append({"closed": True})
            if close_raises is not None:
                raise close_raises

    _FakeConPty.calls = calls
    return _FakeConPty


def test_load_pty_process_raises_off_windows() -> None:
    from clauster import pty_keeper

    with pytest.raises(RuntimeError, match="Windows-only"):
        pty_keeper._load_pty_process()


def test_load_pty_process_returns_winpty_class_on_windows(monkeypatch) -> None:
    from clauster import pty_keeper

    class _FakePtyProcess:
        pass

    fake_winpty = types.ModuleType("winpty")
    fake_winpty.PtyProcess = _FakePtyProcess
    monkeypatch.setitem(sys.modules, "winpty", fake_winpty)
    monkeypatch.setattr(pty_keeper.sys, "platform", "win32")

    assert pty_keeper._load_pty_process() is _FakePtyProcess


def test_run_keeper_conpty_scrapes_url_and_records_exit(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    fake = _fake_conpty(
        chunks=["Continue at https://claude.ai/code/session_01CONPTYAAAAAAAAAAAAAA\r\n"],
        exit_code=0,
    )
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(
        ["claude", "--remote-control"], sidecar, str(tmp_path), None
    )
    assert rc == 0
    data = _read(sidecar)
    assert data["state"] == "exited"
    assert data["session_id"] == "session_01CONPTYAAAAAAAAAAAAAA"
    assert data["connect_url"].endswith("session_01CONPTYAAAAAAAAAAAAAA")
    assert data["bridge_pid"] == 4321
    assert fake.calls[0]["argv"] == ["claude", "--remote-control"]
    assert fake.calls[0]["dimensions"] == (pty_keeper.SCREEN_ROWS, pty_keeper.SCREEN_COLS)


def test_run_keeper_conpty_spawn_failure(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    fake = _fake_conpty(spawn_error=OSError("no conpty"))
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    assert rc == 71
    data = _read(sidecar)
    assert data["state"] == "error"
    assert "conpty spawn failed" in data["error"]


def test_run_keeper_conpty_url_timeout_promotes_ready(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    monkeypatch.setattr(pty_keeper, "_URL_TIMEOUT", 0.0)  # deadline immediately past
    fake = _fake_conpty(chunks=["no url in this output\r\n"], exit_code=0)
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    assert rc == 0
    data = _read(sidecar)
    assert data["state"] == "exited"  # ran to completion
    assert data["connect_url"] is None  # never scraped a URL, promoted via the deadline


def test_run_keeper_conpty_writes_screen_sidecar(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    fake = _fake_conpty(
        chunks=[
            "Continue at https://claude.ai/code/session_01SCREENAAAAAAAAAAAAAA\r\n",
            "more output\r\n",
        ],
        exit_code=0,
    )
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    monkeypatch.setattr(pty_keeper, "_SCREEN_FLUSH_INTERVAL", 0.0)  # flush a frame every tick
    sidecar = tmp_path / "b.keeper.json"
    screen = tmp_path / "b.screen.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, screen)
    assert rc == 0
    frame = _read(screen)
    assert frame["state"] in ("live", "exited")
    assert "screen" in frame


def test_run_keeper_conpty_pyte_unavailable_falls_back_to_raw(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    def _no_pyte():
        raise pty_keeper.PyteUnavailableError("install clauster[pty]")

    monkeypatch.setattr(pty_keeper, "PtyScreen", _no_pyte)
    fake = _fake_conpty(
        chunks=["https://claude.ai/code/session_01NOPYTEAAAAAAAAAAAAAA\r\n"], exit_code=0
    )
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    screen = tmp_path / "b.screen.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, screen)
    assert rc == 0
    assert _read(screen)["state"] == "unavailable"  # dormant view explained to a viewer
    assert (
        _read(sidecar)["session_id"] == "session_01NOPYTEAAAAAAAAAAAAAA"
    )  # raw scrape still works


def test_run_keeper_conpty_screen_init_error(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    def _boom():
        raise RuntimeError("pyte exploded")

    monkeypatch.setattr(pty_keeper, "PtyScreen", _boom)
    fake = _fake_conpty(chunks=["https://claude.ai/code/session_01INITERRAAAAAAAAAAA\r\n"])
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    screen = tmp_path / "b.screen.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, screen)
    assert rc == 0
    err = _read(screen)
    assert err["state"] == "error" and "screen init" in err["error"]


def test_run_keeper_conpty_close_error_swallowed(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    fake = _fake_conpty(chunks=["hi\r\n"], exit_code=0, close_raises=OSError("bad handle"))
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    assert rc == 0  # a close() failure during teardown never crashes the keeper
    assert _read(sidecar)["state"] == "exited"


def test_run_keeper_conpty_handles_read_eof(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    fake = _fake_conpty(chunks=["hi\r\n"], exit_code=3, read_eof=True)
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    assert rc == 3
    assert _read(sidecar)["state"] == "exited"


def test_run_keeper_conpty_idle_eoferror_keeps_session(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    # A pywinpty build that surfaces an idle non-blocking read as EOFError must NOT tear down a
    # still-alive bridge (Greptile #903) — the connect URL can still arrive on a later read.
    fake = _fake_conpty(
        chunks=["Continue at https://claude.ai/code/session_01IDLEEOFAAAAAAAAAA\r\n"],
        exit_code=0,
        alive_reads=2,
        eof_at={1},
    )
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    assert rc == 0
    assert _read(sidecar)["session_id"] == "session_01IDLEEOFAAAAAAAAAA"


def test_run_keeper_conpty_read_error_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    # A non-EOF read error (a broken ConPTY channel) while the bridge is still alive must fail
    # closed (Greptile #903): record the error + terminate the undrivable bridge, NOT keep polling
    # and promote a broken stream to "ready".
    fake = _fake_conpty(chunks=["boot\r\n"], exit_code=0, oserror_at={2}, alive_reads=5)
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    data = _read(sidecar)
    assert "conpty read failed" in (data["error"] or "")
    assert data["state"] == "exited"  # broken stream → exited-with-error, never a false "ready"
    assert {"terminated": True} in fake.calls  # the undrivable bridge was terminated


def test_run_keeper_conpty_drains_exit_tail(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    # The bridge handle reports dead (alive_reads=0) while its final chunks — including the
    # connect URL — are still buffered; the loop must read that tail, not stop at isalive().
    fake = _fake_conpty(
        chunks=["boot\r\n", "Continue at https://claude.ai/code/session_01TAILAAAAAAAAAAAAAA\r\n"],
        exit_code=0,
        alive_reads=0,
    )
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    assert rc == 0
    assert _read(sidecar)["session_id"] == "session_01TAILAAAAAAAAAAAAAA"  # buffered tail drained


def test_run_keeper_conpty_idles_then_exits(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    # An empty read while the bridge is still alive → the loop sleeps and re-polls, not exit.
    fake = _fake_conpty(
        chunks=["", "Continue at https://claude.ai/code/session_01IDLEAAAAAAAAAAAAAA\r\n"],
        exit_code=0,
        alive_reads=2,
    )
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    assert rc == 0
    assert _read(sidecar)["session_id"] == "session_01IDLEAAAAAAAAAAAAAA"


def test_run_keeper_conpty_pywinpty_import_failure(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    def _boom():
        raise ImportError("no winpty")

    monkeypatch.setattr(pty_keeper, "_load_pty_process", _boom)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper._run_keeper_conpty(["claude"], sidecar, None, None)
    assert rc == 72
    data = _read(sidecar)
    assert data["state"] == "error" and "pywinpty unavailable" in data["error"]


def test_run_keeper_dispatches_to_conpty_on_win32(tmp_path: Path, monkeypatch) -> None:
    from clauster import pty_keeper

    monkeypatch.setattr(pty_keeper.sys, "platform", "win32")
    fake = _fake_conpty(
        chunks=["https://claude.ai/code/session_01DISPATCHAAAAAAAAAAA\r\n"], exit_code=0
    )
    monkeypatch.setattr(pty_keeper, "_load_pty_process", lambda: fake)
    sidecar = tmp_path / "b.keeper.json"
    rc = pty_keeper.run_keeper(["claude"], sidecar, cwd=str(tmp_path))
    assert rc == 0
    assert _read(sidecar)["session_id"] == "session_01DISPATCHAAAAAAAAAAA"
