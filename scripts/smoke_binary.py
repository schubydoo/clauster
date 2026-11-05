#!/usr/bin/env python3
"""Smoke-test a built standalone ``clauster`` binary: boot it and hit ``/healthz``.

A PyInstaller one-file bundle can build cleanly yet fail at *runtime* when a
dynamically-imported module (uvicorn's loop/protocol backends, jinja2-fragments,
…) was not collected into the bundle. ``--version`` only forces import-time
loading; this script actually starts the server and polls ``/healthz`` — the real
one-file failure mode — so CI catches a broken bundle before it ships.

Cross-OS, standard library only. Usage::

    python scripts/smoke_binary.py <path-to-binary>
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# The committed fake `claude` (used by the E2E suite) satisfies clauster's
# startup binary-resolution probe without a real Claude install on the runner.
FAKE_CLAUDE = (
    REPO / "tests" / "fixtures" / "fake_claude" / ("claude.cmd" if os.name == "nt" else "claude")
)


def _free_port() -> int:
    """Grab an OS-assigned free loopback port, then release it for the server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthy(port: int, proc: subprocess.Popen, timeout: float = 45.0) -> None:
    """Poll ``/healthz`` until it returns 200, the process dies, or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise SystemExit(f"binary exited early (code {proc.returncode}):\n{out}")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET", "/healthz")
            if conn.getresponse().status == 200:
                return
        except OSError:
            time.sleep(0.3)
        finally:
            conn.close()
    raise SystemExit(f"binary never became healthy on :{port} within {timeout:.0f}s")


def _check_version(binary: Path) -> None:
    """Assert ``<binary> --version`` runs and identifies clauster (import-graph loads)."""
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or "clauster" not in result.stdout:
        raise SystemExit(
            f"`--version` failed (rc={result.returncode}):\n{result.stdout}{result.stderr}"
        )
    print(f"version: {result.stdout.strip()}")


def _check_pty_keeper_routed(binary: Path) -> None:
    """Assert the hidden ``__pty-keeper__`` frozen entry point routes to the keeper CLI.

    A PTY (Interactive) bridge is launched as ``<binary> __pty-keeper__ …`` only in a
    frozen build, because ``sys.executable`` is the binary itself there and the source
    form ``<python> -m clauster.pty_keeper`` cannot run. If that subcommand isn't wired
    into the binary, clauster's top-level argparse rejects it with ``unrecognized
    arguments`` and every PTY session dies the instant it spawns — a binary-only failure
    no unit test can see (the unit tests build the keeper argv by hand). Invoke it with no
    further args: a wired-in route reaches ``clauster.pty_keeper``'s OWN argparse, which
    exits demanding ``--sidecar`` (proving the route); an unwired binary shows clauster's
    top-level error instead. Cross-OS: the route + argparse run before any POSIX-only PTY
    work.
    """
    result = subprocess.run(
        [str(binary), "__pty-keeper__"], capture_output=True, text=True, timeout=60
    )
    out = result.stdout + result.stderr
    # Key on the actual failure condition, not a cosmetic string: an unwired binary
    # shows clauster's top-level "unrecognized arguments", while a wired route reaches
    # the keeper's own parser, whose required-arg error always names `--sidecar`. Both
    # signals track the CLI contract, so a future rename of the keeper's argparse `prog`
    # can't turn this into a false alarm.
    if "unrecognized arguments" in out or "--sidecar" not in out:
        raise SystemExit(
            f"`__pty-keeper__` did not route to the keeper CLI (rc={result.returncode}) — "
            f"PTY (Interactive) sessions would fail to launch on the binary:\n{out}"
        )
    print("pty-keeper subcommand: routed OK")


def _check_boot(binary: Path) -> None:
    """Boot ``<binary> run`` against a throwaway config and confirm ``/healthz`` serves.

    ``clauster run`` probes ``claude --version`` before serving and exits 2 if it
    fails, so boot needs ``python3`` (POSIX) / ``python`` (Windows ``claude.cmd``)
    on PATH to execute the fake-claude fixture — both present on GitHub runners.
    """
    # ignore_cleanup_errors: on Windows the boot opens a SQLite state DB
    # (clauster.db + its -wal/-shm sidecars); the hard proc.terminate() below skips
    # the app's clean engine-dispose, so the OS may still hold the file when this
    # dir is torn down (WinError 32). Cleanup is irrelevant to the smoke check — the
    # runner wipes temp between jobs — so don't let a teardown lock fail the test.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        (tmp / "projects").mkdir()
        port = _free_port()
        cfg = tmp / "clauster.yml"
        cfg.write_text(
            f"host: 127.0.0.1\n"
            f"port: {port}\n"
            f"projects_root: {tmp / 'projects'}\n"
            f"state_dir: {tmp / 'state'}\n"
            f"claude:\n  binary: {FAKE_CLAUDE}\n",
            encoding="utf-8",
        )
        # Isolate HOME/USERPROFILE so the runtime never reads/writes a real
        # ~/.claude.json on the runner.
        home = tmp / "home"
        home.mkdir()
        proc = subprocess.Popen(
            [str(binary), "run", "-c", str(cfg)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
            start_new_session=(os.name != "nt"),
        )
        try:
            _wait_healthy(port, proc)
            print(f"healthz OK on :{port}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


# An LD_LIBRARY_PATH the user "had" before starting the binary. A spawned claude must get
# exactly this back: not the binary's unpack dir prepended, and not dropped. The directory
# does not exist, which is harmless on a library search path.
_USER_LIB = os.path.join(os.sep, "smoke-1708-user-lib")


def _write_config(tmp: Path, port: int) -> Path:
    """Write a throwaway loopback config whose ``claude`` is the env-dumping fake."""
    cfg = tmp / "clauster.yml"
    cfg.write_text(
        f"host: 127.0.0.1\n"
        f"port: {port}\n"
        f"projects_root: {tmp / 'projects'}\n"
        f"state_dir: {tmp / 'state'}\n"
        f"claude:\n  binary: {FAKE_CLAUDE}\n",
        encoding="utf-8",
    )
    return cfg


def _post_json(port: int, path: str, body: dict) -> str:
    """POST ``body`` as JSON to the local server; return the status and any error detail."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(
            "POST",
            path,
            body=json.dumps(body),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
        )
        response = conn.getresponse()
        payload = response.read().decode("utf-8", "replace")
        return str(response.status) + ("" if response.status < 400 else f" {payload[:300]}")
    finally:
        conn.close()


