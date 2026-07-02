"""Fixtures for the browser E2E suite.

These tests drive a REAL headless Chromium (via the ``agent-browser`` CLI) against a REAL clauster
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
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from _driver import AgentBrowser

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
    # The running ``clauster run`` subprocess. Defaulted so the many ``Server(url,
    # state_dir)`` constructions stay valid; the connection-lost test uses it to kill
    # the server mid-session and watch the dashboard's retry banner appear.
    proc: subprocess.Popen | None = None


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude" / "claude"

# Where a failed test's screenshot lands (gitignored). The CI workflow uploads this
# dir as an artifact on failure so a headless, displayless run is still debuggable.
ARTIFACT_DIR = Path(__file__).resolve().parent / "_artifacts"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Stash each phase's report on the item so fixtures can see pass/fail at teardown.

    pytest doesn't otherwise expose the test outcome to a fixture's finalizer; this
    records ``rep_setup`` / ``rep_call`` so the ``browser`` fixture can screenshot only
    when the test actually failed.
    """
    outcome = yield
    setattr(item, f"rep_{outcome.get_result().when}", outcome.get_result())


# Known password for the auth_server fixture (hashed fresh per run).
E2E_PASSWORD = "e2e-secret-123"


@pytest.fixture(scope="session")
def e2e_password() -> str:
    """The plaintext password that authenticates against ``auth_server``."""
    return E2E_PASSWORD


@pytest.fixture
def browser(request: pytest.FixtureRequest) -> Iterator[AgentBrowser]:
    """A fresh ``agent-browser`` session per test (closed on teardown for isolation).

    The suite runs serially (``scripts/e2e.sh`` clears the xdist addopts), so a single
    browser session at a time is safe; closing it per test resets cookies/storage so a
    login in one test never leaks into the next. On failure, snapshots the page into
    :data:`ARTIFACT_DIR` *before* closing the session so CI can upload it.
    """
    driver = AgentBrowser()
    try:
        yield driver
    finally:
        rep_call = getattr(request.node, "rep_call", None)
        rep_setup = getattr(request.node, "rep_setup", None)
        failed = (rep_call is not None and rep_call.failed) or (
            rep_setup is not None and rep_setup.failed
        )
        if failed:
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^\w.-]", "_", request.node.nodeid)
            if not driver.screenshot(str(ARTIFACT_DIR / f"{safe_name}.png")):
                print(
                    f"\n[e2e] could not capture screenshot for {request.node.nodeid}",
                    file=sys.stderr,
                )
        driver.close()


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


