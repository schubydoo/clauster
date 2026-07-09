from __future__ import annotations

import json
from pathlib import Path

from clauster import code_sessions
from clauster.code_sessions import AnchorHealth, CodeSessionsClient, code_session_id_for
from clauster.environments import Credentials

CREDS = Credentials(access_token="tok", organization_uuid="org")


def _transport(status: int, body):
    """A fake transport returning ``(status, bytes)``; body may be dict/bytes."""

    def _t(method: str, url: str, headers: dict, body_: bytes | None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return status, raw

    return _t


def test_code_session_id_for_swaps_prefix():
    assert code_session_id_for("session_01ABCdef") == "cse_01ABCdef"


def test_anchor_health_active_is_healthy():
    c = CodeSessionsClient(
        CREDS, transport=_transport(200, {"response_shape": {"status": "active"}})
    )
    assert c.anchor_health("cse_x") is AnchorHealth.HEALTHY


def test_anchor_health_idle_flat_body_is_healthy():
    # Tolerate a flat body (no response_shape wrapper).
    c = CodeSessionsClient(CREDS, transport=_transport(200, {"status": "idle"}))
    assert c.anchor_health("cse_x") is AnchorHealth.HEALTHY


def test_anchor_health_archived_is_poisoned():
    c = CodeSessionsClient(
        CREDS, transport=_transport(200, {"response_shape": {"status": "archived"}})
    )
    assert c.anchor_health("cse_x") is AnchorHealth.POISONED


def test_anchor_health_404_session_not_found_is_poisoned():
    body = {
        "type": "error",
        "error": {"type": "not_found_error", "resource_type": "session", "message": "gone"},
    }
    assert (
        CodeSessionsClient(CREDS, transport=_transport(404, body)).anchor_health("cse_x")
        is AnchorHealth.POISONED
    )


def test_anchor_health_404_route_not_found_is_unknown():
    # A route-level 404 (unrecognized body) must NOT clear a healthy pointer.
    for body in (b"<html>404</html>", {"message": "Not Found"}, b"null"):
        c = CodeSessionsClient(CREDS, transport=_transport(404, body))
        assert c.anchor_health("cse_x") is AnchorHealth.UNKNOWN


def test_anchor_health_non_object_2xx_body_is_unknown():
    # A bare null/list/scalar 2xx body must be UNKNOWN, never raise (fail-safe contract).
    for body in (b"null", b"[]", b'"a string"', b"42"):
        c = CodeSessionsClient(CREDS, transport=_transport(200, body))
        assert c.anchor_health("cse_x") is AnchorHealth.UNKNOWN


def test_anchor_health_other_status_is_unknown():
    # A status we haven't verified (e.g. terminated) is conservative: leave the pointer.
    c = CodeSessionsClient(
        CREDS, transport=_transport(200, {"response_shape": {"status": "terminated"}})
    )
    assert c.anchor_health("cse_x") is AnchorHealth.UNKNOWN


def test_anchor_health_server_error_is_unknown():
    c = CodeSessionsClient(CREDS, transport=_transport(500, b"oops"))
    assert c.anchor_health("cse_x") is AnchorHealth.UNKNOWN


def test_anchor_health_malformed_json_is_unknown():
    def _t(method, url, headers, body):
        return 200, b"{not json"

    assert CodeSessionsClient(CREDS, transport=_t).anchor_health("cse_x") is AnchorHealth.UNKNOWN


def test_anchor_health_transport_error_is_unknown():
    def _t(method, url, headers, body):
        raise OSError("network down")

    assert CodeSessionsClient(CREDS, transport=_t).anchor_health("cse_x") is AnchorHealth.UNKNOWN


def test_headers_carry_beta_version_and_org():
    captured: dict = {}

    def _t(method, url, headers, body):
        captured.update(headers)
        return 200, json.dumps({"status": "active"}).encode()

    CodeSessionsClient(CREDS, transport=_t).anchor_health("cse_x")
    assert captured["anthropic-version"] == "2023-06-01"
    assert captured["anthropic-beta"]  # dated managed-agents beta
    assert captured["Authorization"] == "Bearer tok"
    assert captured["x-organization-uuid"] == "org"


def test_anchor_health_for_pointer_no_creds_is_unknown(tmp_path: Path):
    # Missing credential files -> CredentialsError -> UNKNOWN (fail-safe; never raises).
    assert (
        code_sessions.anchor_health_for_pointer(
            "session_x",
            credentials_path=tmp_path / "nope.json",
            claude_json_path=tmp_path / "nope2.json",
        )
        is AnchorHealth.UNKNOWN
    )


def test_anchor_health_for_pointer_unexpected_id_shape_is_unknown(tmp_path: Path):
    # A non-session_ id would mis-derive the cse id (404 -> clear a healthy pointer); skip it.
    called: list = []

    def _t(method, url, headers, body):
        called.append(url)
        return 404, b"{}"

    assert (
        code_sessions.anchor_health_for_pointer("cse_already", transport=_t)
        is AnchorHealth.UNKNOWN
    )
    assert not called  # never even probed


def test_anchor_health_for_pointer_loads_creds_and_classifies(tmp_path: Path):
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
    )
    (tmp_path / "claude.json").write_text(
        json.dumps({"oauthAccount": {"organizationUuid": "org"}})
    )
    health = code_sessions.anchor_health_for_pointer(
        "session_01X",
        credentials_path=tmp_path / ".credentials.json",
        claude_json_path=tmp_path / "claude.json",
        transport=_transport(200, {"response_shape": {"status": "archived"}}),
    )
    assert health is AnchorHealth.POISONED