def _read_dumps(dump_dir: Path) -> list[dict]:
    """Return every environment the fake claude recorded (one JSON file per run)."""
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(dump_dir.glob("env-*.json"))]


def _assert_clean(dumps: list[dict], *, expect_ld: str | None, route: str) -> None:
    """Fail unless every recorded child env is free of the binary's PyInstaller state."""
    for dump in dumps:
        env = dump["env"]
        problems = [name for name in env if name.upper().startswith("_PYI_")]
        if "LD_LIBRARY_PATH_ORIG" in env:
            problems.append("LD_LIBRARY_PATH_ORIG")
        if env.get("LD_LIBRARY_PATH") != expect_ld:
            problems.append(f"LD_LIBRARY_PATH={env.get('LD_LIBRARY_PATH')!r}")
        if problems:
            raise SystemExit(
                f"{route}: `claude {' '.join(dump['argv'])}` inherited the binary's runtime "
                f"env (expected LD_LIBRARY_PATH={expect_ld!r}): {problems}"
            )


def _wait_for_argv(dump_dir: Path, wanted: set[str], timeout: float = 45.0) -> list[dict]:
    """Wait until the fake claude has run once with each ``argv[0]`` in ``wanted``."""
    deadline = time.monotonic() + timeout
    while True:
        dumps = _read_dumps(dump_dir)
        seen = {d["argv"][0] for d in dumps if d["argv"]}
        if wanted <= seen:
            return dumps
        if time.monotonic() > deadline:
            raise SystemExit(
                f"fake claude never ran as {sorted(wanted - seen)} (saw {sorted(seen)})"
            )
        time.sleep(0.3)


