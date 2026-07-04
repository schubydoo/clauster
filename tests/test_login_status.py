"""Unit tests for :mod:`clauster.login_status` (#838).

Fully offline: credentials come from temp files under the isolated HOME/tmp_path,
never the real ``~/.claude``.
"""

from __future__ import annotations

import json
from pathlib import Path

from clauster.login_status import check_login_status, credentials_path_for

NOW_MS = 1_800_000_000_000  # fixed reference instant, well past any 2024-era token


def _claude_json(tmp_path: Path) -> Path:
    return tmp_path / "claude.json"


def _write_creds(claude_json: Path, payload: str) -> Path:
    creds = credentials_path_for(claude_json)
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(payload, encoding="utf-8")
    return creds


def test_credentials_path_is_sibling_dot_claude_dir(tmp_path):
    claude_json = tmp_path / "claude.json"
    creds = credentials_path_for(claude_json)
    assert creds == tmp_path / ".claude" / ".credentials.json"


def test_valid_unexpired_token_is_logged_in(tmp_path):
    claude_json = _claude_json(tmp_path)
    _write_creds(
        claude_json,
        json.dumps({"claudeAiOauth": {"accessToken": "tok-abc", "expiresAt": NOW_MS + 3_600_000}}),
    )
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is True
    assert result.expired is False
    assert result.expires_at_ms == NOW_MS + 3_600_000
    assert "tok-abc" not in result.reason  # never surfaces the token value


def test_valid_token_with_no_expiry_field_is_logged_in(tmp_path):
    # expiresAt is documented as present, but the helper must not crash/deny if a
    # future credentials shape omits it — treat "no expiry info" as not-expired.
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({"claudeAiOauth": {"accessToken": "tok-abc"}}))
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is True
    assert result.expired is False
    assert result.expires_at_ms is None


def test_expired_token_is_not_logged_in(tmp_path):
    claude_json = _claude_json(tmp_path)
    _write_creds(
        claude_json,
        json.dumps({"claudeAiOauth": {"accessToken": "tok-abc", "expiresAt": NOW_MS - 1000}}),
    )
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False
    assert result.expired is True
    assert result.expires_at_ms == NOW_MS - 1000
    assert "expired" in result.reason


def test_expiry_exactly_now_counts_as_expired(tmp_path):
    # >= now_ms, not just >, matches ops._check_claude_login / environments.load_credentials.
    claude_json = _claude_json(tmp_path)
    _write_creds(
        claude_json, json.dumps({"claudeAiOauth": {"accessToken": "tok-abc", "expiresAt": NOW_MS}})
    )
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False
    assert result.expired is True


def test_missing_credentials_file_fails_closed(tmp_path):
    claude_json = _claude_json(tmp_path)  # never write the .credentials.json
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False
    assert result.expired is False
    assert result.expires_at_ms is None
    assert "no credentials file" in result.reason


def test_malformed_json_fails_closed(tmp_path):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, "{not valid json")
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False
    assert "not valid JSON" in result.reason


def test_missing_access_token_fails_closed(tmp_path):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({"claudeAiOauth": {}}))
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False
    assert "accessToken" in result.reason


def test_empty_access_token_fails_closed(tmp_path):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({"claudeAiOauth": {"accessToken": ""}}))
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False


def test_missing_oauth_key_fails_closed(tmp_path):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({}))
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False
    assert "accessToken" in result.reason


def test_oauth_value_not_a_dict_does_not_crash(tmp_path):
    # Valid JSON where claudeAiOauth is e.g. a string/null must not raise AttributeError.
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps({"claudeAiOauth": "not-an-object"}))
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False


def test_top_level_json_not_a_dict_does_not_crash(tmp_path):
    claude_json = _claude_json(tmp_path)
    _write_creds(claude_json, json.dumps(["a", "list"]))
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False


def test_non_int_expires_at_is_treated_as_absent(tmp_path):
    claude_json = _claude_json(tmp_path)
    payload = {"claudeAiOauth": {"accessToken": "tok-abc", "expiresAt": "not-a-number"}}
    _write_creds(claude_json, json.dumps(payload))
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is True
    assert result.expires_at_ms is None
    assert result.expired is False


def test_unreadable_file_fails_closed_not_raises(tmp_path, monkeypatch):
    claude_json = _claude_json(tmp_path)
    creds = _write_creds(claude_json, json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))

    real_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self == creds:
            raise PermissionError("simulated permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)
    result = check_login_status(claude_json, now_ms=NOW_MS)
    assert result.logged_in is False
    assert "could not read" in result.reason
