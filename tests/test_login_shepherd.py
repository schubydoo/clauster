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
import os
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
    with pytest.raises(ls.LoginShepherdError, match="did not produce a usable login"):
        shepherd.start("login")
    assert not shepherd.is_active()  # never leaks a subprocess on the fail-closed path


def test_start_crash_before_url_fails_closed(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "crash_before_url")
    monkeypatch.setattr(ls, "START_TIMEOUT_SECONDS", 5.0)
    with pytest.raises(ls.LoginShepherdError, match="exited before printing"):
        shepherd.start("login")
    assert not shepherd.is_active()


def test_start_url_then_immediate_exit_fails_closed(shepherd, monkeypatch) -> None:
    # Defense in depth: the process prints a VALID authorize URL and then exits. Even
    # though a URL was found, start() must FAIL CLOSED — a URL for a dead subprocess would
    # strand the operator (they'd authorize, then submit_code would find no live stdin).
    # It must NEVER return an authorize_url in this case.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "url_then_exit")
    monkeypatch.setattr(ls, "START_TIMEOUT_SECONDS", 5.0)
    with pytest.raises(ls.LoginShepherdError, match="exited before the login could proceed"):
        shepherd.start("login")
    # The contract: start() RAISES (returns no {authorize_url: ...} dict) and reaps the
    # dead process — so there is never a usable-looking URL for a subprocess with no stdin.
    assert not shepherd.is_active()


