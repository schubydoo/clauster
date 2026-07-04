"""Dashboard-driven `claude` account login shepherd (#839).

Two layers:

* :mod:`clauster.login_shepherd` unit tests — a real subprocess is spawned, but it
  is always the *fake* `claude` binary (``tests/fixtures/fake_claude/claude``'s
  `auth login` / `setup-token` branches, scripted via env vars) — never the real
  CLI, never the real account. POSIX-gated: the fake stub is an extensionless
  shebang script, not a Win32 executable.
* The gated ``/api/login-shepherd/*`` routes — config-gate 404, auth-required,
  and request-shape validation, exercised through the same fake binary.

Every test runs under the autouse HOME-isolation fixture (see conftest.py), so
even a hypothetical bug that reached the real ``claude auth login`` would still
land in a throwaway temp HOME, never the developer's real ``~/.claude``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import login_shepherd as ls
from clauster.app import create_app
from clauster.config import ClausterConfig

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"

# The fake `claude` stub is a POSIX shebang script; on Windows it isn't a valid Win32
# executable, so every test here (all of them spawn it) is POSIX-gated — same idiom as
# tests/test_config_write_mcp_cli.py's `_POSIX_ONLY`.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="fake_claude stub is a POSIX script, not a Win32 executable"
)
pytestmark = _POSIX_ONLY


@pytest.fixture
def shepherd() -> ls.LoginShepherd:
    return ls.LoginShepherd(str(FAKE_CLAUDE))


# --- start(): URL parsed, argv shape, timeouts --------------------------------------


def test_start_login_parses_authorize_url(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    try:
        result = shepherd.start("login")
        assert result["authorize_url"] == "https://claude.ai/oauth/authorize?fake=1"
        assert "Open this URL to authorize" in result["output"]
    finally:
        shepherd.cancel()


def test_start_setup_token_parses_authorize_url(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    try:
        result = shepherd.start("setup-token")
        assert result["authorize_url"].startswith("https://")
    finally:
        shepherd.cancel()


def test_start_custom_url_is_parsed(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_URL", "https://claude.ai/code/session_ABC123")
    try:
        result = shepherd.start("login")
        assert result["authorize_url"] == "https://claude.ai/code/session_ABC123"
    finally:
        shepherd.cancel()


def test_start_no_url_fails_closed(shepherd, monkeypatch) -> None:
    # #839 fail-closed contract: if no authorize URL ever appears, start() must raise
    # rather than hang — and the subprocess must be reaped, not leaked.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "no_url")
    monkeypatch.setattr(ls, "START_TIMEOUT_SECONDS", 1.0)
    with pytest.raises(ls.LoginShepherdError, match="did not produce a login URL"):
        shepherd.start("login")
    assert not shepherd.is_active()  # never leaks a subprocess on the fail-closed path


def test_start_crash_before_url_fails_closed(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "crash_before_url")
    monkeypatch.setattr(ls, "START_TIMEOUT_SECONDS", 5.0)
    with pytest.raises(ls.LoginShepherdError, match="exited before printing"):
        shepherd.start("login")
    assert not shepherd.is_active()


def test_start_bounded_wait_survives_a_slow_url(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "slow_url")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_SLOW_SECONDS", "0.2")
    try:
        result = shepherd.start("login")
        assert result["authorize_url"]
    finally:
        shepherd.cancel()


def test_unknown_binary_raises_before_spawning(monkeypatch) -> None:
    bad = ls.LoginShepherd("definitely-not-a-real-claude-binary-xyz")
    with pytest.raises(Exception):  # noqa: B017 - claude_cli.ClaudeNotFound, a RuntimeError
        bad.start("login")
    assert not bad.is_active()


def test_popen_failure_is_wrapped(shepherd, monkeypatch) -> None:
    # The binary resolves fine (claude_cli.resolve_binary succeeds), but the actual
    # Popen() call itself fails (e.g. permission denied, ENOEXEC) — must be wrapped
    # in LoginShepherdError, not an unhandled OSError, and must never leave a flow set.
    def _boom(*_args, **_kwargs):
        raise OSError("no such device")

    monkeypatch.setattr(ls.subprocess, "Popen", _boom)
    with pytest.raises(ls.LoginShepherdError, match="failed to start claude login"):
        shepherd.start("login")
    assert not shepherd.is_active()


# --- single-flight ------------------------------------------------------------------


def test_second_start_while_active_is_rejected(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    try:
        shepherd.start("login")
        assert shepherd.is_active()
        with pytest.raises(ls.AlreadyActiveError):
            shepherd.start("login")
    finally:
        shepherd.cancel()


def test_cancel_then_start_again_succeeds(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    shepherd.start("login")
    shepherd.cancel()
    assert not shepherd.is_active()
    try:
        result = shepherd.start("setup-token")
        assert result["authorize_url"]
    finally:
        shepherd.cancel()


# --- submit_code(): success, rejection, not-active ----------------------------------


def test_submit_code_success_login(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    shepherd.start("login")
    result = shepherd.submit_code("the-pasted-code")
    assert result["ok"] is True
    assert "succeeded" in result["message"].lower() or "successful" in result["message"].lower()
    assert "token" not in result
    assert not shepherd.is_active()  # torn down after a terminal outcome


def test_submit_code_success_setup_token_returns_token_once(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_TOKEN", "canned-token-value-xyz")
    shepherd.start("setup-token")
    result = shepherd.submit_code("the-pasted-code")
    assert result["ok"] is True
    assert result["token"] == "canned-token-value-xyz"


def test_submit_code_rejected_by_cli(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "reject_code")
    shepherd.start("login")
    result = shepherd.submit_code("a-bad-code")
    assert result["ok"] is False
    assert "invalid code" in result["message"]
    assert not shepherd.is_active()


def test_submit_code_without_active_flow_raises(shepherd) -> None:
    with pytest.raises(ls.NotActiveError):
        shepherd.submit_code("anything")


def test_submit_code_survives_a_closed_stdin(shepherd, monkeypatch, caplog) -> None:
    # If the subprocess already exited (closed its stdin pipe) by the time the operator
    # submits a code, writing must not raise past submit_code — it's logged and the
    # method still proceeds to read whatever final output/exit-code is available.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "reject_code")
    shepherd.start("login")
    shepherd._flow.proc.stdin.close()  # noqa: SLF001 - simulate the pipe closing early
    with caplog.at_level(logging.WARNING, logger="clauster.login_shepherd"):
        result = shepherd.submit_code("a-code")
    assert result["ok"] is False
    assert any("writing code" in r.getMessage() for r in caplog.records)


# --- cancel(): reaps cleanly, always safe -------------------------------------------


def test_cancel_is_idempotent_and_safe_when_nothing_active(shepherd) -> None:
    shepherd.cancel()  # no-op, must not raise
    shepherd.cancel()


def test_cancel_reaps_a_running_subprocess(shepherd, monkeypatch) -> None:
    # `ready` mode blocks waiting for a pasted code after printing the URL — cancel()
    # must terminate + reap it (not leave it running) rather than waiting for a code
    # that will never come, and a second cancel() on top must still be a safe no-op.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    shepherd.start("login")
    proc = shepherd._flow.proc  # noqa: SLF001 - test-only introspection to confirm the reap
    assert proc.poll() is None  # still alive, waiting for a pasted code
    shepherd.cancel()
    assert proc.poll() is not None  # terminated + reaped
    assert not shepherd.is_active()
    shepherd.cancel()  # still safe to call again


# --- redaction: the pasted code / token never appear in logs ------------------------


def test_code_and_token_never_logged(shepherd, monkeypatch, caplog) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_TOKEN", "super-secret-token-value")
    secret_code = "super-secret-pasted-oauth-code"
    with caplog.at_level(logging.DEBUG):
        shepherd.start("setup-token")
        result = shepherd.submit_code(secret_code)
    assert result["ok"] is True
    assert result["token"] == "super-secret-token-value"
    for record in caplog.records:
        message = record.getMessage()
        assert secret_code not in message
        assert "super-secret-token-value" not in message


def test_redact_masks_token_in_output() -> None:
    text = "some line\nCLAUDE_CODE_OAUTH_TOKEN=abc123\nmore text"
    redacted = ls._redact(text)
    assert "abc123" not in redacted
    assert "CLAUDE_CODE_OAUTH_TOKEN=<redacted>" in redacted


def test_start_failure_message_never_contains_pasted_code(shepherd, monkeypatch) -> None:
    # The no-URL failure path surfaces captured output in the exception message —
    # confirm it can't ever contain a pasted code (start() never sees one; this
    # guards the invariant even if a future refactor moved code-handling earlier).
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "no_url")
    monkeypatch.setattr(ls, "START_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(ls.LoginShepherdError) as ei:
        shepherd.start("login")
    assert "super-secret" not in str(ei.value)


# --- internals: teardown edge cases + the reader thread -----------------------------


def test_teardown_kills_a_subprocess_that_ignores_terminate(shepherd, monkeypatch) -> None:
    # A subprocess that doesn't react to terminate() within the wait timeout must be
    # force-killed rather than leaked — exercise the TimeoutExpired -> kill() fallback.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    shepherd.start("login")
    flow = shepherd._flow  # noqa: SLF001 - internals test

    real_wait = flow.proc.wait
    calls = {"n": 0}

    def _flaky_wait(timeout: float = 0.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ls.subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
        return real_wait(timeout=timeout)

    monkeypatch.setattr(flow.proc, "wait", _flaky_wait)
    shepherd.cancel()
    assert calls["n"] >= 2  # first (timed-out) wait, then the post-kill wait
    assert flow.proc.poll() is not None


def test_teardown_survives_stdin_close_error(shepherd, monkeypatch) -> None:
    # A `.close()` raising OSError/ValueError (e.g. the pipe already broke) must not
    # propagate out of teardown — the process still gets terminated + reaped.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    shepherd.start("login")
    flow = shepherd._flow  # noqa: SLF001 - internals test

    def _boom():
        raise OSError("already closed")

    monkeypatch.setattr(flow.proc.stdin, "close", _boom)
    shepherd.cancel()  # must not raise
    assert flow.proc.poll() is not None
    assert not shepherd.is_active()


def test_teardown_does_not_clear_a_different_active_flow(shepherd, monkeypatch) -> None:
    # `_teardown(flow, already_cleared=False)` only clears `self._flow` when it is
    # STILL the same flow object — if a newer flow has since replaced it, tearing
    # down a stale handle must not clobber the live one (the `self._flow is flow`
    # guard's false branch).
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    shepherd.start("login")
    stale_flow = shepherd._flow  # noqa: SLF001 - internals test
    shepherd.cancel()
    shepherd.start("login")
    live_flow = shepherd._flow  # noqa: SLF001 - internals test
    assert live_flow is not stale_flow

    shepherd._teardown(stale_flow)  # noqa: SLF001 - internals test; already reaped, must no-op
    assert shepherd._flow is live_flow  # noqa: SLF001 - the live flow survives untouched
    shepherd.cancel()


def test_pump_stdout_noop_when_stdout_missing() -> None:
    class _FakeProc:
        stdout = None

    flow = ls._Flow(mode="login", proc=_FakeProc())  # type: ignore[arg-type]
    ls._pump_stdout(flow)  # must return immediately, no error
    assert flow.snapshot() == ""


def test_pump_stdout_survives_a_read_error() -> None:
    class _BrokenStdout:
        def __iter__(self):
            raise OSError("broken pipe")

    class _FakeProc:
        stdout = _BrokenStdout()

    flow = ls._Flow(mode="login", proc=_FakeProc())  # type: ignore[arg-type]
    ls._pump_stdout(flow)  # must swallow the OSError, not propagate
    assert flow.snapshot() == ""


# --- routes: config gate, auth, request validation ----------------------------------


def _cfg(*, login_shepherd_enabled: bool, auth_enabled: bool, tmp_path: Path) -> ClausterConfig:
    return ClausterConfig.model_validate(
        {
            "projects_root": str(tmp_path / "projects"),
            "state_dir": str(tmp_path / "state"),
            "claude": {"binary": str(FAKE_CLAUDE)},
            "login_shepherd": {"enabled": login_shepherd_enabled},
            "auth": {"enabled": auth_enabled},
        }
    )


def _client(tmp_path: Path, *, enabled: bool, auth_enabled: bool = False) -> TestClient:
    (tmp_path / "projects").mkdir(exist_ok=True)
    cfg = _cfg(login_shepherd_enabled=enabled, auth_enabled=auth_enabled, tmp_path=tmp_path)
    return TestClient(create_app(cfg))


def test_flag_defaults_false(tmp_path: Path) -> None:
    cfg = ClausterConfig.model_validate({"projects_root": str(tmp_path)})
    assert cfg.login_shepherd.enabled is False


def test_routes_404_when_disabled(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=False) as c:
        assert c.post("/api/login-shepherd/start", json={"mode": "login"}).status_code == 404
        assert c.post("/api/login-shepherd/code", json={"code": "x"}).status_code == 404
        assert c.post("/api/login-shepherd/cancel").status_code == 404


def test_start_route_bad_mode_is_422_when_enabled(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=True) as c:
        resp = c.post("/api/login-shepherd/start", json={"mode": "bogus"})
        assert resp.status_code == 422


def test_disabled_surface_404s_even_for_a_bogus_mode(tmp_path: Path) -> None:
    # Invisible-surface invariant (mirrors #819's config-write fix): the capability
    # gate must run BEFORE any body validation, so a disabled surface 404s even for
    # a malformed request rather than leaking a differing 422.
    with _client(tmp_path, enabled=False) as c:
        assert c.post("/api/login-shepherd/start", json={"mode": "bogus"}).status_code == 404


def test_code_route_missing_body_is_422(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=True) as c:
        assert c.post("/api/login-shepherd/code", json={"code": ""}).status_code == 422
        assert c.post("/api/login-shepherd/code", json={}).status_code == 422


def test_full_flow_via_routes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    with _client(tmp_path, enabled=True) as c:
        start = c.post("/api/login-shepherd/start", json={"mode": "login"})
        assert start.status_code == 200
        assert start.json()["authorize_url"].startswith("https://")

        code = c.post("/api/login-shepherd/code", json={"code": "pasted-code"})
        assert code.status_code == 200
        body = code.json()
        assert body["ok"] is True
        assert "token" not in body


def test_second_start_via_routes_is_409(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    with _client(tmp_path, enabled=True) as c:
        first = c.post("/api/login-shepherd/start", json={"mode": "login"})
        assert first.status_code == 200
        second = c.post("/api/login-shepherd/start", json={"mode": "login"})
        assert second.status_code == 409
        # Clean up so the app's shutdown doesn't leave the subprocess running.
        cancel = c.post("/api/login-shepherd/cancel")
        assert cancel.status_code == 200


def test_code_route_without_active_flow_is_409(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=True) as c:
        resp = c.post("/api/login-shepherd/code", json={"code": "x"})
        assert resp.status_code == 409


def test_cancel_route_always_ok(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=True) as c:
        assert c.post("/api/login-shepherd/cancel").status_code == 200


def test_start_route_start_failure_is_400(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "no_url")
    monkeypatch.setattr(ls, "START_TIMEOUT_SECONDS", 0.5)
    with _client(tmp_path, enabled=True) as c:
        resp = c.post("/api/login-shepherd/start", json={"mode": "login"})
        assert resp.status_code == 400


def test_routes_require_auth_when_auth_enabled(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=True, auth_enabled=True) as c:
        resp = c.post("/api/login-shepherd/start", json={"mode": "login"})
        # An unauthenticated POST with no session can be denied either by the guard
        # middleware's 401 ("authentication required") or, first in the chain for an
        # unsafe method, the CSRF Origin check's 403 — both are a deny, never a 200
        # (same tolerant assertion `test_app_routes.py` uses for this interaction).
        assert resp.status_code in {401, 403}


def test_dashboard_context_reflects_flag(tmp_path: Path) -> None:
    # The `function loginShepherd()` Alpine component is always shipped in the shared
    # script bundle (like `reaper()`); what the flag actually gates is the panel's
    # `x-data="loginShepherd()"` USAGE in the dashboard body — assert on that.
    with _client(tmp_path, enabled=True) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        assert 'x-data="loginShepherd()"' in resp.text
    with _client(tmp_path, enabled=False) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        assert 'x-data="loginShepherd()"' not in resp.text
