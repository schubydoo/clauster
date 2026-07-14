"""Unit tests for :mod:`clauster.login_status` (#838).

Fully offline: the login signal comes from the parameterizable fake `claude`
stub (env-var scripted, never the real `claude auth status`), and any credentials
file lives under tmp_path — never the real ``~/.claude``.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from clauster.login_status import LoginStatus, LoginStatusCache, check_login_status

# These tests spawn the fake stub as a real subprocess (check_login_status). On
# Windows the extensionless POSIX shebang script isn't a valid Win32 executable, so
# point at the sibling `.cmd` forwarder (which shells out to `python` and forwards
# argv + env + exit code) — mirroring conftest's WIN_STUB_SUFFIX. Without this the
# spawn dies with `[WinError 193] %1 is not a valid Win32 application`.
_WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""
FAKE_CLAUDE = (
    Path(__file__).resolve().parent / "fixtures" / "fake_claude" / f"claude{_WIN_STUB_SUFFIX}"
)


def _claude_json(tmp_path: Path) -> Path:
    return tmp_path / "claude.json"


def _write_creds(claude_json: Path, payload: str) -> Path:
    creds = claude_json.parent / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(payload, encoding="utf-8")
    return creds


def _set_auth(monkeypatch, *, stdout=None, exit_code=None, hang=False):
    if stdout is not None:
        monkeypatch.setenv("FAKE_CLAUDE_AUTH_STDOUT", stdout)
    if exit_code is not None:
        monkeypatch.setenv("FAKE_CLAUDE_AUTH_EXIT_CODE", str(exit_code))
    if hang:
        monkeypatch.setenv("FAKE_CLAUDE_AUTH_HANG", "1")
        # The caller's timeout is patched to 0.3s, so the stub only needs to outlast that.
        # Keep it short: on Windows the .cmd wrapper's grandchild survives the timeout-kill
        # and holds the pipe for the whole sleep, so the default 8s would make this ~8s there
        # (the 30s default made it a 30s outlier — the pytest --durations winner).
        monkeypatch.setenv("FAKE_CLAUDE_AUTH_HANG_SECONDS", "1")


def test_logged_in_via_claude_ai_oauth(tmp_path, monkeypatch):
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is True
    assert result.method == "claude.ai"
    assert result.reason == "logged in"


def test_logged_in_via_api_key_helper_no_creds_file(tmp_path, monkeypatch):
    # The whole point of the CLI signal: an apiKeyHelper deployment has NO
    # .credentials.json yet is fully logged in — must NOT false-alarm.
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "apiKeyHelper"}')
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is True
    assert result.method == "apiKeyHelper"
    assert result.expires_at_ms is None  # non-OAuth method never reads a creds file


def test_logged_in_via_api_key_env(tmp_path, monkeypatch):
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "apiKey"}')
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is True
    assert result.method == "apiKey"


def test_not_logged_in(tmp_path, monkeypatch):
    _set_auth(monkeypatch, stdout='{"loggedIn": false}')
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is False
    assert result.method is None
    assert result.expires_at_ms is None


def test_not_logged_in_keeps_method_when_present(tmp_path, monkeypatch):
    _set_auth(monkeypatch, stdout='{"loggedIn": false, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is False
    assert result.method == "claude.ai"


def test_oauth_expiry_surfaced_when_creds_present(tmp_path, monkeypatch):
    # claude.ai OAuth + a readable .credentials.json -> proactively surface expiry.
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({"claudeAiOauth": {"expiresAt": 1_800_000_000_000}}))
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), claude_json)
    assert result.logged_in is True
    assert result.expires_at_ms == 1_800_000_000_000


def test_oauth_expiry_none_when_creds_missing(tmp_path, monkeypatch):
    # OAuth method but no creds file yet -> expiry simply absent (never an error).
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is True
    assert result.expires_at_ms is None


def test_oauth_expiry_ignores_malformed_creds(tmp_path, monkeypatch):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, "{not valid json")
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), claude_json)
    assert result.logged_in is True
    assert result.expires_at_ms is None


def test_oauth_expiry_ignores_non_int_expiry(tmp_path, monkeypatch):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({"claudeAiOauth": {"expiresAt": "soon"}}))
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), claude_json)
    assert result.expires_at_ms is None


def test_oauth_expiry_ignores_non_dict_oauth(tmp_path, monkeypatch):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({"claudeAiOauth": "nope"}))
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), claude_json)
    assert result.expires_at_ms is None


def test_oauth_expiry_ignores_non_dict_top_level(tmp_path, monkeypatch):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps(["a", "list"]))
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), claude_json)
    assert result.expires_at_ms is None


def test_non_oauth_method_never_reads_creds(tmp_path, monkeypatch):
    # Even if a stale .credentials.json exists, an apiKey login must not surface it.
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({"claudeAiOauth": {"expiresAt": 1_800_000_000_000}}))
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "apiKey"}')
    result = check_login_status(str(FAKE_CLAUDE), claude_json)
    assert result.expires_at_ms is None


def test_command_nonzero_exit_fails_closed(tmp_path, monkeypatch):
    _set_auth(monkeypatch, stdout="", exit_code=1)
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is False
    assert "exited 1" in result.reason


def test_malformed_json_fails_closed(tmp_path, monkeypatch):
    _set_auth(monkeypatch, stdout="not json at all")
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is False
    assert "non-JSON" in result.reason


def test_non_object_json_fails_closed(tmp_path, monkeypatch):
    _set_auth(monkeypatch, stdout="[1, 2, 3]")
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is False
    assert "non-object" in result.reason


def test_timeout_fails_closed(tmp_path, monkeypatch):
    # Drive the real timeout path with a stub that hangs, but shrink the bound so
    # the test is fast rather than waiting the full 5s.
    monkeypatch.setattr("clauster.login_status._AUTH_STATUS_TIMEOUT_S", 0.3)
    _set_auth(monkeypatch, hang=True)
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is False
    assert "timed out" in result.reason


def test_missing_binary_fails_closed(tmp_path):
    result = check_login_status("definitely-not-claude-xyz", _claude_json(tmp_path))
    assert result.logged_in is False
    assert "not found" in result.reason


def test_oserror_running_command_fails_closed(tmp_path, monkeypatch):
    # subprocess.run raises OSError (e.g. exec failure) -> fail closed, no raise.
    import clauster.login_status as ls

    def _boom(*args, **kwargs):
        raise OSError("simulated exec failure")

    monkeypatch.setattr(ls.subprocess, "run", _boom)
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is False
    assert "could not be run" in result.reason


def test_empty_auth_method_normalized_to_none(tmp_path, monkeypatch):
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": ""}')
    result = check_login_status(str(FAKE_CLAUDE), _claude_json(tmp_path))
    assert result.logged_in is True
    assert result.method is None


def test_token_value_never_appears_in_result(tmp_path, monkeypatch):
    # The OAuth token in .credentials.json must never leak into any returned field.
    claude_json = _claude_json(tmp_path)
    oauth = {"accessToken": "tok-SECRET-XYZ", "expiresAt": 1_800_000_000_000}
    _write_creds(claude_json, json.dumps({"claudeAiOauth": oauth}))
    _set_auth(monkeypatch, stdout='{"loggedIn": true, "authMethod": "claude.ai"}')
    result = check_login_status(str(FAKE_CLAUDE), claude_json)
    assert "tok-SECRET-XYZ" not in repr(result)


# ----- LoginStatusCache (stale-while-revalidate, non-blocking, single-flight) -----


class _CountingProbe:
    """A deterministic stand-in for check_login_status: counts calls, returns a
    fixed result, and can optionally block on an event to simulate a slow/wedged
    probe (so a test can assert read() does NOT wait on it). ``entered`` fires as
    soon as the probe body runs, so a test can wait for the refresh to actually be
    in flight without a fixed sleep."""

    def __init__(self, result, *, block=None):
        self.result = result
        self.block = block  # an Event the probe waits on before returning, if set
        self.calls = 0
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, binary, claude_json):
        with self._lock:
            self.calls += 1
        self.entered.set()
        if self.block is not None:
            self.block.wait(timeout=5.0)
        return self.result


class _ManualClock:
    """A monotonic clock a test advances by hand, so TTL expiry is deterministic."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


