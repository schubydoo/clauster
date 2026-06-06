"""Fixtures for the browser E2E suite.

These tests drive a REAL headless Chromium (Playwright) against a REAL clauster
process — unlike the rest of the suite, which exercises logic in-process via
Starlette's TestClient. The suite is opt-in (run with ``scripts/e2e.sh``) and is
excluded from the default/CI run via ``--ignore=tests/e2e`` in pyproject, so it
never adds a browser dependency or latency to the required ``tests`` gate.

Each server fixture launches ``clauster run`` as a subprocess bound to loopback on
a free port (with an isolated ``HOME`` so the trust flow never touches the real
``~/.claude.json``), waits for ``/healthz``, and tears the process down. Loopback
needs no auth, so a plain server renders the dashboard directly; the ``auth_server``
fixture enables password auth to exercise the login flow; the function-scoped
``bridge_server`` yields a :class:`Server` (URL + ``state_dir``) for driving the
real bridge lifecycle with a clean slate per test.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from clauster.auth import hash_password, make_hasher


class Server(NamedTuple):
    """A running clauster process under test.

    ``url`` is the loopback base URL; ``state_dir`` is its ``state_dir`` (so
    bridge tests can read the spawned bridge's ``--debug-file`` sidecar — the
    fake ``claude`` writes the launch argv to ``<debug-file>.argv.json``, which
    lets a test assert the flags Clauster passed through from the UI).
    """

    url: str
    state_dir: Path


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude" / "claude"

# Known password for the auth_server fixture (hashed fresh per run).
E2E_PASSWORD = "e2e-secret-123"


@pytest.fixture(scope="session")
def e2e_password() -> str:
    """The plaintext password that authenticates against ``auth_server``."""
    return E2E_PASSWORD


def _free_port() -> int:
    """Grab an OS-assigned free loopback port, then release it for the server."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, proc: subprocess.Popen, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"clauster exited early (code {proc.returncode}):\n{out}")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            conn.request("GET", "/healthz")
            if conn.getresponse().status == 200:
                return
        except OSError:
            time.sleep(0.2)
        finally:
            conn.close()
    raise RuntimeError(f"clauster never became ready on port {port} within {timeout}s")


def _start_server(tmp: Path, projects_root: Path, extra: str = "") -> Iterator[Server]:
    port = _free_port()
    state_dir = tmp / "state"
    cfg = tmp / "clauster.yml"
    cfg.write_text(
        f"host: 127.0.0.1\n"
        f"port: {port}\n"
        f"projects_root: {projects_root}\n"
        f"state_dir: {state_dir}\n"
        f"claude:\n  binary: {FAKE_CLAUDE}\n"
        f"{extra}"
    )
    # Isolate HOME so the trust-on-start flow writes to a throwaway ``~/.claude.json``
    # instead of the real one (the host's ``claude`` account is the live dogfood
    # deploy — trusting a fixture project must never touch it). Clauster resolves the
    # trusted-dirs file from HOME at import, and each server is a fresh subprocess, so
    # an env override fully isolates it.
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "clauster", "run", "-c", str(cfg)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env={**os.environ, "HOME": str(home)},
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(port, proc)
        yield Server(base_url, state_dir)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def projects_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A projects_root with three discoverable projects: a git repo, a CLAUDE.md
    project, and a plain one — rendered as cards in the dashboard grid."""
    root = tmp_path_factory.mktemp("e2e-projects")
    (root / "alpha" / ".git").mkdir(parents=True)
    beta = root / "beta"
    beta.mkdir()
    (beta / "CLAUDE.md").write_text("# beta\n")
    (root / "gamma").mkdir()
    return root


@pytest.fixture(scope="module")
def open_server(tmp_path_factory: pytest.TempPathFactory, projects_tree: Path) -> Iterator[str]:
    """A loopback clauster with no auth — the dashboard renders directly."""
    tmp = tmp_path_factory.mktemp("e2e-open")
    for server in _start_server(tmp, projects_tree):
        yield server.url


@pytest.fixture(scope="module")
def auth_server(tmp_path_factory: pytest.TempPathFactory, projects_tree: Path) -> Iterator[str]:
    """A loopback clauster with password auth enabled — exercises the login flow."""
    tmp = tmp_path_factory.mktemp("e2e-auth")
    password_hash = hash_password(make_hasher(), E2E_PASSWORD)
    extra = (
        f'auth:\n  enabled: true\n  password_required: true\n  password_hash: "{password_hash}"\n'
    )
    for server in _start_server(tmp, projects_tree, extra):
        yield server.url


@pytest.fixture
def bridge_server(
    tmp_path_factory: pytest.TempPathFactory, projects_tree: Path
) -> Iterator[Server]:
    """A loopback clauster for driving the real bridge lifecycle (trust → start → stop).

    Function-scoped (unlike the module-scoped read-only servers above) so each
    bridge test gets a clean trust store, instance registry, and state_dir — a
    spawned/trusted bridge in one test never leaks into the next. Yields the full
    :class:`Server` so tests can read the launch argv from ``state_dir``.
    """
    tmp = tmp_path_factory.mktemp("e2e-bridge")
    yield from _start_server(tmp, projects_tree)


@pytest.fixture
def bridge_server_pty(
    tmp_path_factory: pytest.TempPathFactory, projects_tree: Path
) -> Iterator[Server]:
    """A loopback clauster defaulting to pty (true-resume) mode for the bridge lifecycle.

    Like :func:`bridge_server` but with ``claude.resume_mode: pty`` so a started
    bridge comes up under a real :mod:`clauster.pty_keeper` running the ``claude
    --remote-control`` flag form — the true-resume path (Resume re-spawns it with
    ``--continue``). pty mode is POSIX-only; the E2E host is Linux.
    """
    tmp = tmp_path_factory.mktemp("e2e-bridge-pty")
    yield from _start_server(tmp, projects_tree, extra="  resume_mode: pty\n")
