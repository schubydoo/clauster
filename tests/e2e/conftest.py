"""Fixtures for the browser E2E suite.

These tests drive a REAL headless Chromium (Playwright) against a REAL clauster
process — unlike the rest of the suite, which exercises logic in-process via
Starlette's TestClient. The suite is opt-in (run with ``scripts/e2e.sh``) and is
excluded from the default/CI run via ``--ignore=tests/e2e`` in pyproject, so it
never adds a browser dependency or latency to the required ``tests`` gate.

Each server fixture launches ``clauster run`` as a subprocess bound to loopback on
a free port, waits for ``/healthz``, yields the base URL, and tears the process
down. Loopback needs no auth, so a plain server renders the dashboard directly; the
``auth_server`` fixture enables password auth to exercise the login flow.
"""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from clauster.auth import hash_password, make_hasher

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


def _start_server(tmp: Path, projects_root: Path, extra: str = "") -> Iterator[str]:
    port = _free_port()
    cfg = tmp / "clauster.yml"
    cfg.write_text(
        f"host: 127.0.0.1\n"
        f"port: {port}\n"
        f"projects_root: {projects_root}\n"
        f"state_dir: {tmp / 'state'}\n"
        f"claude:\n  binary: {FAKE_CLAUDE}\n"
        f"{extra}"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "clauster", "run", "-c", str(cfg)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(port, proc)
        yield base_url
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
    yield from _start_server(tmp, projects_tree)


@pytest.fixture(scope="module")
def auth_server(tmp_path_factory: pytest.TempPathFactory, projects_tree: Path) -> Iterator[str]:
    """A loopback clauster with password auth enabled — exercises the login flow."""
    tmp = tmp_path_factory.mktemp("e2e-auth")
    password_hash = hash_password(make_hasher(), E2E_PASSWORD)
    extra = (
        f'auth:\n  enabled: true\n  password_required: true\n  password_hash: "{password_hash}"\n'
    )
    yield from _start_server(tmp, projects_tree, extra)