LOGGED_IN = LoginStatus(True, "claude.ai", None, "logged in")
LOGGED_OUT = LoginStatus(False, None, None, "claude reports not logged in")


def _cache(probe, *, clock=None, ttl_s=30.0, tmp_path=None):
    return LoginStatusCache(
        "claude",
        (tmp_path / "claude.json") if tmp_path is not None else Path("/nonexistent/claude.json"),
        ttl_s=ttl_s,
        clock=clock or (lambda: 0.0),
        probe=probe,
    )


def test_cache_cold_start_is_unknown_and_quiet():
    # No probe has run yet -> a NEUTRAL unknown that renders quiet: logged_in True
    # (so no logged-out pill) but known False (not a confirmed result).
    probe = _CountingProbe(LOGGED_OUT)
    cache = _cache(probe)
    first = cache.read()
    assert first.known is False
    assert first.logged_in is True  # quiet — never cry wolf before probing
    cache.wait_for_pending_refresh()
    assert probe.calls == 1  # the cold read kicked exactly one refresh


def test_cache_serves_probe_result_after_refresh():
    probe = _CountingProbe(LOGGED_OUT)
    cache = _cache(probe)
    cache.read()  # cold-start unknown; kicks the probe
    cache.wait_for_pending_refresh()
    settled = cache.read()
    assert settled.known is True
    assert settled.logged_in is False  # the real (logged-out) result now served