def _check_child_env(binary: Path) -> None:
    """Assert the BUILT binary hands every spawned claude a clean environment (issue 1708).

    The PyInstaller bootloader prepends its temporary unpack dir to ``LD_LIBRARY_PATH``
    and sets ``_PYI_*`` state before Python starts; a child that inherits them loads the
    bundle's libraries, and a re-run of the binary from it can fail to start. The fake
    claude dumps its env on every run. This boots the binary, starts a standard bridge
    and (POSIX) a pty bridge — the pty one is spawned by the ``__pty-keeper__``
    re-invocation, so the keeper's own spawn path is covered — then runs ``doctor``
    without an ``LD_LIBRARY_PATH`` to cover the "remove it" case.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        home = tmp / "home"
        dumps_dir = tmp / "dumps"
        for d in (home, dumps_dir, tmp / "projects" / "std", tmp / "projects" / "ptyproj"):
            d.mkdir(parents=True)
        port = _free_port()
        cfg = _write_config(tmp, port)
        env = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "LD_LIBRARY_PATH": _USER_LIB,
            "FAKE_CLAUDE_ENV_DUMP_DIR": str(dumps_dir),
            # The fake bridge exits right after dumping, so nothing outlives the check.
            "FAKE_CLAUDE_MODE": "crash",
        }
        env.pop("LD_LIBRARY_PATH_ORIG", None)
        proc = subprocess.Popen(
            [str(binary), "run", "-c", str(cfg)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=(os.name != "nt"),
        )
        try:
            _wait_healthy(port, proc)
            wanted = {"--version", "remote-control"}
            # Trust is written to the isolated HOME's ~/.claude.json, never the runner's.
            print(f"trust projects: HTTP {_post_json(port, '/api/projects/trust-all', {})}")
            status = _post_json(port, "/api/instances", {"project": "std"})
            print(f"spawn standard bridge: HTTP {status}")
            if os.name != "nt":  # the pty keeper needs a POSIX pty (ConPTY is an optional extra)
                body = {"project": "ptyproj", "resume_mode": "pty"}
                print(f"spawn pty bridge: HTTP {_post_json(port, '/api/instances', body)}")
                wanted.add("--remote-control")
            dumps = _wait_for_argv(dumps_dir, wanted)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        _assert_clean(dumps, expect_ld=_USER_LIB, route="run")
        print(
            f"child env clean with a user LD_LIBRARY_PATH: {len(dumps)} spawns, {sorted(wanted)}"
        )

        doctor_dumps = tmp / "doctor-dumps"
        doctor_dumps.mkdir()
        env = {**env, "FAKE_CLAUDE_ENV_DUMP_DIR": str(doctor_dumps)}
        env.pop("LD_LIBRARY_PATH")
        subprocess.run(
            [str(binary), "doctor", "-c", str(cfg)], env=env, capture_output=True, timeout=120
        )
        dumps = _wait_for_argv(doctor_dumps, {"--version"}, timeout=5)
        _assert_clean(dumps, expect_ld=None, route="doctor")
        print(f"child env clean without an LD_LIBRARY_PATH: {len(dumps)} spawns")


def main() -> int:
    """Run the version + boot + child-env smoke checks against the binary in ``argv[1]``."""
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_binary.py <path-to-binary>")
    binary = Path(sys.argv[1]).resolve()
    if not binary.is_file():
        raise SystemExit(f"binary not found: {binary}")
    if not FAKE_CLAUDE.is_file():
        raise SystemExit(f"fake claude fixture not found: {FAKE_CLAUDE}")
    _check_version(binary)
    _check_pty_keeper_routed(binary)
    _check_boot(binary)
    _check_child_env(binary)
    print("smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