def test_start_url_found_then_process_dies_fails_closed(shepherd, monkeypatch) -> None:
    # The DETERMINISTIC counterpart to the immediate-exit test above. There, the process
    # dies so fast that `start()`'s wait usually already sees the exit. Here the process
    # prints a valid URL and stays alive briefly, so the wait latches the URL while the
    # process still LOOKS alive (exited=False) — then it dies. This is the exact race the
    # flaky immediate-exit variant only hit by chance under load: without the liveness
    # settle, `start()` would hand back a URL for a subprocess with no live stdin. A
    # generous grace + a short child delay make the exit land INSIDE the settle window
    # every time (no scheduling dependence), so `start()` must fail closed deterministically.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "url_then_delayed_exit")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_DELAYED_EXIT_SECONDS", "0.3")
    monkeypatch.setattr(ls, "START_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(ls, "_URL_LIVENESS_GRACE_SECONDS", 5.0)
    with pytest.raises(ls.LoginShepherdError, match="exited before the login could proceed"):
        shepherd.start("login")
    assert not shepherd.is_active()  # dead process reaped — no leaked subprocess or URL


def test_start_bounded_wait_survives_a_slow_url(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "slow_url")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_SLOW_SECONDS", "0.2")
    try:
        result = shepherd.start("login")
        assert result["authorize_url"]
    finally:
        shepherd.cancel()


def test_start_prefers_the_known_host_url_over_a_decoy(shepherd, monkeypatch) -> None:
    # A non-Claude https link printed FIRST must not hijack the operator — the extractor
    # prefers the known-host authorize URL over an arbitrary first match.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "decoy_url")
    try:
        result = shepherd.start("login")
        assert result["authorize_url"] == "https://claude.ai/oauth/authorize?fake=1"
    finally:
        shepherd.cancel()


def test_start_ignores_a_docs_subdomain_of_a_known_parent(shepherd, monkeypatch) -> None:
    # `docs.anthropic.com` (a docs subdomain of the known parent anthropic.com) printed
    # first must NOT be selected — the real claude.ai authorize URL is returned instead.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "docs_decoy_url")
    try:
        result = shepherd.start("login")
        assert result["authorize_url"] == "https://claude.ai/oauth/authorize?fake=1"
    finally:
        shepherd.cancel()


def test_start_trims_trailing_punctuation_off_the_url(shepherd, monkeypatch) -> None:
    # The CLI printed "(<url>)." — the returned href must not carry the trailing `).`.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "punct_url")
    try:
        result = shepherd.start("login")
        assert result["authorize_url"] == "https://claude.ai/oauth/authorize?fake=1"
    finally:
        shepherd.cancel()


def test_start_strips_ansi_escapes_around_the_url(shepherd, monkeypatch) -> None:
    # An ANSI color/reset-wrapped URL must come back clean (no escape bytes in the href).
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ansi_url")
    try:
        result = shepherd.start("login")
        assert result["authorize_url"] == "https://claude.ai/oauth/authorize?fake=1"
        assert "\x1b" not in result["authorize_url"]
    finally:
        shepherd.cancel()


# --- _extract_authorize_url() pure-unit coverage ------------------------------------


def test_extract_url_none_when_absent() -> None:
    assert ls._extract_authorize_url("just some text, no link here") is None


def test_extract_url_prefers_known_host() -> None:
    out = "first https://evil.example.com/x then https://console.anthropic.com/authorize?z"
    assert ls._extract_authorize_url(out) == "https://console.anthropic.com/authorize?z"


def test_extract_url_falls_back_to_last_when_no_known_host() -> None:
    out = "https://a.example.com/1 and https://b.example.com/2"
    assert ls._extract_authorize_url(out) == "https://b.example.com/2"


def test_extract_url_trims_and_strips_ansi() -> None:
    out = "go to \x1b[36mhttps://claude.ai/authorize?fake=1\x1b[0m."
    assert ls._extract_authorize_url(out) == "https://claude.ai/authorize?fake=1"


def test_extract_url_docs_subdomain_is_not_an_auth_host() -> None:
    # `docs.anthropic.com` is a subdomain of a known parent (anthropic.com) but is a docs
    # page, NOT an auth endpoint — a docs link printed BEFORE the real authorize URL must
    # not be selected (this is the "known host decoy" the docs-subdomain exclusion closes).
    out = (
        "Read the docs at https://docs.anthropic.com/claude-code\n"
        "Then authorize at https://claude.ai/oauth/authorize?real=1"
    )
    assert ls._extract_authorize_url(out) == "https://claude.ai/oauth/authorize?real=1"


def test_extract_url_recognizes_claude_com_authorize_host() -> None:
    # Live-verified (claude 2.1.200, 2026-07-03): real `claude auth login` prints its
    # authorize URL on `claude.com` (redirect_uri `platform.claude.com`). A docs decoy on a
    # known parent printed BEFORE it must not win — the real `claude.com` URL is returned.
    real = (
        "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&"
        "redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    )
    out = f"Read the guide at https://docs.anthropic.com/claude-code first.\nOpen this URL: {real}"
    assert ls._extract_authorize_url(out) == real


def test_extract_url_help_and_www_subdomains_excluded() -> None:
    # help./www. subdomains of a known parent are marketing/docs, not auth — excluded.
    out = (
        "https://www.anthropic.com/pricing and https://help.claude.ai/faq\n"
        "https://console.anthropic.com/auth?code=1"
    )
    assert ls._extract_authorize_url(out) == "https://console.anthropic.com/auth?code=1"


def test_extract_url_prefers_the_last_known_auth_host() -> None:
    # Two genuine auth-host URLs → the LAST one wins (the actionable link is printed after
    # any preamble), not the first.
    out = (
        "https://claude.ai/oauth/authorize?first=1\n"
        "Actually, use https://claude.ai/oauth/authorize?second=2"
    )
    assert ls._extract_authorize_url(out) == "https://claude.ai/oauth/authorize?second=2"


def test_extract_url_only_docs_hosts_falls_back_to_last_https() -> None:
    # When every known-parent host is a docs/marketing subdomain (none is a real auth host),
    # fall back to the LAST https URL overall rather than picking a docs page as "known".
    out = "https://docs.anthropic.com/a and https://help.claude.ai/b"
    assert ls._extract_authorize_url(out) == "https://help.claude.ai/b"


def test_extract_url_prefers_authorize_path_over_same_host_decoy_after_it() -> None:
    # Greptile P1: the real authorize URL is printed FIRST, then the CLI later prints a
    # same-host non-authorize page (settings/account) — the OLD "last known-host wins" rule
    # picked the later, wrong URL since both share a known auth host. The authorize-path
    # match must win regardless of print order.
    real = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc"
    out = f"Open this URL: {real}\nLater, see your account at https://claude.com/settings"
    assert ls._extract_authorize_url(out) == real


def test_extract_url_prefers_authorize_path_over_platform_subdomain_decoy() -> None:
    # Same bug, with the decoy on a different known-auth-host subdomain
    # (platform.claude.com) rather than the bare parent host.
    real = "https://claude.com/cai/oauth/authorize?code=true&client_id=abc"
    out = f"{real}\nCheck https://platform.claude.com/dashboard for usage."
    assert ls._extract_authorize_url(out) == real


def test_extract_url_authorize_in_query_only_is_not_a_path_match() -> None:
    # "authorize" appearing only inside a query string (e.g. a redirect_uri value) must NOT
    # be treated as an authorize-endpoint match — only the URL's PATH counts. Proves the
    # fix matches the path component, not the whole URL string.
    decoy = "https://claude.com/settings?redirect_uri=https%3A%2F%2Fx.com%2Fauthorize"
    out = f"{decoy}\nThen see https://claude.com/account"
    # Neither candidate has an authorize PATH, so this falls back to known-auth-host
    # selection (both are known hosts) → the LAST one, exactly the pre-existing rule.
    assert ls._extract_authorize_url(out) == "https://claude.com/account"


def test_extract_url_authorize_path_match_on_unknown_host_still_wins() -> None:
    # An authorize-PATH match on an UNKNOWN host still beats a later known-host non-authorize
    # URL: path-match is checked first regardless of host, only preferring a known host
    # *among* the authorize-path matches (there are none here, so it falls through to the
    # bare authorize_matches list rather than known_authorize).
    out = (
        "https://weird-unknown-host.example/cai/oauth/authorize?code=1\n"
        "Later: https://claude.com/settings"
    )
    assert (
        ls._extract_authorize_url(out)
        == "https://weird-unknown-host.example/cai/oauth/authorize?code=1"
    )


def test_unknown_binary_raises_login_shepherd_error(monkeypatch) -> None:
    # An unresolvable binary must surface as a LoginShepherdError (→ 400 at the route),
    # NOT the raw claude_cli.ClaudeNotFound that would escape the route's except and 500.
    bad = ls.LoginShepherd("definitely-not-a-real-claude-binary-xyz")
    with pytest.raises(ls.LoginShepherdError, match="claude binary not found"):
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


# --- setup-token PTY transport (#846): fail-closed + spawn-failure paths -----------


def test_setup_token_fails_closed_without_pyte(shepherd, monkeypatch) -> None:
    # The long-lived-token mode needs the optional `pty` extra (pyte). Without it,
    # `PtyScreen()` raises `PyteUnavailableError` — `_spawn_pty` must turn that into a
    # clear, actionable `LoginShepherdError` BEFORE opening a pty or spawning anything,
    # never a crash, and must leave no flow behind (`login` mode stays unaffected).
    def _boom(*_args, **_kwargs):
        raise ls.PyteUnavailableError("pyte is not installed; run: pip install 'clauster[pty]'")

    monkeypatch.setattr(ls, "PtyScreen", _boom)
    with pytest.raises(ls.LoginShepherdError, match="pty` extra"):
        shepherd.start("setup-token")
    assert not shepherd.is_active()


def test_setup_token_openpty_failure_is_wrapped(shepherd, monkeypatch) -> None:
    # A pty-open failure (e.g. the process fd table is exhausted) must be wrapped in
    # LoginShepherdError, not an unhandled OSError, and leave no flow behind.
    def _boom():
        raise OSError("out of ptys")

    monkeypatch.setattr(ls.os, "openpty", _boom)
    with pytest.raises(ls.LoginShepherdError, match="failed to open a pty"):
        shepherd.start("setup-token")
    assert not shepherd.is_active()


def test_setup_token_popen_failure_closes_both_fds(shepherd, monkeypatch) -> None:
    # A Popen() failure on the PTY path must close BOTH the master and slave fds it
    # already opened (never leak them) and surface as LoginShepherdError, not a bare
    # OSError — mirrors test_popen_failure_is_wrapped for the plain-pipe transport.
    closed: list[int] = []
    real_close = ls.os.close

    def _tracking_close(fd):
        closed.append(fd)
        real_close(fd)

    def _boom(*_args, **_kwargs):
        raise OSError("no such device")

    monkeypatch.setattr(ls.os, "close", _tracking_close)
    monkeypatch.setattr(ls.subprocess, "Popen", _boom)
    with pytest.raises(ls.LoginShepherdError, match="failed to start claude setup-token"):
        shepherd.start("setup-token")
    assert not shepherd.is_active()
    assert len(closed) == 2  # master + slave, both reclaimed


def test_setup_token_pty_is_sized_wide_not_the_80_col_default(shepherd, monkeypatch) -> None:
    # Live-smoke-test regression (#846 follow-up): a bare os.openpty() defaults to 80
    # columns, which wraps (and thus truncates) claude setup-token's ~450-char authorize
    # URL. The slave's real winsize (TIOCGWINSZ) must reflect the widened _LOGIN_PTY_COLS,
    # not the pty default — and the shepherd's PtyScreen must be built at the SAME width,
    # or pyte would just re-wrap the already-wide line down to its own default geometry.
    import fcntl
    import struct
    import termios

    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    try:
        shepherd.start("setup-token")
        flow = shepherd._flow  # noqa: SLF001 - internals test
        assert flow.screen is not None
        assert flow.screen.cols == ls._LOGIN_PTY_COLS
        assert flow.screen.rows == ls.SCREEN_ROWS

        # Read back the slave's winsize via its master (TIOCGWINSZ mirrors TIOCSWINSZ).
        packed = fcntl.ioctl(flow.master_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert cols == ls._LOGIN_PTY_COLS
        assert rows == ls.SCREEN_ROWS
        assert cols != 80  # the os.openpty() default this regression guards against
    finally:
        shepherd.cancel()


def test_setup_token_pty_winsize_ioctl_failure_is_best_effort(shepherd, monkeypatch) -> None:
    # A TIOCSWINSZ failure must NOT abort the spawn (unlike the ECHO-disable failure below,
    # which is security-critical and fails closed) — it only risks the rare wrap/truncation
    # case that find_authorize_url's hard-wrap reassembly additionally defends against.
    import fcntl as real_fcntl
    import termios

    real_ioctl = real_fcntl.ioctl

    def _selective_boom(fd, request, *args, **kwargs):
        if request == termios.TIOCSWINSZ:
            raise OSError("simulated TIOCSWINSZ failure")
        return real_ioctl(fd, request, *args, **kwargs)

    monkeypatch.setattr(real_fcntl, "ioctl", _selective_boom)
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    try:
        result = shepherd.start("setup-token")
        # The flow still starts and finds the (narrow-rendered, but present) authorize URL —
        # a winsize failure degrades gracefully rather than crashing the spawn.
        assert result["authorize_url"].startswith("https://")
    finally:
        shepherd.cancel()


def test_setup_token_pty_full_flow_start_submit_token(shepherd, monkeypatch) -> None:
    # End-to-end PTY happy path: start() scrapes the authorize URL from the rendered
    # screen, submit_code() writes to the pty master (not a stdin pipe), and the
    # printed CLAUDE_CODE_OAUTH_TOKEN is scraped via PtyScreen.find_oauth_token().
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_TOKEN", "pty-flow-token-abc")
    result = shepherd.start("setup-token")
    assert result["authorize_url"] == "https://claude.ai/oauth/authorize?fake=1"
    flow = shepherd._flow  # noqa: SLF001 - internals test
    assert flow.master_fd is not None
    assert flow.screen is not None
    outcome = shepherd.submit_code("the-pasted-code")
    assert outcome["ok"] is True
    assert outcome["token"] == "pty-flow-token-abc"
    assert not shepherd.is_active()


def test_setup_token_pty_reject_code_fails_without_token(shepherd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "reject_code")
    shepherd.start("setup-token")
    outcome = shepherd.submit_code("a-bad-code")
    assert outcome["ok"] is False
    assert "token" not in outcome
    assert not shepherd.is_active()


def test_setup_token_pty_never_echoes_the_pasted_code_back(shepherd, monkeypatch) -> None:
    # Security regression guard: a pty's line discipline echoes back whatever is
    # written to it by DEFAULT (unlike `login`'s plain subprocess.PIPE, where a stdin
    # write is never mirrored into stdout) — `_spawn_pty` must disable that echo so
    # the operator-pasted code never round-trips through the master into
    # `flow.buffer`, and from there into a redacted-output failure message returned
    # over the API. Assert the secret is nowhere in the failure result — not just
    # that `_redact()` would mask a TOKEN= line, which this codepath never emits.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "reject_code")
    shepherd.start("setup-token")
    secret_code = "SUPER-SECRET-PASTED-CODE-MUST-NOT-ECHO"
    outcome = shepherd.submit_code(secret_code)
    assert outcome["ok"] is False
    assert secret_code not in outcome["message"]
    assert not shepherd.is_active()


def test_setup_token_pty_echo_disable_failure_is_wrapped(shepherd, monkeypatch) -> None:
    # A termios.tcsetattr failure while disabling local echo must fail closed (never
    # silently spawn with echo left on, which would risk leaking the pasted code) and
    # must reclaim both fds rather than leak them.
    closed: list[int] = []
    real_close = ls.os.close

    def _tracking_close(fd):
        closed.append(fd)
        real_close(fd)

    import termios

    def _boom(*_args, **_kwargs):
        raise termios.error("simulated tcsetattr failure")

    monkeypatch.setattr(ls.os, "close", _tracking_close)
    monkeypatch.setattr(termios, "tcsetattr", _boom)
    with pytest.raises(ls.LoginShepherdError, match="failed to disable pty echo"):
        shepherd.start("setup-token")
    assert not shepherd.is_active()
    assert len(closed) == 2  # master + slave, both reclaimed


def test_setup_token_pty_cancel_closes_master_fd(shepherd, monkeypatch) -> None:
    # cancel() must close the pty master (not leak it) and reap the subprocess even
    # though this transport has no proc.stdin pipe to close.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    shepherd.start("setup-token")
    flow = shepherd._flow  # noqa: SLF001 - internals test
    master_fd = flow.master_fd
    assert flow.proc.stdin is None  # PTY transport never uses a stdin pipe
    shepherd.cancel()
    assert not shepherd.is_active()
    with pytest.raises(OSError):
        os.fstat(master_fd)  # the fd was closed, not leaked


def test_setup_token_pty_write_code_after_close_is_a_noop(shepherd, monkeypatch) -> None:
    # _write_code must not raise once the pty master has already been torn down
    # (flow.stdin_closed guards a double-close / write-after-close).
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    shepherd.start("setup-token")
    flow = shepherd._flow  # noqa: SLF001 - internals test
    shepherd.cancel()
    shepherd._write_code(flow, "too-late")  # noqa: SLF001 - must not raise


def test_setup_token_pty_write_code_survives_a_broken_fd(shepherd, caplog) -> None:
    # A write to an already-closed/broken pty master must be logged, not raised, past
    # _write_code — mirrors test_submit_code_survives_a_closed_stdin for the PTY path.
    # Drives a hand-built flow on a guaranteed-invalid fd (no live reader thread): a real
    # spawn here would leave the _pump_pty reader blocked in os.read on the master, and
    # closing that master out from under it does NOT interrupt the read on macOS/BSD (only
    # on Linux) — the leaked reader + fd then piles up and wedges the whole xdist worker
    # (the macOS CI hang). Exercising _write_code directly covers the same os.write path.
    scr = ls.PtyScreen()

    class _FakeProc:
        pass

    flow = ls._Flow(mode="setup-token", proc=_FakeProc(), screen=scr, master_fd=999999)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger="clauster.login_shepherd"):
        shepherd._write_code(flow, "a-code")  # noqa: SLF001 - internals test; must not raise
    assert any("writing code" in r.getMessage() for r in caplog.records)


def test_pump_pty_survives_a_non_eio_read_error() -> None:
    # A non-EIO OSError from os.read (e.g. EBADF from a concurrently-closed fd) must
    # still be treated as end-of-stream, not propagated past the reader thread.
    scr = ls.PtyScreen()

    class _FakeProc:
        pass

    flow = ls._Flow(mode="setup-token", proc=_FakeProc(), screen=scr, master_fd=999999)  # type: ignore[arg-type]
    ls._pump_pty(flow)  # must return cleanly (bad fd -> OSError, not EIO)
    assert flow.snapshot() == ""


def test_pump_pty_treats_an_empty_read_as_eof(monkeypatch) -> None:
    # Some platforms/kernels may return b"" instead of raising EIO once the child's
    # side of the pty is gone (a plain pipe's ordinary clean-EOF shape) — the reader
    # must treat that the same as the EIO case: break cleanly, never spin or raise.
    scr = ls.PtyScreen()

    class _FakeProc:
        pass

    flow = ls._Flow(mode="setup-token", proc=_FakeProc(), screen=scr, master_fd=123)  # type: ignore[arg-type]
    monkeypatch.setattr(ls.os, "read", lambda _fd, _n: b"")
    ls._pump_pty(flow)  # must return cleanly on the very first (empty) read
    assert flow.snapshot() == ""


def test_pump_pty_survives_a_screen_feed_error(monkeypatch) -> None:
    # A pyte feed failure must not crash the reader thread — the read loop keeps
    # draining (falling back to the buffer for text) instead of dying mid-stream.
    # Drives os.read directly rather than a real pty: closing a pty slave can discard its
    # still-queued bytes before the master reads them on macOS/BSD, so the master's first
    # read returns EOF and the buffer stays empty there — flaking this assertion. A
    # scripted read (one chunk, then EOF) reproduces the feed-then-drain path everywhere.
    scr = ls.PtyScreen()

    def _boom(_data):
        raise RuntimeError("pyte choked")

    scr.feed = _boom  # type: ignore[method-assign]
    reads = [b"hello\n", b""]  # one chunk (feed() will choke on it), then clean EOF
    monkeypatch.setattr(ls.os, "read", lambda _fd, _n: reads.pop(0))

    class _FakeProc:
        pass

    flow = ls._Flow(mode="setup-token", proc=_FakeProc(), screen=scr, master_fd=123)  # type: ignore[arg-type]
    ls._pump_pty(flow)  # must not raise despite the feed() failure
    assert "hello" in flow.snapshot()


def test_pump_pty_noop_when_unpaired() -> None:
    # Defensive: a _Flow with only one of screen/master_fd set (shouldn't happen in
    # practice — they're always set together) must return immediately, not crash.
    class _FakeProc:
        pass

    flow = ls._Flow(mode="setup-token", proc=_FakeProc())  # type: ignore[arg-type]
    ls._pump_pty(flow)  # both None -> no-op
    assert flow.snapshot() == ""


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
    # A genuine terminal failure is NOT pending — the flow is done and torn down.
    assert result.get("pending") is not True
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


def test_submit_code_keys_off_fresh_poll_not_stale_wait_flag(shepherd, monkeypatch) -> None:
    # Finding 2 (timeout-boundary race): a completed OAuth (exit 0) that `_wait_for` failed
    # to observe as `exited` must STILL be reported as success + drain the token, because
    # submit_code keys off a fresh `flow.proc.poll()`, not `_wait_for`'s stale flag. Force
    # `_wait_for` to always claim NOT-exited to prove the fresh poll() is what decides.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_TOKEN", "boundary-token")
    shepherd.start("setup-token")

    # Patch _wait_for only for the submit call (start() already ran with the real one).
    def _wait_reports_not_exited(flow, condition, *, timeout):
        # Let the real process actually finish (it exits 0 after reading the code), but
        # report exited=False — the boundary race the old code trusted and got wrong.
        flow.proc.wait(timeout=5)
        return None, flow.snapshot(), False

    monkeypatch.setattr(ls, "_wait_for", _wait_reports_not_exited)
    result = shepherd.submit_code("the-code")
    assert result["ok"] is True  # fresh poll() saw exit 0, not the stale exited=False
    assert result["token"] == "boundary-token"
    assert not shepherd.is_active()


def test_submit_code_slow_verification_is_not_killed(shepherd, monkeypatch) -> None:
    # Finding 3 (slow-verification-kill): if the login is still running after the submit
    # timeout (a slow provider), submit_code must NOT tear it down / kill it — it returns a
    # "still verifying" result and leaves the flow ACTIVE so the operator can wait or cancel.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "hang_after_code")
    monkeypatch.setattr(ls, "SUBMIT_TIMEOUT_SECONDS", 0.5)
    shepherd.start("login")
    proc = shepherd._flow.proc  # noqa: SLF001 - confirm it's left alive
    result = shepherd.submit_code("the-code")
    assert result["ok"] is False
    assert result["pending"] is True  # distinguishes IN-PROGRESS from a terminal failure
    assert "still verifying" in result["message"]
    assert "token" not in result
    assert shepherd.is_active()  # flow left active, not reaped
    assert proc.poll() is None  # the still-valid login was NOT killed
    shepherd.cancel()  # explicit cleanup, as the operator would


# --- poll(): fetch the eventual result after a pending submit -----------------------


def test_poll_without_active_flow_raises(shepherd) -> None:
    with pytest.raises(ls.NotActiveError):
        shepherd.poll()


def test_poll_still_running_returns_pending(shepherd, monkeypatch) -> None:
    # A flow that's started but hasn't received/finished a code is still running → poll()
    # returns the same pending shape and leaves the flow active.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "hang_after_code")
    shepherd.start("login")
    result = shepherd.poll()
    assert result["ok"] is False
    assert result["pending"] is True
    assert "still verifying" in result["message"]
    assert shepherd.is_active()  # not reaped — still verifying
    shepherd.cancel()


def test_poll_after_completion_returns_terminal_and_reaps(shepherd, monkeypatch) -> None:
    # The core of the pending flow: a slow login that finishes AFTER submit_code returned
    # pending. poll() must observe the exit, return the TERMINAL result (with token for
    # setup-token), and REAP the flow so it is no longer active-forever.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "hang_after_code")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_TOKEN", "eventual-token")
    monkeypatch.setattr(ls, "SUBMIT_TIMEOUT_SECONDS", 0.3)
    shepherd.start("setup-token")
    pending = shepherd.submit_code("the-code")
    assert pending["pending"] is True
    assert shepherd.is_active()

    # Now let the still-running process finish (SIGTERM makes the idle stub exit), then poll.
    shepherd._flow.proc.terminate()  # noqa: SLF001 - simulate the provider finishing verification
    shepherd._flow.proc.wait(timeout=5)  # noqa: SLF001
    result = shepherd.poll()
    assert "pending" not in result  # terminal — no longer pending
    assert not shepherd.is_active()  # reaped by poll()


def test_poll_after_success_exit_returns_ok_and_token(shepherd, monkeypatch) -> None:
    # A setup-token flow that exits 0 with a token line: poll() surfaces ok + the token once.
    # setup-token is a PTY flow (#846) — write the code to the pty master (os.write), not
    # a proc.stdin pipe (which is None for this transport; see `_write_code`).
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_TOKEN", "polled-token-xyz")
    shepherd.start("setup-token")
    # Feed the code so the stub prints success + token and exits 0, but DON'T drain it via
    # submit_code — poll() should pick up the completed process and return the terminal result.
    os.write(shepherd._flow.master_fd, b"the-code\n")  # noqa: SLF001
    shepherd._flow.proc.wait(timeout=5)  # noqa: SLF001 - let it finish
    result = shepherd.poll()
    assert result["ok"] is True
    assert result["token"] == "polled-token-xyz"
    assert "pending" not in result
    assert not shepherd.is_active()


def test_poll_after_failure_exit_is_terminal_and_reaps(shepherd, monkeypatch) -> None:
    # A rejected code (exit 1): poll() after it exits returns a terminal ok:false (no pending)
    # and reaps the flow.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "reject_code")
    shepherd.start("login")
    shepherd._flow.proc.stdin.write("bad-code\n")  # noqa: SLF001
    shepherd._flow.proc.stdin.flush()  # noqa: SLF001
    shepherd._flow.proc.wait(timeout=5)  # noqa: SLF001
    result = shepherd.poll()
    assert result["ok"] is False
    assert result.get("pending") is not True
    assert not shepherd.is_active()


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


def test_write_code_plain_pipe_skipped_when_stdin_is_none(shepherd) -> None:
    # `_write_code`'s plain-pipe branch (`flow.master_fd is None`) guards the write on
    # `flow.proc.stdin is not None and not flow.stdin_closed` — cover the `stdin is None`
    # edge (e.g. a Popen spawned without capturing stdin) where the write must be
    # skipped rather than raising an AttributeError on `None.write(...)`.
    class _FakeProc:
        stdin = None

    flow = ls._Flow(mode="login", proc=_FakeProc())  # type: ignore[arg-type]
    shepherd._write_code(flow, "a-code")  # noqa: SLF001 - internals test; must not raise
    assert flow.stdin_closed is False  # untouched — the write was never attempted


def test_write_code_plain_pipe_skipped_when_stdin_already_closed(shepherd) -> None:
    # Same guard, other half: `stdin` is set but `stdin_closed` is already True (a prior
    # teardown/close already ran) — the write must still be skipped, not attempted again.
    calls: list[str] = []

    class _FakeStdin:
        def write(self, data: str) -> None:
            calls.append(data)

        def flush(self) -> None:
            calls.append("flush")

    class _FakeProc:
        stdin = _FakeStdin()

    flow = ls._Flow(mode="login", proc=_FakeProc(), stdin_closed=True)  # type: ignore[arg-type]
    shepherd._write_code(flow, "a-code")  # noqa: SLF001 - internals test; must not raise
    assert calls == []  # the already-closed stdin was never written to


def test_finalize_exited_skips_join_when_no_reader_thread(shepherd) -> None:
    # `_finalize_exited` joins `flow.reader_thread` only `if ... is not None` — cover the
    # None edge (a flow torn down/finalized before its reader thread was ever assigned,
    # or one whose thread already completed and was cleared) where the join is skipped.
    class _FakeProc:
        stdin = None

        def poll(self):
            return 0  # already exited — _finalize_exited's own _teardown() call is a no-op

    flow = ls._Flow(mode="login", proc=_FakeProc(), reader_thread=None)  # type: ignore[arg-type]
    result = shepherd._finalize_exited(flow, 0)  # noqa: SLF001 - internals test
    assert result["ok"] is True


def test_teardown_skips_both_stdin_branches_when_neither_transport_is_open(shepherd) -> None:
    # `_teardown`'s stdin-close step is `if master_fd is not None: ... elif proc.stdin is
    # not None: ...` — cover the case where NEITHER is set (already closed by an earlier
    # teardown, or a flow whose transport was never opened): both branches must be
    # skipped rather than raising on a None close.
    class _FakeProc:
        stdin = None

        def poll(self):
            return 0  # already exited — no terminate/kill/wait needed

    flow = ls._Flow(mode="login", proc=_FakeProc(), master_fd=None)  # type: ignore[arg-type]
    shepherd._teardown(flow)  # noqa: SLF001 - internals test; must not raise
    assert flow.stdin_closed is False  # neither branch ran, so the flag was never flipped


def test_teardown_skips_reader_join_when_no_reader_thread(shepherd) -> None:
    # `_teardown` joins `flow.reader_thread` only `if ... is not None` — cover the None
    # edge (mirrors `_finalize_exited`'s same guard) where the join is skipped.
    class _FakeProc:
        stdin = None

        def poll(self):
            return 0  # already exited

    flow = ls._Flow(mode="login", proc=_FakeProc(), reader_thread=None)  # type: ignore[arg-type]
    shepherd._teardown(flow)  # noqa: SLF001 - internals test; must not raise (no join attempted)


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


def _cfg(
    *,
    login_shepherd_enabled: bool,
    auth_enabled: bool,
    tmp_path: Path,
    allow_setup_token: bool = False,
) -> ClausterConfig:
    return ClausterConfig.model_validate(
        {
            "projects_root": str(tmp_path / "projects"),
            "state_dir": str(tmp_path / "state"),
            "claude": {"binary": str(FAKE_CLAUDE)},
            "login_shepherd": {
                "enabled": login_shepherd_enabled,
                "allow_setup_token": allow_setup_token,
            },
            "auth": {"enabled": auth_enabled},
        }
    )


def _client(
    tmp_path: Path,
    *,
    enabled: bool,
    auth_enabled: bool = False,
    allow_setup_token: bool = False,
) -> TestClient:
    (tmp_path / "projects").mkdir(exist_ok=True)
    cfg = _cfg(
        login_shepherd_enabled=enabled,
        auth_enabled=auth_enabled,
        tmp_path=tmp_path,
        allow_setup_token=allow_setup_token,
    )
    return TestClient(create_app(cfg))


def test_flag_defaults_false(tmp_path: Path) -> None:
    cfg = ClausterConfig.model_validate({"projects_root": str(tmp_path)})
    assert cfg.login_shepherd.enabled is False


def test_allow_setup_token_defaults_false(tmp_path: Path) -> None:
    # Second gate (#846), mirroring config_write.allow_user_scope: off by default,
    # so setup-token stays invisible even when the base surface is turned on.
    cfg = ClausterConfig.model_validate({"projects_root": str(tmp_path)})
    assert cfg.login_shepherd.allow_setup_token is False


def test_routes_404_when_disabled(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=False) as c:
        assert c.post("/api/login-shepherd/start", json={"mode": "login"}).status_code == 404
        assert c.post("/api/login-shepherd/code", json={"code": "x"}).status_code == 404
        assert c.post("/api/login-shepherd/status").status_code == 404
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


# --- second gate (#846): setup-token requires allow_setup_token too -----------------


def test_setup_token_404s_when_enabled_but_allow_setup_token_false(tmp_path: Path) -> None:
    # Second gate mirrors config_write.allow_user_scope: enabled=True alone is not
    # enough for the higher-risk mode. A distinct 403 would leak that the mode
    # exists-but-is-disabled, so this must be the SAME 404 shape as the whole
    # surface being off (invisible-surface invariant).
    with _client(tmp_path, enabled=True, allow_setup_token=False) as c:
        resp = c.post("/api/login-shepherd/start", json={"mode": "setup-token"})
        assert resp.status_code == 404


def test_login_mode_works_regardless_of_allow_setup_token(tmp_path: Path, monkeypatch) -> None:
    # `login` needs only the base gate — allow_setup_token being off must not affect it.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    with _client(tmp_path, enabled=True, allow_setup_token=False) as c:
        resp = c.post("/api/login-shepherd/start", json={"mode": "login"})
        assert resp.status_code == 200
        assert c.post("/api/login-shepherd/cancel").status_code == 200


def test_setup_token_works_when_both_gates_are_on(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    with _client(tmp_path, enabled=True, allow_setup_token=True) as c:
        resp = c.post("/api/login-shepherd/start", json={"mode": "setup-token"})
        assert resp.status_code == 200
        assert resp.json()["authorize_url"].startswith("https://")
        assert c.post("/api/login-shepherd/cancel").status_code == 200


def test_setup_token_still_404s_when_whole_surface_disabled(tmp_path: Path) -> None:
    # allow_setup_token alone (without the base enabled flag) must not open the surface.
    with _client(tmp_path, enabled=False, allow_setup_token=True) as c:
        resp = c.post("/api/login-shepherd/start", json={"mode": "setup-token"})
        assert resp.status_code == 404


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


def test_code_route_still_verifying_is_200_ok_false(tmp_path: Path, monkeypatch) -> None:
    # A slow-but-valid login (still running past the submit timeout) returns a normal 200
    # with ok=false + a "still verifying" message — NOT an error, and the flow stays active
    # (the lifespan shutdown reaps it when the client context exits).
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "hang_after_code")
    monkeypatch.setattr(ls, "SUBMIT_TIMEOUT_SECONDS", 0.5)
    with _client(tmp_path, enabled=True) as c:
        start = c.post("/api/login-shepherd/start", json={"mode": "login"})
        assert start.status_code == 200
        code = c.post("/api/login-shepherd/code", json={"code": "the-code"})
        assert code.status_code == 200
        body = code.json()
        assert body["ok"] is False
        assert body["pending"] is True  # the UI keys off this to stay IN-PROGRESS, not finished
        assert "still verifying" in body["message"]
        # Flow is intentionally left active; explicitly cancel so shutdown is clean-fast.
        assert c.post("/api/login-shepherd/cancel").status_code == 200


def test_status_route_without_active_flow_is_409(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=True) as c:
        resp = c.post("/api/login-shepherd/status")
        assert resp.status_code == 409  # the client's cue to stop polling


def test_status_route_still_running_then_terminal(tmp_path: Path, monkeypatch) -> None:
    # The pending-poll flow end-to-end through the routes: submit → pending, /status →
    # pending while running, then (after the login finishes) /status → terminal + reaped.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "hang_after_code")
    monkeypatch.setattr(ls, "SUBMIT_TIMEOUT_SECONDS", 0.3)
    (tmp_path / "projects").mkdir(exist_ok=True)
    cfg = _cfg(login_shepherd_enabled=True, auth_enabled=False, tmp_path=tmp_path)
    app = create_app(cfg)
    with TestClient(app) as c:
        assert c.post("/api/login-shepherd/start", json={"mode": "login"}).status_code == 200
        submit = c.post("/api/login-shepherd/code", json={"code": "the-code"})
        assert submit.json()["pending"] is True

        pending = c.post("/api/login-shepherd/status")
        assert pending.status_code == 200
        assert pending.json()["pending"] is True

        # Let the still-running login finish, then /status returns the terminal result and
        # reaps the flow — a subsequent /status is 409 (flow gone → stop polling).
        app.state.login_shepherd._flow.proc.terminate()  # noqa: SLF001 - provider "finished"
        app.state.login_shepherd._flow.proc.wait(timeout=5)  # noqa: SLF001
        terminal = c.post("/api/login-shepherd/status")
        assert terminal.status_code == 200
        assert "pending" not in terminal.json()
        assert c.post("/api/login-shepherd/status").status_code == 409


def test_start_route_unresolvable_binary_is_400_not_500(tmp_path: Path) -> None:
    # An unresolvable `claude` binary raises claude_cli.ClaudeNotFound inside start();
    # it must be re-raised as LoginShepherdError so the route returns a clean 400, never
    # a 500 that escapes the route's except.
    (tmp_path / "projects").mkdir(exist_ok=True)
    cfg = ClausterConfig.model_validate(
        {
            "projects_root": str(tmp_path / "projects"),
            "state_dir": str(tmp_path / "state"),
            "claude": {"binary": "definitely-not-a-real-claude-binary-xyz"},
            "login_shepherd": {"enabled": True},
        }
    )
    with TestClient(create_app(cfg)) as c:
        resp = c.post("/api/login-shepherd/start", json={"mode": "login"})
        assert resp.status_code == 400


def test_lifespan_shutdown_reaps_an_active_flow(tmp_path: Path, monkeypatch) -> None:
    # FIX #839: an in-flight login subprocess (started, awaiting a pasted code) must be
    # cancelled + reaped when the app's lifespan exits — not left running past shutdown.
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN_MODE", "ready")
    (tmp_path / "projects").mkdir(exist_ok=True)
    cfg = _cfg(login_shepherd_enabled=True, auth_enabled=False, tmp_path=tmp_path)
    app = create_app(cfg)
    with TestClient(app) as c:
        start = c.post("/api/login-shepherd/start", json={"mode": "login"})
        assert start.status_code == 200
        shepherd = app.state.login_shepherd
        assert shepherd.is_active()
        proc = shepherd._flow.proc  # noqa: SLF001 - confirm the reap after lifespan exit
    # The `with` block exited → FastAPI ran lifespan shutdown, which cancels the shepherd.
    assert not shepherd.is_active()
    assert proc.poll() is not None  # terminated + reaped, not orphaned


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
        # The pending-poll wiring must ship with the panel: the "Recheck now" control and
        # the /status poll endpoint (used by the interval + manual recheck).
        assert "recheck()" in resp.text
        assert "/api/login-shepherd/status" in resp.text
        # Overlapping-poll defenses must ship too: the single-flight `rechecking` guard,
        # and the terminal-wins guard that returns early once `finished` (so a late
        # status/409 response can't clobber an already-captured one-time token).
        assert "rechecking" in resp.text
        assert "this.finished || this.rechecking" in resp.text
    with _client(tmp_path, enabled=False) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        assert 'x-data="loginShepherd()"' not in resp.text


# --- template: the setup-token mode control is gated on allow_setup_token (#846) ----


def test_setup_token_control_absent_when_allow_setup_token_false(tmp_path: Path) -> None:
    # enabled=True but allow_setup_token=False (the default): only the single
    # subscription-sign-in mode renders, no two-option toggle and no way to pick
    # or submit "setup-token" from the DOM.
    with _client(tmp_path, enabled=True, allow_setup_token=False) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert "Create a long-lived token" not in html
        assert 'value="setup-token"' not in html
        assert 'class="ls-mode-toggle' not in html
        # The single-mode fallback description still renders (no toggle, just this).
        assert "Opens a one-time authorize link tied to your Claude subscription." in html


def test_setup_token_control_present_when_allow_setup_token_true(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=True, allow_setup_token=True) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert "Create a long-lived token" in html
        assert 'value="setup-token"' in html
        assert 'class="ls-mode-toggle' in html


def test_setup_token_control_absent_when_whole_surface_disabled(tmp_path: Path) -> None:
    # allow_setup_token alone can't resurrect the panel when the base gate is off.
    with _client(tmp_path, enabled=False, allow_setup_token=True) as c:
        resp = c.get("/")
        assert resp.status_code == 200
        assert "Create a long-lived token" not in resp.text