def test_cache_hit_within_ttl_does_not_reprobe():
    clock = _ManualClock()
    probe = _CountingProbe(LOGGED_IN)
    cache = _cache(probe, clock=clock, ttl_s=30.0)
    cache.read()
    cache.wait_for_pending_refresh()
    assert probe.calls == 1
    # A read well within the TTL returns the cached value and spawns NO new probe.
    clock.now = 29.0
    hit = cache.read()
    cache.wait_for_pending_refresh()
    assert hit.logged_in is True
    assert probe.calls == 1  # still one — served from cache, no subprocess


def test_cache_stale_read_triggers_one_refresh():
    clock = _ManualClock()
    probe = _CountingProbe(LOGGED_IN)
    cache = _cache(probe, clock=clock, ttl_s=30.0)
    cache.read()
    cache.wait_for_pending_refresh()
    assert probe.calls == 1
    clock.now = 31.0  # past the TTL -> next read is stale
    cache.read()
    cache.wait_for_pending_refresh()
    assert probe.calls == 2  # exactly one additional refresh


def test_cache_thread_spawn_failure_does_not_raise_or_wedge(monkeypatch):
    # If Thread.start() fails (OS thread exhaustion, `can't start new thread`),
    # read() must NOT raise (never 500 /healthz) and must NOT leave _refreshing stuck
    # True — a later read has to be able to retry. `_refreshing`/`_thread` are set only
    # after a successful start, so both hold.
    probe = _CountingProbe(LOGGED_OUT)
    cache = _cache(probe)

    calls = {"n": 0}
    real_start = threading.Thread.start

    def _flaky_start(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", _flaky_start)

    first = cache.read()  # spawn fails -> swallowed, stale/unknown returned
    assert first.known is False  # still the cold-start value (no probe ran)
    assert probe.calls == 0

    # The single-flight flag was NOT wedged: a subsequent read retries and, now that
    # start() succeeds, actually runs the probe.
    second = cache.read()
    cache.wait_for_pending_refresh()
    assert probe.calls == 1
    assert cache.read().logged_in is False  # real result served after recovery
    assert second is not None


def test_cache_slow_probe_does_not_block_read():
    # A wedged probe must NOT delay read(): it returns the stale/cold value at once
    # while the probe runs on the background thread.
    gate = threading.Event()  # the probe blocks until we set this
    probe = _CountingProbe(LOGGED_OUT, block=gate)
    cache = _cache(probe)
    # This read must return immediately even though the probe is stuck waiting.
    value = cache.read()
    assert value.known is False  # cold-start unknown, returned without waiting
    assert probe.entered.wait(5.0)  # the probe was kicked (and is now blocked)
    assert probe.calls == 1
    gate.set()  # release the probe so the daemon thread can finish
    cache.wait_for_pending_refresh()
    assert cache.read().logged_in is False  # real result served once it lands


def test_cache_single_flight_under_concurrent_reads():
    # Many concurrent stale reads must spawn AT MOST ONE refresh thread. Hold the
    # probe on a gate so all reader threads observe the same in-flight refresh.
    gate = threading.Event()
    probe = _CountingProbe(LOGGED_IN, block=gate)
    cache = _cache(probe)
    barrier = threading.Barrier(8)

    def _hammer():
        barrier.wait()  # release all readers at once to maximize contention
        cache.read()

    readers = [threading.Thread(target=_hammer) for _ in range(8)]
    for t in readers:
        t.start()
    for t in readers:
        t.join(5.0)
    # All 8 reads happened while the single refresh was in flight -> only one probe.
    assert probe.entered.wait(5.0)  # the one refresh thread has started
    assert probe.calls == 1
    gate.set()
    cache.wait_for_pending_refresh()


def test_cache_wait_for_pending_refresh_noop_when_idle():
    # No refresh in flight (nothing ever read) -> the helper is a harmless no-op.
    cache = _cache(_CountingProbe(LOGGED_IN))
    cache.wait_for_pending_refresh()  # must not raise


class _RaisingProbe:
    """A probe that always raises, to exercise `_refresh`'s fail-closed except."""

    def __init__(self):
        self.calls = 0

    def __call__(self, binary, claude_json):
        self.calls += 1
        raise RuntimeError("probe blew up")


def test_cache_probe_that_raises_fails_closed_and_clears_refreshing():
    # `_refresh`'s broad except must convert an unexpected probe failure into a
    # fail-closed result AND still clear the single-flight flag, so a one-off failure
    # can't wedge the cache into "never refresh again". Assert the surfaced result and
    # that a later stale read actually re-probes (proving `_refreshing` was cleared).
    clock = _ManualClock()
    probe = _RaisingProbe()
    cache = _cache(probe, clock=clock, ttl_s=30.0)
    cache.read()  # cold-start kicks the probe, which raises
    cache.wait_for_pending_refresh()
    settled = cache.read()
    assert settled == LoginStatus(False, None, None, "login-status probe failed")
    assert probe.calls == 1
    # A stale read re-probes -> the flag was cleared, not wedged True.
    clock.now = 31.0
    cache.read()
    cache.wait_for_pending_refresh()
    assert probe.calls == 2
