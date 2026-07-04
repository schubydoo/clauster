"""Unit tests for :mod:`clauster.login_status` (#838).

Fully offline: the login signal comes from the parameterizable fake `claude`
stub (env-var scripted, never the real `claude auth status`), and any credentials
file lives under tmp_path — never the real ``~/.claude``.
"""

from __future__ import annotations

import json
from pathlib import Path

from clauster.login_status import check_login_status

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


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