def _start_server(
    tmp: Path,
    projects_root: Path,
    extra: str = "",
    extra_env: dict[str, str] | None = None,
) -> Iterator[Server]:
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
    env = {**os.environ, "HOME": str(home)}
    # ``extra_env`` lets a fixture select the fake ``claude``'s behaviour (e.g.
    # FAKE_CLAUDE_MODE=stderr_error to induce a real spawn failure, or
    # FAKE_CLAUDE_LOG_EXTRA to emit an ANSI + session-token line for the log-tail
    # redaction check). The fake reads these from its own environment.
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, "-m", "clauster", "run", "-c", str(cfg)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(port, proc)
        yield Server(base_url, state_dir, proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _build_projects_tree(root: Path) -> Path:
    """Populate ``root`` with three discoverable projects: a git repo, a CLAUDE.md
    project, and a plain one — rendered as cards in the dashboard grid."""
    (root / "alpha" / ".git").mkdir(parents=True)
    beta = root / "beta"
    beta.mkdir()
    (beta / "CLAUDE.md").write_text("# beta\n")
    (root / "gamma").mkdir()
    return root


@pytest.fixture(scope="module")
def projects_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A shared, read-only projects_root for the module-scoped (read-only) servers."""
    return _build_projects_tree(tmp_path_factory.mktemp("e2e-projects"))


@pytest.fixture
def mutable_projects_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh, per-test projects_root for write-path tests (a saved CLAUDE.md, a
    created project). The module-scoped ``projects_tree`` is shared, so an on-disk
    write there would leak into later tests in the module; this gives each function a
    clean tree so write tests can't observe each other's mutations."""
    return _build_projects_tree(tmp_path_factory.mktemp("e2e-projects-mut"))


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


@pytest.fixture(scope="module")
def reaper_server(tmp_path_factory: pytest.TempPathFactory, projects_tree: Path) -> Iterator[str]:
    """A loopback clauster with the ghost-environment reaper UI enabled.

    Sets ``reaper.ui_enabled: true`` so the dashboard panel renders and
    ``/api/environments/ghosts`` is no longer gated off (404). Read-only — the gating
    assertions never mutate state — so a shared module-scoped server is fine.
    """
    tmp = tmp_path_factory.mktemp("e2e-reaper")
    for server in _start_server(tmp, projects_tree, extra="reaper:\n  ui_enabled: true\n"):
        yield server.url


@pytest.fixture
def bypass_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A loopback clauster that opens the bypassPermissions ceiling for ``gamma``.

    Sets ``projects.gamma.allow_bypass_permissions: true`` so the footgun permission
    option renders for ``gamma`` only. Function-scoped (like ``bridge_server``): the
    typed-confirm flow trusts ``gamma``, writing the isolated trust store, so a fresh
    server + projects tree per test keeps that mutation from leaking into the next.
    """
    tmp = tmp_path_factory.mktemp("e2e-bypass")
    extra = "projects:\n  gamma:\n    allow_bypass_permissions: true\n"
    yield from _start_server(tmp, mutable_projects_tree, extra)


@pytest.fixture
def bridge_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A loopback clauster for driving the real bridge lifecycle (trust → start → stop).

    Function-scoped (unlike the module-scoped read-only servers above) so each
    bridge test gets a clean trust store, instance registry, state_dir, AND projects
    tree — a spawned/trusted bridge or an on-disk write (saved CLAUDE.md, created
    project) in one test never leaks into the next. Yields the full :class:`Server`
    so tests can read the launch argv from ``state_dir``.
    """
    tmp = tmp_path_factory.mktemp("e2e-bridge")
    yield from _start_server(tmp, mutable_projects_tree)


@pytest.fixture
def config_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A loopback clauster seeded with a known Tier-A value for the config-editor E2E.

    Function-scoped so the on-disk config write (and its backup) from one test never
    leaks into the next. The config file is ``state_dir.parent / 'clauster.yml'``.
    """
    tmp = tmp_path_factory.mktemp("e2e-config")
    yield from _start_server(tmp, mutable_projects_tree, extra="usage:\n  fx_rate: 1.0\n")


@pytest.fixture
def enum_config_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A clauster seeded with enum fields set to their NON-first option.

    ``claude.launch_mode: pty`` (first choice is ``standard``) and ``usage.mode:
    "off"`` (first choice is ``cost``) so the config-editor dropdowns must reflect
    the saved value rather than falling back to option index 0 — the regression
    guard for the x-model/x-for ``<select>`` ordering bug.
    """
    tmp = tmp_path_factory.mktemp("e2e-config-enum")
    yield from _start_server(
        tmp, mutable_projects_tree, extra='  launch_mode: pty\nusage:\n  mode: "off"\n'
    )


@pytest.fixture
def trust_fail_bridge_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A bridge_server whose trust-on-start write fails, surfacing an inline error.

    The trust-on-start flow (the first step of a bridge spawn) writes the isolated
    ``HOME/.claude.json``; here that path is pre-created as a *directory*, so the
    writer's atomic ``os.replace`` raises ``OSError`` and the trust POST returns 500.
    The dashboard then renders the failure in the persistent inline ``errorOf``
    ``.alert-danger`` block on the card — not just a transient toast. A deterministic,
    real action failure (vs a fake-``claude`` crash, which the runner surfaces as an
    error-*status* instance rather than the ``errorOf`` block).

    Function-scoped like :func:`bridge_server` so the obstruction / failed-action
    state never leaks across tests.
    """
    tmp = tmp_path_factory.mktemp("e2e-bridge-trustfail")
    # _start_server creates HOME at ``tmp/home`` with ``mkdir(exist_ok=True)``; pre-create
    # it and obstruct the trust file before the server starts.
    (tmp / "home").mkdir()
    (tmp / "home" / ".claude.json").mkdir()
    yield from _start_server(tmp, mutable_projects_tree)


@pytest.fixture
def log_extra_bridge_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A bridge_server whose fake ``claude`` emits an ANSI + session-token log line.

    Sets ``FAKE_CLAUDE_LOG_EXTRA=1`` so the fake writes one extra ``--debug-file``
    line carrying ANSI color codes and a ``claude.ai/code/session_…`` deep link plus a
    bearer token — the live-tail redaction test asserts the streamed view strips the
    ANSI and masks the session id / token (clauster's :func:`clauster.redact.sanitize_line`).
    """
    tmp = tmp_path_factory.mktemp("e2e-bridge-logx")
    yield from _start_server(tmp, mutable_projects_tree, extra_env={"FAKE_CLAUDE_LOG_EXTRA": "1"})


@pytest.fixture
def multi_session_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A bridge_server whose fake ``claude`` delays each pty connect URL (``pty_slow``).

    The multi-session E2E (#779) launches several interactive (pty) sessions per
    project. ``FAKE_CLAUDE_MODE=pty_slow`` holds every pty spawn in its ready-wait for
    ``FAKE_CLAUDE_SLOW`` seconds (the real ``claude`` takes seconds too), so the
    pending-launch stub (``data-test="pty-pending-row"``) is reliably observable —
    with the default instant-URL fake the spawn POST returns sub-second and the stub
    would be a coin-flip to catch. ``run_bridge`` (the standard subcommand form)
    ignores this mode, so the project's standard bridge spawns normally.
    """
    tmp = tmp_path_factory.mktemp("e2e-multi-session")
    yield from _start_server(
        tmp,
        mutable_projects_tree,
        extra_env={"FAKE_CLAUDE_MODE": "pty_slow", "FAKE_CLAUDE_SLOW": "1.2"},
    )


@pytest.fixture
def bridge_server_pty(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A loopback clauster defaulting to pty (true-resume) mode for the bridge lifecycle.

    Like :func:`bridge_server` but with ``claude.launch_mode: pty`` so a started
    bridge comes up under a real :mod:`clauster.pty_keeper` running the ``claude
    --remote-control`` flag form — the true-resume path (Resume re-spawns it with
    ``--continue``). pty mode is POSIX-only; the E2E host is Linux.
    """
    tmp = tmp_path_factory.mktemp("e2e-bridge-pty")
    yield from _start_server(tmp, mutable_projects_tree, extra="  launch_mode: pty\n")


@pytest.fixture
def usage_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A clauster whose ``alpha`` project has a seeded usage transcript (cost badge).

    The per-project cost/token badge lazy-loads from ``/api/projects/<name>/usage``,
    which reads Claude's per-session transcripts under
    ``$HOME/.claude/projects/<sanitized-cwd>/``. ``_start_server`` isolates HOME to
    ``tmp/home``, so pre-seed one priced transcript for ``alpha`` there (its badge
    renders) and leave ``beta`` blank (no badge). ``usage.mode`` defaults to ``cost``,
    so no extra config is needed. Function-scoped: the seed is per-test.
    """
    from clauster.pointers import sanitize_cwd  # pure cwd→dirname mapping

    tmp = tmp_path_factory.mktemp("e2e-usage")
    alpha = (mutable_projects_tree / "alpha").resolve()
    transcript_dir = tmp / "home" / ".claude" / "projects" / sanitize_cwd(alpha)
    transcript_dir.mkdir(parents=True)
    # 200k input + 100k output on sonnet → ≈$2.10 (0.2·$3 + 0.1·$15), a clearly non-zero badge.
    (transcript_dir / "seed.jsonl").write_text(
        json.dumps(
            {
                "message": {
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 200000, "output_tokens": 100000},
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    yield from _start_server(tmp, mutable_projects_tree)


@pytest.fixture
def external_session_server(
    tmp_path_factory: pytest.TempPathFactory, mutable_projects_tree: Path
) -> Iterator[Server]:
    """A clauster that sees one EXTERNAL working session in ``alpha``.

    The fake ``claude agents --json`` returns (via ``FAKE_CLAUDE_AGENTS``) a session
    whose cwd is ``alpha`` but which Clauster never started, so
    :func:`inspector.reconcile` attributes it ``EXTERNAL`` → the dashboard shows the
    "External session active" indicator. A 1s ``agents_json_poll_interval_seconds``
    (the first ``poll_once`` runs at loop start) makes it appear within a poll.
    """
    tmp = tmp_path_factory.mktemp("e2e-external")
    alpha = (mutable_projects_tree / "alpha").resolve()
    agents = json.dumps(
        [
            {
                "pid": 999999,  # not a real clauster bridge → reconcile() marks it EXTERNAL
                "cwd": str(alpha),
                "kind": "interactive",  # a bridge-eligible kind (not background)
                "state": "running",  # non-terminal
                "startedAt": 1735689600000,  # epoch ms (the model rejects an ISO string)
                "sessionId": "e2e-external-0000",
            }
        ]
    )
    # Poll agents --json every 1s (default 300) so the cross-check lands promptly.
    yield from _start_server(
        tmp,
        mutable_projects_tree,
        extra="  agents_json_poll_interval_seconds: 1\n",
        extra_env={"FAKE_CLAUDE_AGENTS": agents},
    )
