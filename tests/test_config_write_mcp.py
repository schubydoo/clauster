"""MCP-server config-write surface (#688) over the #347 Foundation.

Covers the pure structural validator (validate-never-execute, incl. an RCE-negative
sentinel that proves a candidate ``command`` is never run), the project/user read+write
routers, and the gated routes (capability/scope 404, type-the-name 400, bad-shape 422
writing nothing, stale-hash 409, path-escape 400, redaction round-trip).

Every test that touches ``~/.claude.json`` passes an explicit ``tmp_path`` file and
runs under the autouse HOME-isolation fixture — the live account is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_mcp as mcp
from clauster.app import create_app
from clauster.config import load_config

# --- structural validator: accept the valid shapes ---------------------------------


def test_validate_accepts_stdio_entry() -> None:
    mcp.validate_mcp_servers(
        {"srv": {"command": "/bin/foo", "args": ["--flag"], "env": {"K": "v"}}}
    )  # no raise


def test_validate_accepts_minimal_stdio() -> None:
    mcp.validate_mcp_servers({"srv": {"command": "node"}})  # args/env optional


def test_validate_accepts_remote_entry() -> None:
    mcp.validate_mcp_servers(
        {"api": {"type": "http", "url": "https://x/mcp", "headers": {"H": "v"}}}
    )
    mcp.validate_mcp_servers({"api": {"type": "sse", "url": "https://x/sse"}})


def test_validate_accepts_explicit_stdio_type() -> None:
    mcp.validate_mcp_servers({"srv": {"type": "stdio", "command": "node"}})


def test_validate_accepts_empty_map() -> None:
    mcp.validate_mcp_servers({})  # removing all servers is valid


# --- structural validator: reject the bad shapes (→ InvalidCandidateError / 422) ----


@pytest.mark.parametrize(
    "candidate",
    [
        ["not", "a", "dict"],
        {"": {"command": "x"}},  # empty server name
        {"srv": "not-an-object"},
        {"srv": {}},  # stdio with no command
        {"srv": {"command": ""}},  # empty command
        {"srv": {"command": 123}},  # non-string command
        {"srv": {"command": "x", "args": "nope"}},  # args not a list
        {"srv": {"command": "x", "args": [1, 2]}},  # args not strings
        {"srv": {"command": "x", "env": {"K": 1}}},  # env value not a string
        {"srv": {"command": "x", "env": ["nope"]}},  # env not an object
        {"srv": {"command": "x", "bogus": 1}},  # unknown key
        {"srv": {"type": "ftp", "url": "x"}},  # unknown transport
        {"srv": {"type": "http"}},  # remote with no url
        {"srv": {"type": "http", "url": ""}},  # empty url
        {"srv": {"type": "http", "url": "x", "headers": {"H": 1}}},  # bad headers
        {"srv": {"type": "http", "transport": "sse", "url": "x"}},  # type/transport disagree
        {"srv": {"type": 5, "command": "x"}},  # non-string type
    ],
)
def test_validate_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        mcp.validate_mcp_servers(candidate)


# --- TASK-449.8: every documented server-definition field, per transport ------------
# Source: https://code.claude.com/docs/en/mcp (plus /errors for bareElicitationCapability
# and /changelog for request_timeout_ms), cross-checked against the 2.1.318 CLI schema.

_OAUTH_FULL = {
    "clientId": "my-client",
    "callbackPort": 8080,
    "authServerMetadataUrl": "https://auth.example.com/.well-known/openid-configuration",
    "scopes": "channels:read chat:write",
}


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(
            {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "srv"],
                "env": {"K": "v"},
                "timeout": 600000,
                "alwaysLoad": True,
                "bareElicitationCapability": True,
            },
            id="stdio-all-fields",
        ),
        pytest.param(
            {
                "type": "http",
                "url": "https://mcp.example.com/mcp",
                "headers": {"X-Api": "v"},
                "headersHelper": "/opt/bin/get-mcp-auth-headers.sh",
                "oauth": _OAUTH_FULL,
                "timeout": 600000,
                "request_timeout_ms": 120000,
                "alwaysLoad": False,
                "bareElicitationCapability": True,
            },
            id="http-all-fields",
        ),
        pytest.param(
            {"type": "streamable-http", "url": "https://mcp.example.com/mcp"},
            id="streamable-http-alias",
        ),
        pytest.param(
            {
                "type": "sse",
                "url": "https://mcp.example.com/sse",
                "headers": {"X-Api": "v"},
                "headersHelper": "get-headers",
                "oauth": {"callbackPort": 8080},
                "timeout": 1000,
                "request_timeout_ms": 5000,
                "alwaysLoad": True,
                "bareElicitationCapability": False,
            },
            id="sse-all-fields",
        ),
        pytest.param(
            {
                "type": "ws",
                "url": "wss://mcp.example.com/socket",
                "headers": {"Authorization": "Bearer t"},
                "headersHelper": "get-headers",
                "timeout": 1000,
                "alwaysLoad": True,
                "bareElicitationCapability": True,
            },
            id="ws-all-fields",
        ),
        pytest.param(
            {"type": "http", "url": "https://x/mcp", "oauth": {"scopes": "a"}},
            id="oauth-scopes-only",
        ),
        pytest.param(
            {"type": "http", "url": "https://x/mcp", "oauth": {}},
            id="oauth-empty",
        ),
        # CLI-parsed, undocumented fields (2.1.318 `claude mcp add-json` stores each one):
        pytest.param(
            {
                "type": "http",
                "url": "https://x/mcp",
                "tools": [{"name": "t1", "permission_policy": "always_ask"}, {"name": "t2"}],
                "toolPermissions": {"t1": "ask", "t2": "blocked", "t3": "allow"},
                "displayName": "Nice Name",
                "discoveryCache": True,
                "role": "comms",
                "oauth": {"clientId": "c", "xaa": True},
            },
            id="http-cli-parsed-fields",
        ),
        pytest.param(
            {
                "type": "sse",
                "url": "https://x/sse",
                "tools": [{"name": "t", "permission_policy": "always_deny"}],
                "discoveryCache": False,
            },
            id="sse-cli-parsed-fields",
        ),
        pytest.param({"command": "x", "role": "comms"}, id="stdio-role"),
        pytest.param({"type": "ws", "url": "wss://x/s", "role": "comms"}, id="ws-role"),
        pytest.param(
            # The read path masks every oauth string; the sentinel round-trips.
            {
                "type": "http",
                "url": "https://x/mcp",
                "oauth": {"authServerMetadataUrl": cw.REDACTION_SENTINEL},
            },
            id="oauth-metadata-url-sentinel",
        ),
    ],
)
def test_validate_accepts_every_documented_field(entry: dict) -> None:
    mcp.validate_mcp_servers({"srv": entry})  # no raise


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"command": "x", "timeout": 0}, "timeout"),
        ({"command": "x", "timeout": -5}, "timeout"),
        ({"command": "x", "timeout": 1.5}, "timeout"),
        ({"command": "x", "timeout": True}, "timeout"),
        ({"command": "x", "timeout": "600000"}, "timeout"),
        ({"command": "x", "alwaysLoad": "true"}, "alwaysLoad"),
        ({"command": "x", "bareElicitationCapability": 1}, "bareElicitationCapability"),
        # stdio takes no remote-only field
        ({"command": "x", "headersHelper": "h"}, "unknown keys"),
        ({"command": "x", "oauth": {}}, "unknown keys"),
        ({"command": "x", "cwd": "/tmp"}, "unknown keys"),  # not documented, CLI drops it
        ({"type": "http", "url": "u", "headersHelper": 5}, "headersHelper"),
        ({"type": "http", "url": "u", "request_timeout_ms": 0}, "request_timeout_ms"),
        ({"type": "http", "url": "u", "oauth": "x"}, "oauth"),
        ({"type": "http", "url": "u", "oauth": {"clientSecret": "s"}}, "unknown keys"),
        ({"type": "http", "url": "u", "oauth": {"xaa": "yes"}}, "xaa"),
        # CLI-parsed fields: wrong values, and transports where the CLI drops them
        ({"type": "http", "url": "u", "role": "boss"}, "role"),  # CLI drops any non-"comms"
        ({"type": "http", "url": "u", "displayName": 5}, "displayName"),
        ({"type": "http", "url": "u", "discoveryCache": "on"}, "discoveryCache"),
        ({"type": "http", "url": "u", "tools": {"t": 1}}, "tools"),
        ({"type": "http", "url": "u", "tools": [{"permission_policy": "always_ask"}]}, "name"),
        ({"type": "http", "url": "u", "tools": [{"name": "t", "extra": 1}]}, "unknown keys"),
        (
            {"type": "http", "url": "u", "tools": [{"name": "t", "permission_policy": "never"}]},
            "permission_policy",
        ),
        (
            {"type": "http", "url": "u", "tools": [{"name": "t", "permission_policy": ["x"]}]},
            "permission_policy",
        ),
        ({"type": "http", "url": "u", "toolPermissions": {"t": "deny"}}, "toolPermissions"),
        ({"type": "http", "url": "u", "toolPermissions": {"t": ["ask"]}}, "toolPermissions"),
        ({"type": "http", "url": "u", "toolPermissions": ["t"]}, "toolPermissions"),
        ({"command": "x", "displayName": "n"}, "unknown keys"),  # CLI drops it on stdio
        ({"command": "x", "tools": [{"name": "t"}]}, "unknown keys"),
        ({"type": "ws", "url": "wss://x", "discoveryCache": True}, "unknown keys"),
        # Claude Code ignores these, so refuse them (was accepted before TASK-449.8)
        ({"type": "sse", "url": "u", "env": {"K": "v"}}, "ignores"),
        ({"type": "sse", "transport": "sse", "url": "u"}, "transport"),
        ({"transport": "sse", "url": "u"}, "transport"),
        ({"type": "sse-ide", "url": "u"}, "runtime"),
        ({"type": "ws-ide", "url": "u"}, "runtime"),
        ({"type": "claudeai-proxy", "url": "u"}, "runtime"),
        ({"type": "http", "url": "u", "oauth": {"clientId": 5}}, "clientId"),
        ({"type": "http", "url": "u", "oauth": {"callbackPort": 0}}, "callbackPort"),
        ({"type": "http", "url": "u", "oauth": {"callbackPort": 65536}}, "callbackPort"),
        ({"type": "http", "url": "u", "oauth": {"callbackPort": "8080"}}, "callbackPort"),
        ({"type": "http", "url": "u", "oauth": {"callbackPort": True}}, "callbackPort"),
        (
            {"type": "http", "url": "u", "oauth": {"authServerMetadataUrl": "http://a/x"}},
            "authServerMetadataUrl",
        ),
        (
            {"type": "http", "url": "u", "oauth": {"authServerMetadataUrl": "https://"}},
            "authServerMetadataUrl",
        ),
        (
            {
                "type": "http",
                "url": "u",
                "oauth": {"authServerMetadataUrl": "https://a/" + "x" * 4096},
            },
            "authServerMetadataUrl",
        ),
        ({"type": "http", "url": "u", "oauth": {"scopes": ""}}, "scopes"),
        ({"type": "http", "url": "u", "oauth": {"scopes": ["a"]}}, "scopes"),
        # ws: no OAuth (the docs: WebSocket supports neither OAuth nor --transport)
        ({"type": "ws", "url": "wss://x", "oauth": {"clientId": "c"}}, "unknown keys"),
        ({"type": "ws", "url": "wss://x", "env": {"K": "v"}}, "ignores"),
        ({"type": "ws"}, "url"),
        # documented error shapes
        ({"url": "https://x/mcp"}, "no 'type'"),
        ({"type": "sdk", "name": "x"}, "runtime"),
    ],
)
def test_validate_rejects_bad_documented_field_values(entry: dict, match: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match=match):
        mcp.validate_mcp_servers({"srv": entry})


@pytest.mark.parametrize(
    "name", ["workspace", "claude-in-chrome", "computer-use", "constructor", "__proto__"]
)
def test_validate_rejects_reserved_server_names(name: str) -> None:
    # The docs: Claude Code reserves its built-in server names, `claude mcp add` rejects
    # them and a file entry is skipped at load. The CLI also refuses the JS prototype names.
    with pytest.raises(cw.InvalidCandidateError, match="reserved"):
        mcp.validate_mcp_servers({name: {"command": "x"}})


def test_validate_rejects_dotted_server_name() -> None:
    # The docs and the 2.1.318 CLI allow only letters, numbers, hyphens and underscores.
    with pytest.raises(cw.InvalidCandidateError, match="letters, numbers"):
        mcp.validate_mcp_servers({"srv.v2": {"command": "x"}})


@pytest.mark.parametrize(
    ("entry", "want"),
    [
        ({"command": "x"}, "stdio"),
        ({"type": "stdio", "command": "x"}, "stdio"),
        ({"type": "http", "url": "u"}, "http"),
        ({"type": "streamable-http", "url": "u"}, "http"),
        ({"type": "sse", "url": "u"}, "sse"),
        ({"type": "ws", "url": "u"}, "ws"),
    ],
)
def test_entry_transport_normalizes(entry: dict, want: str) -> None:
    assert mcp.entry_transport(entry) == want


def test_new_fields_round_trip_through_project_writer(tmp_path: Path) -> None:
    entry = {
        "type": "http",
        "url": "https://mcp.example.com/mcp",
        "headersHelper": "/opt/bin/h.sh",
        "oauth": _OAUTH_FULL,
        "timeout": 600000,
        "alwaysLoad": True,
        "bareElicitationCapability": True,
    }
    mcp.write_project_servers(tmp_path, {"srv": entry}, expected_hash=None)
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["srv"] == entry  # stored verbatim, nothing dropped


def test_read_masks_every_secret_bearing_field(tmp_path: Path) -> None:
    # Key-name detection alone missed a token under a benign key (GH_PAT, X-Custom) and
    # never looked at headersHelper. Every env/headers value, the helper command and every
    # oauth string are masked; structural fields (type, url, timeout, ports) stay readable.
    stored = {
        "gh": {
            "command": "npx",
            "env": {"GH_PAT": "ghp_FAKE4498", "EMPTY": ""},
        },
        "api": {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"X-Custom": "Bearer sk-FAKE4498"},
            "headersHelper": 'echo \'{"Authorization": "Bearer sk-FAKE4498-helper"}\'',
            "oauth": _OAUTH_FULL,
            "timeout": 600000,
            "alwaysLoad": True,
        },
    }
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": stored}), encoding="utf-8")
    servers, h = mcp.read_project_servers(tmp_path)
    text = json.dumps(servers)
    assert "FAKE4498" not in text
    assert "my-client" not in text and "auth.example.com" not in text
    m = cw.REDACTION_SENTINEL
    assert servers["gh"]["env"] == {"GH_PAT": m, "EMPTY": ""}
    assert servers["api"]["headers"] == {"X-Custom": m}
    assert servers["api"]["headersHelper"] == m
    assert servers["api"]["oauth"] == {
        "clientId": m,
        "callbackPort": 8080,
        "authServerMetadataUrl": m,
        "scopes": m,
    }
    assert servers["api"]["url"] == "https://mcp.example.com/mcp"
    assert servers["api"]["timeout"] == 600000

    # The masked view validates and writes back unchanged: every sentinel keeps its value.
    mcp.write_project_servers(tmp_path, servers, h)
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"] == stored


def test_read_passes_a_non_object_entry_through(tmp_path: Path) -> None:
    # A hand-edited file can hold a non-object server value. The display read must not
    # crash on it; the entry writer refuses it on the next write anyway.
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"odd": "not-an-object"}}), encoding="utf-8"
    )
    servers, _h = mcp.read_project_servers(tmp_path)
    assert servers == {"odd": "not-an-object"}


@pytest.mark.parametrize(
    ("entry", "where"),
    [
        ({"type": "http", "url": cw.REDACTION_SENTINEL}, "new.url"),
        ({"command": "x", "env": {"K": cw.REDACTION_SENTINEL}}, "new.env.K"),
        ({"command": "x", "args": [cw.REDACTION_SENTINEL]}, "new.args[0]"),
        (
            {"type": "http", "url": "https://x/mcp", "oauth": {"clientId": cw.REDACTION_SENTINEL}},
            "new.oauth.clientId",
        ),
    ],
)
def test_entry_writer_refuses_a_sentinel_with_nothing_stored(
    tmp_path: Path, entry: dict, where: str
) -> None:
    # Pasting the masked view of one server into "New" used to drop every sentinel and
    # write a server without its url/env, reporting success. Refuse it whole instead.
    with pytest.raises(cw.InvalidCandidateError, match=where.replace("[", r"\[")):
        mcp.write_project_server_entry(tmp_path, "new", entry, op="add")
    assert not (tmp_path / ".mcp.json").exists()


def test_whole_map_writer_refuses_a_sentinel_with_nothing_stored(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x", "env": {"K": "v"}}}}), encoding="utf-8"
    )
    before = (tmp_path / ".mcp.json").read_bytes()
    _s, h = mcp.read_project_servers(tmp_path)
    m = cw.REDACTION_SENTINEL
    bad = {"a": {"command": "x", "env": {"K": m, "NEW": m}}}
    with pytest.raises(cw.InvalidCandidateError, match=r"a\.env\.NEW"):
        mcp.write_project_servers(tmp_path, bad, h)
    cj = tmp_path / "claude.json"
    with pytest.raises(cw.InvalidCandidateError, match="nothing is stored"):
        mcp.write_user_servers(cj, {"u": {"type": "http", "url": m}})
    with pytest.raises(cw.InvalidCandidateError, match="nothing is stored"):
        mcp.write_project_local_servers(cj, tmp_path, {"l": {"command": "x", "args": [m]}})
    assert (tmp_path / ".mcp.json").read_bytes() == before
    assert not cj.exists()


def test_entry_writer_refuses_a_sentinel_under_a_non_object_stored_value(tmp_path: Path) -> None:
    # The stored `env` is not an object, so nothing backs the sentinel nested under it.
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": {"command": "x", "env": "oops"}}}), encoding="utf-8"
    )
    entry = {"command": "x", "env": {"K": cw.REDACTION_SENTINEL}}
    with pytest.raises(cw.InvalidCandidateError, match=r"s\.env\.K"):
        mcp.write_project_server_entry(tmp_path, "s", entry, op="edit")


@pytest.mark.parametrize(
    "entry",
    [
        {"type": ["http"], "url": "u"},  # unhashable: was a TypeError (500), now a 422
        {"type": {"t": 1}, "url": "u"},
        {"type": "http", "transport": "sse", "url": "u"},
    ],
)
def test_entry_transport_rejects_bad_or_disagreeing_values(entry: dict) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        mcp.validate_mcp_servers({"srv": entry})


def test_unbacked_sentinel_walks_nested_lists() -> None:
    m = cw.REDACTION_SENTINEL
    assert mcp._unbacked_sentinel({"a": [[m]]}, {"a": [[]]}, "") == "a[0][0]"
    assert mcp._unbacked_sentinel({"a": [[m]]}, {"a": [["kept"]]}, "") is None


def test_unbacked_sentinel_matches_merge_redacted_for_a_stored_null() -> None:
    # merge_redacted keeps a stored null under a present key, so that sentinel is backed.
    m = cw.REDACTION_SENTINEL
    assert mcp._unbacked_sentinel({"a": {"k": m}}, {"a": {"k": None}}, "") is None
    assert mcp._unbacked_sentinel({"a": [m]}, {"a": [None]}, "") is None


def test_tool_permissions_keyed_by_secret_shaped_tool_name_stay_editable(tmp_path: Path) -> None:
    # A tool called `get_token` would act as a secret hint and mask its own "allow"; the
    # validator would then refuse the sentinel and the server could never be saved again.
    stored = {
        "type": "http",
        "url": "https://x/mcp",
        "toolPermissions": {"get_token": "allow", "rotate_secret": "blocked"},
    }
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    servers, h = mcp.read_project_servers(tmp_path)
    assert servers["s"]["toolPermissions"] == stored["toolPermissions"]
    mcp.write_project_servers(tmp_path, servers, h)  # round-trips, no 422


def test_tool_permissions_carve_out_never_unmasks_a_non_permission_value(tmp_path: Path) -> None:
    # Only the three permission words are shown raw. Anything else a hand-edited file holds
    # keeps its redacted form, and an odd (unhashable) value must not crash the read.
    stored = {
        "type": "http",
        "url": "https://x/mcp",
        "toolPermissions": {"get_token": "sk-FAKE4498", "lst": ["x"], "ok": "allow"},
    }
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    servers, _h = mcp.read_project_servers(tmp_path)
    shown = servers["s"]["toolPermissions"]
    assert shown["get_token"] == cw.REDACTION_SENTINEL
    assert shown["ok"] == "allow"
    assert shown["lst"] == ["x"]
    assert "FAKE4498" not in json.dumps(servers)


def test_rollback_restore_writes_a_literal_sentinel_verbatim(tmp_path: Path) -> None:
    # The edit-rollback restore runs after `claude mcp remove`, so nothing is stored under
    # the name. A literal "********" in the on-disk snapshot is data, not a mask: store it
    # verbatim instead of refusing the restore and losing the server.
    prior = {"command": "x", "args": [cw.REDACTION_SENTINEL], "autoApprove": ["t"]}
    mcp.write_project_server_entry(tmp_path, "s", prior, op="edit", validate=False)
    cj = tmp_path / "claude.json"
    mcp.write_user_server_entry(cj, "s", prior, op="edit", validate=False)
    mcp.write_project_local_server_entry(cj, tmp_path, "s", prior, op="edit", validate=False)
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["s"] == prior
    cj_data = json.loads(cj.read_text(encoding="utf-8"))
    assert cj_data["mcpServers"]["s"] == prior
    assert cj_data["projects"][str(tmp_path)]["mcpServers"]["s"] == prior


def test_user_and_local_reads_mask_headers_helper(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    entry = {"type": "ws", "url": "wss://x/s", "headersHelper": "print-token"}
    proj = str(tmp_path / "p")
    cj.write_text(
        json.dumps({"mcpServers": {"u": entry}, "projects": {proj: {"mcpServers": {"l": entry}}}}),
        encoding="utf-8",
    )
    assert mcp.read_user_servers(cj)["u"]["headersHelper"] == cw.REDACTION_SENTINEL
    local = mcp.read_project_local_servers(cj, Path(proj))
    assert local["l"]["headersHelper"] == cw.REDACTION_SENTINEL


def test_headers_helper_is_stored_never_executed(tmp_path: Path) -> None:
    # Validate-never-execute covers the new code-bearing field too.
    marker = tmp_path / "HELPER_RAN"
    script = tmp_path / "helper.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\necho '{{}}'\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    entry = {"type": "http", "url": "https://x/mcp", "headersHelper": str(script)}
    mcp.validate_mcp_servers({"srv": entry})
    mcp.write_project_servers(tmp_path, {"srv": entry}, expected_hash=None)
    mcp.read_project_servers(tmp_path)
    assert not marker.exists(), "validation/write/read executed the headersHelper (RCE!)"


# --- RCE-NEGATIVE: validation must NEVER execute the candidate command -------------


def test_validation_never_executes_command(tmp_path: Path) -> None:
    """A candidate command pointing at a marker-writing script must NOT run on validate.

    Proves the validator is structural-only: if validation resolved/spawned the
    command, the sentinel marker file would appear. It must not.
    """
    marker = tmp_path / "MARKER_SHOULD_NOT_EXIST"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    candidate = {
        "evil": {
            "command": str(script),
            "args": [str(marker)],
            "env": {"OUT": str(marker)},
        }
    }
    # Validating (and writing) must store the command as inert data, never run it.
    mcp.validate_mcp_servers(candidate)
    mcp.write_project_servers(tmp_path, candidate, expected_hash=cw.hash_bytes(b""))

    assert not marker.exists(), "validation/write executed the candidate command (RCE!)"
    # The command landed verbatim as stored data, proving it was treated as inert.
    stored = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert stored["mcpServers"]["evil"]["command"] == str(script)


# --- project read/write round-trip + stale-hash ------------------------------------


def test_project_write_then_read_round_trip(tmp_path: Path) -> None:
    _servers, h0 = mcp.read_project_servers(tmp_path)
    assert _servers == {}
    mcp.write_project_servers(tmp_path, {"srv": {"command": "/bin/foo"}}, expected_hash=h0)
    servers, _h1 = mcp.read_project_servers(tmp_path)
    assert servers == {"srv": {"command": "/bin/foo"}}


def test_secret_shaped_server_name_does_not_break_reads_or_writes(tmp_path: Path) -> None:
    # A server NAME is a user-chosen identifier, not a config key — but `_SECRET_KEY_RE`
    # matches `oauth`, `github-token`, `vault-secrets`, `auth0`, and `_SERVER_NAME_RE`
    # admits all of them. Redacting the whole map at once made such a name mark its own
    # entry secret, masking the structural `type` to the sentinel; `entry_transport` then
    # rejects that as an unknown transport, so EVERY add/edit/remove of EVERY server in the
    # scope 422s — with the value the operator must retype shown as `********`.
    # Redacting per entry keeps the name out of the hint chain.
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "oauth-gw": {
                        "type": "http",
                        "url": "https://example.invalid/",
                        "headers": {"Authorization": "Bearer sk-live-SECRET"},
                    },
                    "plain-fs": {"command": "/bin/fs"},
                }
            }
        ),
        encoding="utf-8",
    )
    servers, h = mcp.read_project_servers(tmp_path)

    # Structural fields survive, so the entry still validates...
    assert servers["oauth-gw"]["type"] == "http"
    assert mcp.entry_transport(servers["oauth-gw"]) == "http"
    # ...while a secret INSIDE the entry is still masked.
    assert servers["oauth-gw"]["headers"]["Authorization"] == cw.REDACTION_SENTINEL
    assert "sk-live-SECRET" not in json.dumps(servers)

    # And an unrelated server can still be edited without the whole scope 422ing.
    mcp.write_project_servers(
        tmp_path, {**servers, "plain-fs": {"command": "/bin/fs2"}}, expected_hash=h
    )
    out = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert out["mcpServers"]["plain-fs"]["command"] == "/bin/fs2"
    # merge_redacted restored the real secret rather than writing the sentinel back.
    assert out["mcpServers"]["oauth-gw"]["headers"]["Authorization"] == "Bearer sk-live-SECRET"


def test_project_write_preserves_sibling_keys(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}, "other": 1}),
        encoding="utf-8",
    )
    _s, h = mcp.read_project_servers(tmp_path)
    mcp.write_project_servers(tmp_path, {"a": {"command": "x"}, "b": {"command": "y"}}, h)
    out = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert out["other"] == 1  # untouched sibling preserved
    assert set(out["mcpServers"]) == {"a", "b"}


def test_project_write_stale_hash_raises(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    stale = cw.hash_bytes(b'{"mcpServers": {"old": {"command": "x"}}}')
    with pytest.raises(cw.StaleConfigWriteError):
        mcp.write_project_servers(tmp_path, {"new": {"command": "y"}}, stale)


def test_project_write_no_hash_on_existing_file_is_stale(tmp_path: Path) -> None:
    # An absent hash is only the first-write path; refusing the unguarded overwrite of
    # an EXISTING file means a client can't drop `hash` to bypass the external-edit guard.
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    with pytest.raises(cw.StaleConfigWriteError):
        mcp.write_project_servers(tmp_path, {"s": {"command": "x"}}, expected_hash=None)


def test_project_write_no_hash_on_absent_file_ok(tmp_path: Path) -> None:
    # The legitimate first write: no file yet ⇒ no hash needed.
    mcp.write_project_servers(tmp_path, {"s": {"command": "x"}}, expected_hash=None)
    out = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert out["mcpServers"] == {"s": {"command": "x"}}


def test_project_read_rejects_non_object_file(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        mcp.read_project_servers(tmp_path)


# --- redaction round-trip (read masks; write keep-stored on sentinel) --------------


def test_project_redaction_and_keep_stored(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": {"command": "x", "env": {"API_TOKEN": "sk-live-real"}}}}),
        encoding="utf-8",
    )
    servers, h = mcp.read_project_servers(tmp_path)
    # Read-back masks the secret — the live value never leaves the reader.
    assert servers["s"]["env"]["API_TOKEN"] == cw.REDACTION_SENTINEL
    assert "sk-live-real" not in json.dumps(servers)

    # Write the masked view back unchanged (e.g. user only tweaked a sibling) — the
    # "********" sentinel keeps the stored secret rather than clobbering it.
    mcp.write_project_servers(tmp_path, servers, h)
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert on_disk["mcpServers"]["s"]["env"]["API_TOKEN"] == "sk-live-real"


# --- user-scope subtree writer (HOME-isolated, explicit tmp file) ------------------


def test_user_write_preserves_other_keys(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    cj.write_text(
        json.dumps({"projects": {"/p": {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    mcp.write_user_servers(cj, {"srv": {"command": "/bin/foo"}})
    out = json.loads(cj.read_text(encoding="utf-8"))
    assert out["mcpServers"]["srv"]["command"] == "/bin/foo"  # subtree written
    assert out["projects"]["/p"]["hasTrustDialogAccepted"] is True  # sibling preserved


def test_user_read_redacts(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    cj.write_text(
        json.dumps({"mcpServers": {"s": {"command": "x", "env": {"TOKEN": "sk-x"}}}}),
        encoding="utf-8",
    )
    servers = mcp.read_user_servers(cj)
    assert servers["s"]["env"]["TOKEN"] == cw.REDACTION_SENTINEL


def test_user_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"  # absent
    with pytest.raises(cw.InvalidCandidateError):
        mcp.write_user_servers(cj, {"srv": {"command": ""}})
    assert not cj.exists()  # nothing written on a validation failure


def test_user_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert mcp.read_user_servers(tmp_path / "absent.json") == {}


def test_read_rejects_malformed_json(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        mcp.read_project_servers(tmp_path)


# --- local-scope writer (~/.claude.json projects[<abs-path>].mcpServers) -----------


def test_local_write_then_read_round_trip(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert mcp.read_project_local_servers(cj, project_dir) == {}
    mcp.write_project_local_servers(cj, project_dir, {"s": {"command": "/bin/x"}})
    assert mcp.read_project_local_servers(cj, project_dir) == {"s": {"command": "/bin/x"}}


def test_local_write_keyed_by_resolved_absolute_project_path(tmp_path: Path) -> None:
    # Same shape trust.py uses for hasTrustDialogAccepted: projects[str(path.resolve())].
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    mcp.write_project_local_servers(cj, project_dir, {"s": {"command": "x"}})
    out = json.loads(cj.read_text(encoding="utf-8"))
    assert str(project_dir.resolve()) in out["projects"]
    assert out["projects"][str(project_dir.resolve())]["mcpServers"] == {"s": {"command": "x"}}


def test_local_write_preserves_other_projects_and_sibling_subtrees(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    proj_a = tmp_path / "a"
    proj_a.mkdir()
    proj_b = tmp_path / "b"
    proj_b.mkdir()
    cj.write_text(
        json.dumps(
            {
                "projects": {
                    str(proj_a.resolve()): {
                        "hasTrustDialogAccepted": True,
                        "mcpServers": {"old": {"command": "x"}},
                    },
                    str(proj_b.resolve()): {"hasTrustDialogAccepted": True},
                },
                "misc": 1,
            }
        ),
        encoding="utf-8",
    )
    mcp.write_project_local_servers(cj, proj_a, {"new": {"command": "y"}})
    out = json.loads(cj.read_text(encoding="utf-8"))
    a_entry = out["projects"][str(proj_a.resolve())]
    assert a_entry["mcpServers"] == {"new": {"command": "y"}}
    assert a_entry["hasTrustDialogAccepted"] is True  # sibling subtree preserved
    assert out["projects"][str(proj_b.resolve())]["hasTrustDialogAccepted"] is True
    assert out["misc"] == 1


def test_local_read_redacts_and_write_keeps_stored_secret(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    mcp.write_project_local_servers(
        cj, project_dir, {"s": {"command": "x", "env": {"API_TOKEN": "sk-live-real"}}}
    )
    servers = mcp.read_project_local_servers(cj, project_dir)
    assert servers["s"]["env"]["API_TOKEN"] == cw.REDACTION_SENTINEL
    assert "sk-live-real" not in json.dumps(servers)
    # Writing the masked view back unchanged keeps the stored secret (keep-stored).
    mcp.write_project_local_servers(cj, project_dir, servers)
    out = json.loads(cj.read_text(encoding="utf-8"))
    assert (
        out["projects"][str(project_dir.resolve())]["mcpServers"]["s"]["env"]["API_TOKEN"]
        == "sk-live-real"
    )


def test_local_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"  # absent
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    with pytest.raises(cw.InvalidCandidateError):
        mcp.write_project_local_servers(cj, project_dir, {"s": {"command": ""}})
    assert not cj.exists()  # nothing written on a validation failure


def test_local_read_missing_file_is_empty(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert mcp.read_project_local_servers(tmp_path / "absent.json", project_dir) == {}


def test_local_scope_independent_of_project_and_user_scope(tmp_path: Path) -> None:
    # Local scope lives in ~/.claude.json's per-project block; it must never collide
    # with the project-scope .mcp.json file or the flat user-scope mcpServers subtree.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    mcp.write_project_servers(project_dir, {"p": {"command": "proj"}}, expected_hash=None)
    mcp.write_user_servers(cj, {"u": {"command": "user"}})
    mcp.write_project_local_servers(cj, project_dir, {"l": {"command": "local"}})

    project_servers, _ = mcp.read_project_servers(project_dir)
    assert project_servers == {"p": {"command": "proj"}}
    assert mcp.read_user_servers(cj) == {"u": {"command": "user"}}
    assert mcp.read_project_local_servers(cj, project_dir) == {"l": {"command": "local"}}


# --- #837: unapproved_mcp_servers (pre-spawn MCP-approval preflight) ---------------


def test_unapproved_no_mcp_json_is_empty(tmp_path: Path) -> None:
    # No .mcp.json at all -> nothing to warn about.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_empty_servers_map_is_empty(tmp_path: Path) -> None:
    # An .mcp.json present but with no mcpServers key/empty object -> nothing to warn.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_all_approved_is_empty(tmp_path: Path) -> None:
    # Every committed server is either enabled or disabled -> nothing left to warn.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}),
        encoding="utf-8",
    )
    mcp.write_project_approvals(cj, project_dir.resolve(), ["a"], ["b"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_some_unapproved_lists_the_set(tmp_path: Path) -> None:
    # "a" approved, "b" rejected, "c" never decided -> only "c" is unapproved.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "a": {"command": "x"},
                    "b": {"command": "y"},
                    "c": {"command": "z"},
                }
            }
        ),
        encoding="utf-8",
    )
    mcp.write_project_approvals(cj, project_dir.resolve(), ["a"], ["b"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == ["c"]


def test_unapproved_no_approvals_file_lists_every_server(tmp_path: Path) -> None:
    # A committed .mcp.json with no ~/.claude.json approvals at all -> every server
    # is unapproved (this is the exact scenario #837 hangs on: a fresh clone with a
    # committed .mcp.json and no prior approval decision).
    cj = tmp_path / "claude.json"  # never created
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}),
        encoding="utf-8",
    )
    assert mcp.unapproved_mcp_servers(cj, project_dir) == ["a", "b"]


def test_unapproved_malformed_mcp_json_is_safe_empty(tmp_path: Path, caplog) -> None:
    # Malformed .mcp.json ("cannot determine") must degrade to [] and NEVER raise —
    # a preflight read failure must not crash (or block) the launch path.
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text("{not valid json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert mcp.unapproved_mcp_servers(cj, project_dir) == []
    assert any("could not read" in rec.message for rec in caplog.records)


def test_unapproved_non_object_mcp_json_is_safe_empty(tmp_path: Path) -> None:
    # Valid JSON but not an object (e.g. a bare list) -> also "cannot determine".
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text("[]", encoding="utf-8")
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_malformed_claude_json_is_safe_empty(tmp_path: Path, caplog) -> None:
    # .mcp.json is fine, but ~/.claude.json (the approvals store) is corrupt -> still
    # "cannot determine", never a crash. Distinct from the .mcp.json failure above:
    # this exercises the second try/except around the approvals read.
    cj = tmp_path / "claude.json"
    cj.write_text("{not valid json", encoding="utf-8")
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8"
    )
    with caplog.at_level("WARNING"):
        assert mcp.unapproved_mcp_servers(cj, project_dir) == []
    assert any("could not read MCP approvals" in rec.message for rec in caplog.records)


def test_unapproved_resolves_project_dir_for_approvals_lookup(tmp_path: Path) -> None:
    # Project.path from discovery is not guaranteed resolved (e.g. a symlinked
    # projects_root); approvals are keyed by the RESOLVED path (mirrors
    # resolve_project_dir), so a caller passing an unresolved-but-equivalent path
    # must still see the approval. Simulate via a "./" indirection.
    cj = tmp_path / "claude.json"
    real_dir = tmp_path / "proj"
    real_dir.mkdir()
    (real_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8"
    )
    mcp.write_project_approvals(cj, real_dir.resolve(), ["a"], [])
    unresolved = tmp_path / "." / "proj"
    assert mcp.unapproved_mcp_servers(cj, unresolved) == []


# --- #850: settings-file decisions also count as "decided" (no false warn) ----------


def _mcp_project(tmp_path: Path, *servers: str) -> tuple[Path, Path]:
    """Make a project with a committed .mcp.json listing `servers`; return (claude_json, dir)."""
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {s: {"command": "x"} for s in servers}}), encoding="utf-8"
    )
    return cj, project_dir


def _write_settings(path: Path, **keys) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys), encoding="utf-8")


def test_unapproved_settings_local_disable_counts_as_decided(tmp_path: Path) -> None:
    # #850 repro: a server DISABLED in .claude/settings.local.json is decided as far as
    # claude is concerned (no enable gate), so the preflight must NOT warn about it.
    cj, project_dir = _mcp_project(tmp_path, "demo-fs", "demo-http")
    _write_settings(
        project_dir / ".claude" / "settings.local.json",
        disabledMcpjsonServers=["demo-fs", "demo-http"],
    )
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_project_settings_enable_counts_as_decided(tmp_path: Path) -> None:
    # An ENABLE in the project-shared .claude/settings.json likewise clears the gate.
    cj, project_dir = _mcp_project(tmp_path, "a", "b")
    _write_settings(project_dir / ".claude" / "settings.json", enabledMcpjsonServers=["a", "b"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_user_settings_decision_counts_as_decided(tmp_path: Path) -> None:
    # The user-scope ~/.claude/settings.json (beside ~/.claude.json) is honored too.
    cj, project_dir = _mcp_project(tmp_path, "a")
    _write_settings(cj.parent / ".claude" / "settings.json", enabledMcpjsonServers=["a"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == []


def test_unapproved_merges_every_source(tmp_path: Path) -> None:
    # 'a' decided in ~/.claude.json, 'b' via settings.local.json, 'c' never decided
    # anywhere -> only 'c' still needs approval.
    cj, project_dir = _mcp_project(tmp_path, "a", "b", "c")
    mcp.write_project_approvals(cj, project_dir.resolve(), ["a"], [])
    _write_settings(project_dir / ".claude" / "settings.local.json", disabledMcpjsonServers=["b"])
    assert mcp.unapproved_mcp_servers(cj, project_dir) == ["c"]


def test_unapproved_malformed_settings_file_is_ignored(tmp_path: Path) -> None:
    # A corrupt settings file must not crash the preflight AND must not silently hide a
    # genuinely-undecided server — it simply contributes no decisions.
    cj, project_dir = _mcp_project(tmp_path, "a")
    (project_dir / ".claude").mkdir(parents=True)
    (project_dir / ".claude" / "settings.local.json").write_text("{not json", encoding="utf-8")
    assert mcp.unapproved_mcp_servers(cj, project_dir) == ["a"]


# --- #958 P2: read_project_approvals folds in settings-file top-level approvals -----


def _approvals_project(tmp_path: Path) -> tuple[Path, Path]:
    """Return (claude_json, project_dir) for a read_project_approvals fixture."""
    cj = tmp_path / "claude.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    return cj, project_dir


def test_read_approvals_settings_local_enable_shows_enabled(tmp_path: Path) -> None:
    # DF-9: `claude mcp add-json --scope local` relocates the approval into
    # settings.local.json and clears ~/.claude.json — the panel must still show it enabled.
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["fs"],
        "disabled": [],
        "locked": ["fs"],
    }


def test_read_approvals_project_settings_disable_shows_disabled(tmp_path: Path) -> None:
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(project_dir / ".claude" / "settings.json", disabledMcpjsonServers=["http"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": [],
        "disabled": ["http"],
        "locked": ["http"],
    }


def test_read_approvals_user_settings_folded(tmp_path: Path) -> None:
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(cj.parent / ".claude" / "settings.json", enabledMcpjsonServers=["u"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["u"],
        "disabled": [],
        "locked": ["u"],
    }


def test_read_approvals_settings_local_overrides_claude_json(tmp_path: Path) -> None:
    # Conflict across sources: enabled in ~/.claude.json but disabled in settings.local.json
    # — the more-specific (local) source wins, matching what claude loads.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["z"], [])
    _write_settings(project_dir / ".claude" / "settings.local.json", disabledMcpjsonServers=["z"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": [],
        "disabled": ["z"],
        "locked": ["z"],
    }


def test_read_approvals_project_settings_overrides_user_settings(tmp_path: Path) -> None:
    # Settings-vs-settings precedence (not just claude.json-vs-local): user settings.json
    # disables X, project settings.json enables X — project is more specific, so X reads
    # as enabled. Pins the ~/.claude.json < user < project < local ordering across the
    # settings files themselves.
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(cj.parent / ".claude" / "settings.json", disabledMcpjsonServers=["x"])
    _write_settings(project_dir / ".claude" / "settings.json", enabledMcpjsonServers=["x"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["x"],
        "disabled": [],
        "locked": ["x"],
    }


def test_read_approvals_local_settings_overrides_project_settings(tmp_path: Path) -> None:
    # Local is the most specific settings file: project settings.json enables X, local
    # settings.local.json disables X — local wins, X reads as disabled.
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(project_dir / ".claude" / "settings.json", enabledMcpjsonServers=["x"])
    _write_settings(project_dir / ".claude" / "settings.local.json", disabledMcpjsonServers=["x"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": [],
        "disabled": ["x"],
        "locked": ["x"],
    }


def test_read_approvals_claude_json_only_unchanged(tmp_path: Path) -> None:
    # No settings files -> behaves exactly as the legacy ~/.claude.json-only read.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["a"], ["b"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["a"],
        "disabled": ["b"],
        "locked": [],
    }


def test_read_approvals_malformed_settings_ignored(tmp_path: Path) -> None:
    # A corrupt settings file contributes nothing — the ~/.claude.json state still reads.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["a"], [])
    (project_dir / ".claude" / "settings.local.json").parent.mkdir(parents=True, exist_ok=True)
    (project_dir / ".claude" / "settings.local.json").write_text("{bad", encoding="utf-8")
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["a"],
        "disabled": [],
        "locked": [],
    }


def test_read_approvals_locked_even_when_claude_json_agrees(tmp_path: Path) -> None:
    # A server enabled in BOTH ~/.claude.json AND settings.local.json is still `locked`:
    # the settings entry owns the effective decision, so a panel Unset (which only rewrites
    # ~/.claude.json) would be reverted on reload. The panel renders such a row read-only
    # rather than offer an action that silently won't take (#958 P2 write-asymmetry guard).
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["fs"], [])
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["fs"],
        "disabled": [],
        "locked": ["fs"],
    }


def _base_lists(cj: Path, project_dir: Path) -> dict[str, list[str]]:
    """Return the raw ~/.claude.json projects[] approval lists (no settings overlay)."""
    proj = json.loads(cj.read_text(encoding="utf-8"))["projects"][str(project_dir)]
    return {
        "enabled": proj.get("enabledMcpjsonServers", []),
        "disabled": proj.get("disabledMcpjsonServers", []),
    }


def test_write_approvals_does_not_copy_settings_owned_into_claude_json(tmp_path: Path) -> None:
    # Greptile P1: the panel echoes the merged (settings-folded) lists back on any save.
    # Persisting `fs` (owned only by settings.local.json) into ~/.claude.json would create a
    # phantom base approval that lingers if the settings entry is later removed. The writer
    # must drop settings-owned names and persist ONLY the decisions this path owns.
    cj, project_dir = _approvals_project(tmp_path)
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    mcp.write_project_approvals(cj, project_dir, ["fs", "other"], [])
    assert _base_lists(cj, project_dir) == {"enabled": ["other"], "disabled": []}
    # The display read still reflects fs (from settings) + the new `other`.
    got = mcp.read_project_approvals(cj, project_dir)
    assert set(got["enabled"]) == {"fs", "other"} and got["locked"] == ["fs"]


def test_write_approvals_preserves_original_base_value_for_settings_owned(tmp_path: Path) -> None:
    # A name that is a GENUINE ~/.claude.json approval AND is later also settings-owned keeps
    # its original base value on a subsequent save — the writer neither drops the real base
    # decision nor overwrites it with the settings-derived one.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["fs"], [])  # genuine base approval first
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    mcp.write_project_approvals(cj, project_dir, ["fs", "other"], [])  # merged echo + a new one
    assert _base_lists(cj, project_dir) == {"enabled": ["other", "fs"], "disabled": []}


def test_write_approvals_drops_contradictory_base_pair_for_settings_owned(tmp_path: Path) -> None:
    # Defensive: a hand-corrupted base listing a settings-owned name in BOTH enabled and
    # disabled must not be persisted as a self-contradicting pair. The name is dropped from
    # both (a settings file owns it, so the inert base entry is meaningless) — the writer's
    # own output stays a clean set even from a corrupt base.
    cj, project_dir = _approvals_project(tmp_path)
    cj.write_text(
        json.dumps(
            {
                "projects": {
                    str(project_dir): {
                        "enabledMcpjsonServers": ["fs"],
                        "disabledMcpjsonServers": ["fs"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    mcp.write_project_approvals(cj, project_dir, ["fs", "other"], [])
    assert _base_lists(cj, project_dir) == {"enabled": ["other"], "disabled": []}


def test_write_approvals_fails_closed_on_unreadable_settings(tmp_path: Path) -> None:
    # Fail closed: if a settings file is present but unreadable/malformed at WRITE time (e.g.
    # corrupted between the panel's read and this PUT), ownership can't be verified — the
    # write must abort rather than treat a settings-owned decision as panel-owned and copy it
    # into ~/.claude.json. The READ path stays lenient (that case is covered separately).
    cj, project_dir = _approvals_project(tmp_path)
    broken = project_dir / ".claude" / "settings.local.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{bad json", encoding="utf-8")
    with pytest.raises(cw.ConfigWriteError):
        mcp.write_project_approvals(cj, project_dir, ["other"], [])
    # Nothing was written — no partial ~/.claude.json project entry.
    assert not cj.exists() or "projects" not in json.loads(cj.read_text(encoding="utf-8"))


def test_write_approvals_fails_closed_on_unreadable_settings_dir(tmp_path: Path) -> None:
    # A settings PATH that is a directory (read_bytes -> OSError, not a malformed JSON body)
    # also fails the strict write closed: ownership still can't be verified.
    cj, project_dir = _approvals_project(tmp_path)
    (project_dir / ".claude" / "settings.local.json").mkdir(parents=True)
    with pytest.raises(cw.ConfigWriteError):
        mcp.write_project_approvals(cj, project_dir, ["other"], [])


def test_read_approvals_unreadable_settings_dir_ignored(tmp_path: Path) -> None:
    # The lenient read path tolerates the same OSError (a directory at the settings path):
    # it contributes nothing rather than crashing the panel, so the base approval still reads.
    cj, project_dir = _approvals_project(tmp_path)
    mcp.write_project_approvals(cj, project_dir, ["a"], [])
    (project_dir / ".claude" / "settings.local.json").mkdir(parents=True)
    assert mcp.read_project_approvals(cj, project_dir) == {
        "enabled": ["a"],
        "disabled": [],
        "locked": [],
    }


def test_write_approvals_dedupes_preserved_base_duplicates(tmp_path: Path) -> None:
    # Defensive: a hand-edited ~/.claude.json with a duplicate owned name must not survive as
    # a duplicate when its base value is preserved across a save (the incoming lists are
    # already validated dup-free, so only a tampered base can reach the dedup path).
    cj, project_dir = _approvals_project(tmp_path)
    cj.write_text(
        json.dumps({"projects": {str(project_dir): {"enabledMcpjsonServers": ["fs", "fs"]}}}),
        encoding="utf-8",
    )
    _write_settings(project_dir / ".claude" / "settings.local.json", enabledMcpjsonServers=["fs"])
    mcp.write_project_approvals(cj, project_dir, ["fs", "other"], [])
    assert _base_lists(cj, project_dir) == {"enabled": ["other", "fs"], "disabled": []}


# --- gated routes (full FastAPI lifespan) ------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path, extra: str) -> TestClient:
    # The autouse HOME-isolation fixture already pins HOME to an isolated tmp dir, so
    # runner.claude_json resolves to <isolated-home>/.claude.json — the real account is
    # never touched by the user-scope routes.
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"


def test_route_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/mcp?project=alpha").status_code == 404
        assert (
            c.put(
                "/api/config-write/mcp",
                json={"scope": "project", "project": "alpha", "confirm": "alpha", "servers": {}},
            ).status_code
            == 404
        )


def test_route_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get("/api/config-write/mcp?scope=user").status_code == 404
        assert (
            c.put(
                "/api/config-write/mcp",
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "servers": {}},
            ).status_code
            == 404
        )


def test_route_confirm_mismatch_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "servers": {"s": {"command": "x"}},
            },
        )
        assert resp.status_code == 400


def test_route_bad_shape_is_422_and_writes_nothing(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x", "bogus": 1}},  # unknown key
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".mcp.json").exists()  # nothing written


def test_route_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "servers": {},
            },
        )
        assert resp.status_code == 400
        assert c.get("/api/config-write/mcp?project=../escape").status_code == 400


def test_route_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    (projects_root / "alpha" / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x"}},
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_no_hash_on_existing_file_is_409(write_config, tmp_path, projects_root) -> None:
    (projects_root / "alpha" / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x"}},
                # no "hash" — must not silently overwrite an existing file
            },
        )
        assert resp.status_code == 409


def test_route_project_write_read_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get("/api/config-write/mcp?project=alpha")
        assert read0.status_code == 200
        h0 = read0.json()["hash"]
        wr = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"srv": {"command": "/bin/foo", "args": ["--x"]}},
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read1 = c.get("/api/config-write/mcp?project=alpha")
        assert read1.json()["servers"] == {"srv": {"command": "/bin/foo", "args": ["--x"]}}


def test_route_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?scope=bogus").status_code == 422
        assert (
            c.put(
                "/api/config-write/mcp",
                json={"scope": "bogus", "servers": {}},
            ).status_code
            == 422
        )


def test_route_user_write_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        wr = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "servers": {"u": {"command": "node"}},
            },
        )
        assert wr.status_code == 200
        read = c.get("/api/config-write/mcp?scope=user")
        assert read.json()["servers"] == {"u": {"command": "node"}}
    # The write landed in the ISOLATED home (autouse fixture), never the real account.
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    assert out["mcpServers"]["u"]["command"] == "node"


# --- regression: the route-level error guards (P1/P2 from review) -------------------


def test_route_read_corrupt_file_is_422(write_config, tmp_path, projects_root) -> None:
    # A hand-edited / partially-written .mcp.json that is not valid JSON must surface
    # as a clean 422 from the GET route, never an unhandled 500 (P1). read_project_servers
    # -> _load_json_obj raises InvalidCandidateError; the route maps it through the helper.
    (projects_root / "alpha" / ".mcp.json").write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get("/api/config-write/mcp?project=alpha")
        assert resp.status_code == 422


def test_route_read_non_utf8_file_is_422(write_config, tmp_path, projects_root) -> None:
    # A non-UTF-8 .mcp.json: _load_json_obj's raw.decode("utf-8") raises UnicodeDecodeError,
    # which is wrapped as InvalidCandidateError so the GET route reports 422, not a 500.
    (projects_root / "alpha" / ".mcp.json").write_bytes(b"\xff\xfe not utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?project=alpha").status_code == 422


def test_route_read_user_corrupt_file_is_422(write_config, tmp_path) -> None:
    # User-scope complement of the project read guard (P1): a corrupt ~/.claude.json must
    # surface as a clean 422 from the GET route, never an unhandled 500. read_user_servers
    # -> _load_json_obj raises InvalidCandidateError; the route maps it through the helper.
    (Path(os.environ["HOME"]) / ".claude.json").write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?scope=user").status_code == 422


def test_route_write_over_non_utf8_file_is_422(write_config, tmp_path, projects_root) -> None:
    # The write-side complement: a non-UTF-8 existing file is read under the lock in
    # _mutate; the UnicodeDecodeError must surface as a clean 422, never escape the
    # ConfigWriteError guard as a 500. (A valid hash can't be formed for unreadable bytes,
    # but the decode failure is reached before the hash check via the stale-hash path.)
    (projects_root / "alpha" / ".mcp.json").write_bytes(b"\xff\xfe not utf-8")
    file_hash = hashlib.sha256(b"\xff\xfe not utf-8").hexdigest()
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x"}},
                "hash": file_hash,
            },
        )
        assert resp.status_code == 422


def test_route_read_non_object_file_is_422(write_config, tmp_path, projects_root) -> None:
    # Valid JSON whose root is not an object (e.g. an array) is likewise a 422 on read,
    # not a 500 — we refuse to interpret a file we could not parse as a server map.
    (projects_root / "alpha" / ".mcp.json").write_text("[1, 2, 3]", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?project=alpha").status_code == 422


def test_route_write_missing_project_dir_is_404(write_config, tmp_path, projects_root) -> None:
    # A contained-but-absent project dir on the first-write path used to reach
    # mkstemp(dir=path.parent) and raise FileNotFoundError outside the ConfigWriteError
    # guard -> unhandled 500 (P2). It must now be a clean 404, and nothing is written.
    absent = projects_root / "noexist"
    assert not absent.exists()
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "servers": {"s": {"command": "x"}},
            },
        )
        assert resp.status_code == 404
    assert not (absent / ".mcp.json").exists()  # nothing written


def test_route_confirm_runs_before_validate(write_config, tmp_path, projects_root) -> None:
    # P2 ordering: the type-the-name confirm is the FIRST semantic gate after capability.
    # A request that BOTH omits a valid confirm AND carries a malformed `servers` must
    # fail at the confirm gate (400), never reach the structural validator (422).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "servers": "not-a-dict",  # malformed shape (would 422 if reached)
            },
        )
        assert resp.status_code == 400
    # User scope: same ordering — bad confirm short-circuits before the shape check.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={"scope": "user", "confirm": "WRONG", "servers": 123},
        )
        assert resp.status_code == 400


def test_route_missing_servers_is_422_after_valid_confirm(
    write_config, tmp_path, projects_root
) -> None:
    # With a VALID confirm token, a request omitting `servers` reaches the shape check
    # and is a 422 — confirms the reorder did not drop the structural guard.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={"scope": "project", "project": "alpha", "confirm": "alpha"},
        )
        assert resp.status_code == 422


def test_route_read_empty_project_is_422(write_config, tmp_path, projects_root) -> None:
    # GET with no/empty project name fails the project-string guard (422) before any I/O.
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?project=").status_code == 422


def test_route_user_missing_servers_is_422_after_valid_confirm(write_config, tmp_path) -> None:
    # User scope, valid confirm, no `servers` -> the user-branch shape check 422s.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN},
        )
        assert resp.status_code == 422


def test_route_user_invalid_entry_is_422(write_config, tmp_path) -> None:
    # User scope, valid confirm + dict `servers` but a malformed entry: the structural
    # validator raises InvalidCandidateError, mapped through _map_config_write_error -> 422.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "servers": {"s": {"command": "x", "bogus": 1}},  # unknown key
            },
        )
        assert resp.status_code == 422


def test_route_project_non_string_hash_is_422(write_config, tmp_path, projects_root) -> None:
    # A non-string `hash` (when present) is a 422 before any write.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "servers": {"s": {"command": "x"}},
                "hash": 123,  # not a string
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".mcp.json").exists()


def test_route_project_missing_name_is_400(write_config, tmp_path, projects_root) -> None:
    # Project scope with no `project` name: confirm runs first and a project-scope write
    # with no resolvable token fails closed at 400 (never reaches resolve/validate).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={"scope": "project", "confirm": "", "servers": {"s": {"command": "x"}}},
        )
        assert resp.status_code == 400


# --- local scope routes (own confirm token, project-only gate, no gitignore) -------


def test_route_local_scope_works_without_allow_user_scope(
    write_config, tmp_path, projects_root
) -> None:
    # Local scope needs no allow_user_scope opt-in — the base `enabled` flag alone
    # gates it, same as project scope (#766 scope decision).
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        read0 = c.get("/api/config-write/mcp?scope=local&project=alpha")
        assert read0.status_code == 200
        assert read0.json() == {
            "scope": "local",
            "project": "alpha",
            "servers": {},
            "hash": None,
            "conversions": [],
        }
        wr = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "servers": {"srv": {"command": "/bin/foo"}},
            },
        )
        assert wr.status_code == 200
        read1 = c.get("/api/config-write/mcp?scope=local&project=alpha")
        assert read1.json()["servers"] == {"srv": {"command": "/bin/foo"}}
    # The write landed in the ISOLATED home (autouse fixture), never the real account,
    # keyed by the resolved absolute project path — never the project-scope .mcp.json.
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    resolved_project = str((projects_root / "alpha").resolve())
    assert out["projects"][resolved_project]["mcpServers"]["srv"]["command"] == "/bin/foo"
    assert not (projects_root / "alpha" / ".mcp.json").exists()


def test_route_local_confirm_rejects_plain_project_name(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha",  # the PROJECT-scope token, not the local one
                "servers": {"srv": {"command": "/bin/foo"}},
            },
        )
        assert resp.status_code == 400


def test_route_local_scope_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/mcp?scope=local&project=alpha").status_code == 404
        assert (
            c.put(
                "/api/config-write/mcp",
                json={
                    "scope": "local",
                    "project": "alpha",
                    "confirm": "alpha (local)",
                    "servers": {},
                },
            ).status_code
            == 404
        )


def test_route_local_write_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "noexist",
                "confirm": "noexist (local)",
                "servers": {"s": {"command": "x"}},
            },
        )
        assert resp.status_code == 404


def test_route_local_write_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "../escape",
                "confirm": "../escape (local)",
                "servers": {},
            },
        )
        assert resp.status_code == 400
        assert c.get("/api/config-write/mcp?scope=local&project=../escape").status_code == 400


def test_route_local_write_bad_shape_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "servers": {"s": {"command": "x", "bogus": 1}},
            },
        )
        assert resp.status_code == 422
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    assert not isolated.exists()  # nothing written on a validation failure


def test_route_local_write_no_hash_forwarded(write_config, tmp_path, projects_root) -> None:
    # MCP local scope owns its own locking (nested subtree merge) — a client-supplied
    # "hash" must be silently ignored, never forwarded/enforced (mirrors user scope).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "servers": {"s": {"command": "x"}},
                "hash": "bogus-hash-value",
            },
        )
        assert resp.status_code == 200


def test_route_local_read_corrupt_claude_json_is_422(
    write_config, tmp_path, projects_root
) -> None:
    # A hand-edited/corrupt ~/.claude.json must surface as a clean 422 from the local-
    # scope GET route too, never an unhandled 500 (same guard as the user-scope read).
    (Path(os.environ["HOME"]) / ".claude.json").write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get("/api/config-write/mcp?scope=local&project=alpha").status_code == 422


# --- #1600: a ~/.claude.json that does not parse is never rewritten -------------------

_TRUNCATED_CJ = b'{"projects": {"/keep": {"hasTrustDialogAccepted": true}}, "oauthAcc'


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda cj, d: mcp.write_user_servers(cj, {"s": {"command": "x"}}), id="user"),
        pytest.param(
            lambda cj, d: mcp.write_project_local_servers(cj, d, {"s": {"command": "x"}}),
            id="local",
        ),
        pytest.param(lambda cj, d: mcp.write_project_approvals(cj, d, ["s"], []), id="approvals"),
    ],
)
def test_claude_json_writers_refuse_an_unparseable_file(tmp_path: Path, write) -> None:
    # Each writer reaches the shared transaction without reading the file first. It used to
    # parse the truncated file as {} and replace it with only its own subtree.
    cj, project_dir = _approvals_project(tmp_path)
    cj.write_bytes(_TRUNCATED_CJ)
    with pytest.raises(cw.InvalidCandidateError, match="not a valid JSON object"):
        write(cj, project_dir)
    assert cj.read_bytes() == _TRUNCATED_CJ


def test_route_user_write_unparseable_claude_json_is_422_and_unchanged(
    write_config, tmp_path
) -> None:
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    isolated.write_bytes(_TRUNCATED_CJ)
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            "/api/config-write/mcp",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "servers": {"s": {"command": "x"}},
            },
        )
    assert resp.status_code == 422, resp.text
    assert "Clauster did not change it" in resp.json()["detail"]
    assert isolated.read_bytes() == _TRUNCATED_CJ


# --- TASK-449.8: convert on LOAD the stored fields Claude Code ignores --------------
# Earlier clauster versions accepted `transport` and a remote `env`. The readers convert
# them in the display copy, the read names each conversion, and the next save fixes the
# file. A write that still carries them is refused with a message that says so.


@pytest.mark.parametrize(
    ("stored", "shown", "fields"),
    [
        pytest.param(
            {"transport": "sse", "url": "https://x/sse"},
            {"type": "sse", "url": "https://x/sse"},
            ["transport"],
            id="transport-becomes-type",
        ),
        pytest.param(
            {"type": "http", "transport": "sse", "url": "https://x/mcp"},
            {"type": "http", "url": "https://x/mcp"},
            ["transport"],
            id="transport-dropped-when-type-set",
        ),
        pytest.param(
            {"type": "sse", "url": "https://x/sse", "env": {"A": "a", "B": "b"}},
            {"type": "sse", "url": "https://x/sse"},
            ["env"],
            id="remote-env-dropped",
        ),
        pytest.param(
            {"transport": "ws", "url": "wss://x/s", "env": {"A": "a"}},
            {"type": "ws", "url": "wss://x/s"},
            ["transport", "env"],
            id="both",
        ),
        pytest.param(
            {"command": "x", "env": {"A": "a"}},
            {"command": "x", "env": {"A": cw.REDACTION_SENTINEL}},
            [],
            id="stdio-env-kept",
        ),
        pytest.param(
            # Not a plain transport word: never moved into `type`; the validator refuses it.
            {"transport": "${T}", "url": "https://x/mcp"},
            {"transport": cw.REDACTION_SENTINEL, "url": "https://x/mcp"},
            [],
            id="odd-transport-left-alone",
        ),
    ],
)
def test_reader_converts_ignored_fields_on_load(
    tmp_path: Path, stored: dict, shown: dict, fields: list[str]
) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    before = (tmp_path / ".mcp.json").read_bytes()
    servers, _h = mcp.read_project_servers(tmp_path)
    assert servers["s"] == shown
    notes = mcp.read_conversion_notes("project", claude_json=tmp_path / "cj", project_dir=tmp_path)
    assert [n["field"] for n in notes] == fields
    assert all(n["name"] == "s" for n in notes)
    assert (tmp_path / ".mcp.json").read_bytes() == before  # a read never writes


def test_conversion_note_counts_masked_env_and_carries_no_value(tmp_path: Path) -> None:
    stored = {"type": "http", "url": "https://x/mcp", "env": {"TOKEN": "sk-FAKE4498", "B": "b"}}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    (note,) = mcp.read_conversion_notes(
        "project", claude_json=tmp_path / "cj", project_dir=tmp_path
    )
    assert note["field"] == "env"
    assert "2 value(s)" in note["message"]
    assert "never read" in note["message"]
    assert "FAKE4498" not in json.dumps(note)


def test_conversion_notes_for_user_and_local_scope(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    proj = tmp_path / "p"
    cj.write_text(
        json.dumps(
            {
                "mcpServers": {"u": {"transport": "http", "url": "https://x/mcp"}},
                "projects": {
                    str(proj): {
                        "mcpServers": {"l": {"type": "sse", "url": "u", "env": {"K": "v"}}}
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    user = mcp.read_conversion_notes("user", claude_json=cj, project_dir=proj)
    local = mcp.read_conversion_notes("local", claude_json=cj, project_dir=proj)
    assert [(n["name"], n["field"]) for n in user] == [("u", "transport")]
    assert [(n["name"], n["field"]) for n in local] == [("l", "env")]
    assert mcp.read_user_servers(cj)["u"] == {"type": "http", "url": "https://x/mcp"}
    assert mcp.read_project_local_servers(cj, proj)["l"] == {"type": "sse", "url": "u"}
    # No file at all: no notes, no error.
    assert mcp.read_conversion_notes("user", claude_json=tmp_path / "none", project_dir=proj) == []
    assert mcp.read_conversion_notes("project", claude_json=cj, project_dir=tmp_path / "x") == []


def test_saving_the_converted_view_fixes_the_file(tmp_path: Path) -> None:
    # The whole-map save of the display copy writes the conversion out, while a masked
    # secret elsewhere in the entry is still restored from the stored value.
    stored = {
        "legacy": {
            "transport": "sse",
            "url": "https://x/sse",
            "headers": {"Authorization": "Bearer sk-FAKE4498"},
            "env": {"OLD": "never-read"},
        },
        "fine": {"command": "x", "env": {"K": "v"}},
    }
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": stored}), encoding="utf-8")
    servers, h = mcp.read_project_servers(tmp_path)
    mcp.write_project_servers(tmp_path, servers, h)
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert on_disk["legacy"] == {
        "type": "sse",
        "url": "https://x/sse",
        "headers": {"Authorization": "Bearer sk-FAKE4498"},
    }
    assert on_disk["fine"] == {"command": "x", "env": {"K": "v"}}
    assert (
        mcp.read_conversion_notes("project", claude_json=tmp_path / "cj", project_dir=tmp_path)
        == []
    )


def test_saving_one_converted_entry_fixes_it_and_leaves_siblings(tmp_path: Path) -> None:
    stored = {
        "a": {"type": "http", "transport": "http", "url": "https://x/mcp", "env": {"K": "v"}},
        "b": {"transport": "sse", "url": "https://y/sse"},
    }
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": stored}), encoding="utf-8")
    servers, _h = mcp.read_project_servers(tmp_path)
    mcp.write_project_server_entry(tmp_path, "a", servers["a"], op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert on_disk["a"] == {"type": "http", "url": "https://x/mcp"}
    assert on_disk["b"] == stored["b"]  # an untouched sibling is not rewritten


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"transport": "sse", "url": "u"}, "converts a stored 'transport'"),
        ({"type": "http", "transport": "x", "url": "u"}, "converts a stored 'transport'"),
        ({"type": "sse", "url": "u", "env": {"K": "v"}}, "drops a stored remote 'env'"),
        # Never converted on load, so the message must not promise a reload fix.
        ({"transport": "${T}", "url": "u"}, "set 'type' by hand"),
        ({"transport": 5, "url": "u"}, "set 'type' by hand"),
        ({"transport": ["sse"], "url": "u"}, "set 'type' by hand"),  # unhashable: no 500
    ],
)
def test_a_write_still_carrying_ignored_fields_explains_the_conversion(
    entry: dict, match: str
) -> None:
    with pytest.raises(cw.InvalidCandidateError, match=match):
        mcp.validate_mcp_servers({"s": entry})


def test_saving_converted_entries_fixes_user_and_local_scope(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    proj = tmp_path / "p"
    legacy = {"transport": "http", "url": "https://x/mcp", "env": {"K": "v"}}
    cj.write_text(
        json.dumps(
            {"mcpServers": {"u": legacy}, "projects": {str(proj): {"mcpServers": {"l": legacy}}}}
        ),
        encoding="utf-8",
    )
    user = mcp.read_user_servers(cj)
    mcp.write_user_server_entry(cj, "u", user["u"], op="edit")
    local = mcp.read_project_local_servers(cj, proj)
    mcp.write_project_local_server_entry(cj, proj, "l", local["l"], op="edit")
    data = json.loads(cj.read_text(encoding="utf-8"))
    fixed = {"type": "http", "url": "https://x/mcp"}
    assert data["mcpServers"]["u"] == fixed
    assert data["projects"][str(proj)]["mcpServers"]["l"] == fixed


def test_route_read_returns_conversion_notes(write_config, tmp_path, projects_root) -> None:
    (projects_root / "alpha" / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": {"transport": "sse", "url": "https://x/sse"}}}),
        encoding="utf-8",
    )
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    isolated.write_text(
        json.dumps({"mcpServers": {"u": {"type": "ws", "url": "wss://x", "env": {"K": "v"}}}}),
        encoding="utf-8",
    )
    with _client(write_config, tmp_path, _ON) as c:
        project = c.get("/api/config-write/mcp", params={"scope": "project", "project": "alpha"})
        user = c.get("/api/config-write/mcp", params={"scope": "user"})
        local = c.get("/api/config-write/mcp", params={"scope": "local", "project": "alpha"})
    assert project.json()["servers"]["s"] == {"type": "sse", "url": "https://x/sse"}
    assert [(n["name"], n["field"]) for n in project.json()["conversions"]] == [("s", "transport")]
    assert [(n["name"], n["field"]) for n in user.json()["conversions"]] == [("u", "env")]
    assert local.json()["conversions"] == []


def test_route_read_returns_local_scope_conversion_notes(
    write_config, tmp_path, projects_root
) -> None:
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    key = str((projects_root / "alpha").resolve())
    isolated.write_text(
        json.dumps(
            {"projects": {key: {"mcpServers": {"l": {"transport": "ws", "url": "wss://x"}}}}}
        ),
        encoding="utf-8",
    )
    with _client(write_config, tmp_path, _ON) as c:
        local = c.get("/api/config-write/mcp", params={"scope": "local", "project": "alpha"})
    assert local.json()["servers"]["l"] == {"type": "ws", "url": "wss://x"}
    assert [(n["name"], n["field"]) for n in local.json()["conversions"]] == [("l", "transport")]


# --- TASK-449.8: a masked credential is bound to its destination ------------------
# `merge_redacted` restores a mask by key. Without a destination check, changing a
# remote server's url and resending the mask would send the stored credential to the
# new host.

_BOUND = {
    "type": "http",
    "url": "https://mcp.example.com/v1/mcp?tenant=a",
    "headers": {"Authorization": "Bearer sk-FAKE4498"},
    "headersHelper": "print-token-4498",
    "oauth": {"clientId": "client-4498"},
}


def _seed_bound(tmp_path: Path) -> bytes:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": _BOUND}}), encoding="utf-8"
    )
    return (tmp_path / ".mcp.json").read_bytes()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"url": "https://evil.example.net/v1/mcp?tenant=a"}, id="host"),
        pytest.param({"url": "http://mcp.example.com/v1/mcp?tenant=a"}, id="scheme"),
        pytest.param({"url": "https://mcp.example.com:8443/v1/mcp?tenant=a"}, id="port"),
        pytest.param({"url": "https://mcp.example.com/other?tenant=a"}, id="path"),
        pytest.param({"url": "https://mcp.example.com:bad/v1"}, id="unparseable"),
        # Exact text, never parsed: urlsplit reads this host as mcp.example.com, while the
        # WHATWG parser in Claude Code's Node runtime reads evil.example.net.
        pytest.param(
            {"url": "https://evil.example.net\\@mcp.example.com/v1/mcp?tenant=a"},
            id="backslash",
        ),
        pytest.param({"url": "https://mcp.example.com/v1/mcp?tenant=b"}, id="query"),
        pytest.param({"url": "HTTPS://MCP.EXAMPLE.COM/v1/mcp?tenant=a"}, id="case"),
        pytest.param({"url": "https://mcp.example.com:443/v1/mcp?tenant=a"}, id="default-port"),
        pytest.param(
            {"oauth": {"clientId": "********", "authServerMetadataUrl": "https://a.example/m"}},
            id="added-nested-url",
        ),
        pytest.param({"type": "sse"}, id="type"),
        pytest.param({"type": "streamable-http"}, id="alias-is-same-type"),
    ],
)
def test_masked_credential_is_refused_for_a_new_destination(
    tmp_path: Path, change: dict, request: pytest.FixtureRequest
) -> None:
    before = _seed_bound(tmp_path)
    shown, h = mcp.read_project_servers(tmp_path)
    edited = {**shown["s"], **change}
    if request.node.callspec.id == "alias-is-same-type":
        # `streamable-http` IS `http`: the destination is unchanged, so the mask is kept.
        mcp.write_project_server_entry(tmp_path, "s", edited, op="edit")
        on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["s"]["headers"] == _BOUND["headers"]
        return
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_project_server_entry(tmp_path, "s", edited, op="edit")
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_project_servers(tmp_path, {"s": edited}, h)
    assert (tmp_path / ".mcp.json").read_bytes() == before  # nothing written


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({}, id="unchanged"),
        pytest.param({"timeout": 120000}, id="other-field"),
    ],
)
def test_masked_credential_is_kept_for_the_same_destination(tmp_path: Path, change: dict) -> None:
    _seed_bound(tmp_path)
    shown, h = mcp.read_project_servers(tmp_path)
    edited = {**shown["s"], **change}
    mcp.write_project_server_entry(tmp_path, "s", edited, op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["s"]
    assert on_disk["headers"] == _BOUND["headers"]
    assert on_disk["headersHelper"] == _BOUND["headersHelper"]
    assert on_disk["oauth"] == _BOUND["oauth"]


def test_url_texts_walks_lists_and_restores_masks_by_path() -> None:
    m = cw.REDACTION_SENTINEL
    stored = {"a": [{"iconUrl": "https://i.example/1"}], "url": "https://x.example"}
    shown = {"a": [{"iconUrl": m}, {"iconUrl": "https://new.example"}], "url": m}
    assert mcp._url_texts(shown, stored) == {
        ("a", 0, "iconUrl"): "https://i.example/1",
        ("a", 1, "iconUrl"): "https://new.example",
        ("url",): "https://x.example",
    }
    # Tuple paths: a key spelled with a dot cannot collide with a nested path.
    dotted = {"oauth.authServerMetadataUrl": "A", "oauth": {"authServerMetadataUrl": "B"}}
    assert mcp._url_texts(dotted, {}) == {
        ("oauth.authServerMetadataUrl",): "A",
        ("oauth", "authServerMetadataUrl"): "B",
    }


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"toolPermissions": {"fetch_url": "allow"}}, id="tool-named-fetch_url"),
        pytest.param(
            {"headers": {"X": cw.REDACTION_SENTINEL, "X-Callback-Url": "b"}}, id="header"
        ),
    ],
)
def test_free_form_names_ending_in_url_are_not_destinations(tmp_path: Path, change: dict) -> None:
    # A tool or header NAME that ends in "url" is not a url field: editing it while a
    # masked credential is kept must not be refused as "a url changed".
    stored = {
        "type": "http",
        "url": "https://x.example/mcp",
        "headers": {"X": "h-4498", "X-Callback-Url": "a"},
        "toolPermissions": {"fetch_url": "ask"},
    }
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    shown, _h = mcp.read_project_servers(tmp_path)
    mcp.write_project_server_entry(tmp_path, "s", {**shown["s"], **change}, op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["s"]
    assert on_disk["headers"]["X"] == "h-4498"


def test_nested_url_change_refuses_a_mask(tmp_path: Path) -> None:
    stored = {
        "type": "http",
        "url": "https://x.example/mcp",
        "headers": {"X": "h-4498"},
        "oauth": {"authServerMetadataUrl": "https://auth.example/meta"},
    }
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    before = (tmp_path / ".mcp.json").read_bytes()
    shown, _h = mcp.read_project_servers(tmp_path)
    moved = {**shown["s"], "oauth": {"authServerMetadataUrl": "https://evil.example/meta"}}
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_project_server_entry(tmp_path, "s", moved, op="edit")
    removed = {k: v for k, v in shown["s"].items() if k != "oauth"}
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_project_server_entry(tmp_path, "s", removed, op="edit")
    assert (tmp_path / ".mcp.json").read_bytes() == before
    # Resent unchanged (the nested url shows masked), the mask is kept.
    mcp.write_project_server_entry(tmp_path, "s", shown["s"], op="edit")
    assert (
        json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["s"]
        == stored
    )


def test_new_destination_with_retyped_credentials_is_accepted(tmp_path: Path) -> None:
    _seed_bound(tmp_path)
    fresh = {"type": "http", "url": "https://other.example.org/mcp", "headers": {"X": "new"}}
    mcp.write_project_server_entry(tmp_path, "s", fresh, op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert on_disk["s"] == fresh


def test_masked_url_with_masked_header_is_kept(tmp_path: Path) -> None:
    # A credential url is itself masked; a resent mask restores it, so the host is the same.
    stored = {
        "type": "http",
        "url": "https://user:pw-4498@mcp.example.com/mcp",
        "headers": {"X": "h-4498"},
    }
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    shown, h = mcp.read_project_servers(tmp_path)
    assert shown["s"]["url"] == cw.REDACTION_SENTINEL
    mcp.write_project_servers(tmp_path, shown, h)
    assert (
        json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["s"]
        == stored
    )


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(
            {"type": "http", "url": 5, "headers": {"X": "h"}}, id="stored-url-not-a-string"
        ),
        pytest.param(
            {"type": "ftp", "url": "https://x.example/mcp", "headers": {"X": "h"}},
            id="stored-type-unknown",
        ),
    ],
)
def test_undecidable_stored_destination_refuses_a_mask(tmp_path: Path, stored: dict) -> None:
    # A stored entry whose destination cannot be decided never matches: fail closed.
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    entry = {
        "type": "http",
        "url": "https://x.example/mcp",
        "headers": {"X": cw.REDACTION_SENTINEL},
    }
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_project_server_entry(tmp_path, "s", entry, op="edit")


_STDIO = {"command": "npx", "args": ["-y", "srv@1"], "env": {"T": "sk-4498", "MODE": "a"}}


def _seed_stdio(tmp_path: Path) -> bytes:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": _STDIO}}), encoding="utf-8"
    )
    return (tmp_path / ".mcp.json").read_bytes()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"command": "new"}, id="command"),
        pytest.param({"command": "NPX"}, id="command-case"),
        pytest.param({"args": ["-y", "srv@2"]}, id="arg-changed"),
        pytest.param({"args": ["srv@1", "-y"]}, id="arg-order"),
        pytest.param({"args": ["-y", "srv@1", "--extra"]}, id="arg-added"),
        pytest.param({"args": ["-y"]}, id="arg-removed"),
        pytest.param({"args": []}, id="args-emptied"),
    ],
)
def test_stdio_command_or_args_change_refuses_a_masked_env(tmp_path: Path, change: dict) -> None:
    # Maintainer decision (2026-11-05): a masked env is bound to the exact command and args,
    # because the env goes to whatever program those name.
    before = _seed_stdio(tmp_path)
    shown, h = mcp.read_project_servers(tmp_path)
    edited = {**shown["s"], **change}
    with pytest.raises(cw.InvalidCandidateError, match="Retype the masked env values"):
        mcp.write_project_server_entry(tmp_path, "s", edited, op="edit")
    with pytest.raises(cw.InvalidCandidateError, match="Retype the masked env values"):
        mcp.write_project_servers(tmp_path, {"s": edited}, h)
    assert (tmp_path / ".mcp.json").read_bytes() == before


def test_stdio_masked_arg_without_env_is_bound_too(tmp_path: Path) -> None:
    # A masked args element (here a ${VAR}) is handed to the program as well, so it is
    # bound like an env value, and the message does not claim an env exists.
    stored = {"command": "srv", "args": ["--key", "${API_KEY}"]}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    before = (tmp_path / ".mcp.json").read_bytes()
    shown, _h = mcp.read_project_servers(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="any other masked value"):
        mcp.write_project_server_entry(tmp_path, "s", {**shown["s"], "command": "evil"}, op="edit")
    assert (tmp_path / ".mcp.json").read_bytes() == before


def test_stdio_new_command_with_retyped_env_saves(tmp_path: Path) -> None:
    _seed_stdio(tmp_path)
    fresh = {"command": "uvx", "args": ["other"], "env": {"T": "sk-new", "MODE": "b"}}
    mcp.write_project_server_entry(tmp_path, "s", fresh, op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert on_disk["s"] == fresh


def test_stdio_unmasked_env_change_restores_the_masks(tmp_path: Path) -> None:
    # Command and args unchanged; the operator retypes one env value and leaves the other
    # masked: the mask is restored from the stored value.
    _seed_stdio(tmp_path)
    shown, _h = mcp.read_project_servers(tmp_path)
    edited = {**shown["s"], "env": {"T": cw.REDACTION_SENTINEL, "MODE": "b"}}
    mcp.write_project_server_entry(tmp_path, "s", edited, op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert on_disk["s"] == {**_STDIO, "env": {"T": "sk-4498", "MODE": "b"}}


def test_stdio_masked_arg_resent_counts_as_unchanged(tmp_path: Path) -> None:
    # An args element masked by shape (a ${VAR}) is restored at its index, so resending the
    # masked view unchanged keeps both the arg and the env.
    stored = {"command": "srv", "args": ["--key", "${API_KEY}"], "env": {"T": "sk-4498"}}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    shown, _h = mcp.read_project_servers(tmp_path)
    assert shown["s"]["args"][1] == cw.REDACTION_SENTINEL
    mcp.write_project_server_entry(tmp_path, "s", shown["s"], op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert on_disk["s"] == stored


def test_stdio_masked_command_resent_counts_as_unchanged(tmp_path: Path) -> None:
    stored = {"command": "${MCP_BIN}", "env": {"T": "sk-4498"}}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    shown, _h = mcp.read_project_servers(tmp_path)
    assert shown["s"]["command"] == cw.REDACTION_SENTINEL
    mcp.write_project_server_entry(tmp_path, "s", shown["s"], op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert on_disk["s"] == stored


def test_stdio_stored_args_not_a_list_refuses_a_masked_env(tmp_path: Path) -> None:
    # A hand-edited `args` that is not a list never equals any list: fail closed.
    stored = {"command": "srv", "args": "oops", "env": {"T": "sk-4498"}}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    entry = {"command": "srv", "args": [], "env": {"T": cw.REDACTION_SENTINEL}}
    with pytest.raises(cw.InvalidCandidateError, match="Retype the masked env values"):
        mcp.write_project_server_entry(tmp_path, "s", entry, op="edit")


def test_stdio_absent_args_equals_empty_args(tmp_path: Path) -> None:
    # The CLI writes `"args": []` for an entry stored without args; the two are the same.
    stored = {"command": "srv", "env": {"T": "sk-4498"}}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    shown, _h = mcp.read_project_servers(tmp_path)
    mcp.write_project_server_entry(tmp_path, "s", {**shown["s"], "args": []}, op="edit")
    on_disk = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert on_disk["s"] == {**stored, "args": []}


def test_remote_to_stdio_with_a_mask_is_refused(tmp_path: Path) -> None:
    # A legacy remote `env` backs the mask by key, so only the type change stops it from
    # being handed to a new stdio program.
    stored = {"type": "http", "url": "https://x.example/mcp", "env": {"K": "sk-4498"}}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"s": stored}}), encoding="utf-8"
    )
    before = (tmp_path / ".mcp.json").read_bytes()
    entry = {"command": "x", "env": {"K": cw.REDACTION_SENTINEL}}
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_project_server_entry(tmp_path, "s", entry, op="edit")
    assert (tmp_path / ".mcp.json").read_bytes() == before


def test_destination_binding_on_user_and_local_scope(tmp_path: Path) -> None:
    cj = tmp_path / "claude.json"
    proj = tmp_path / "p"
    cj.write_text(
        json.dumps(
            {"mcpServers": {"u": _BOUND}, "projects": {str(proj): {"mcpServers": {"l": _BOUND}}}}
        ),
        encoding="utf-8",
    )
    before = cj.read_bytes()
    moved = {"url": "https://evil.example.net/v1/mcp"}
    user = mcp.read_user_servers(cj)["u"]
    local = mcp.read_project_local_servers(cj, proj)["l"]
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_user_server_entry(cj, "u", {**user, **moved}, op="edit")
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_project_local_server_entry(cj, proj, "l", {**local, **moved}, op="edit")
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_user_servers(cj, {"u": {**user, **moved}})
    assert cj.read_bytes() == before


def test_route_edit_to_a_new_host_with_a_mask_is_422(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    mcp_json = projects_root / "alpha" / ".mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {"s": _BOUND}}), encoding="utf-8")
    before = mcp_json.read_bytes()
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path, _ON) as c:
        shown = c.get(
            "/api/config-write/mcp", params={"scope": "project", "project": "alpha"}
        ).json()["servers"]["s"]
        resp = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "edit",
                "name": "s",
                "entry": {**shown, "url": "https://evil.example.net/v1/mcp"},
            },
        )
    assert resp.status_code == 422
    assert "Retype the credential" in resp.json()["detail"]
    assert mcp_json.read_bytes() == before
    assert not argv_file.exists()


# --- TASK-449.8: one file read per MCP GET -----------------------------------------
# The entries, the hash and the conversion notes must describe the SAME file contents.
# With two reads, a write in between made the notes disagree with the entries, and a
# missing note left an unchanged converted entry unsavable in the dashboard.

_LEGACY_MAP = {"mcpServers": {"s": {"transport": "sse", "url": "https://x.example/sse"}}}
_LEGACY_LOCAL = {"transport": "sse", "url": "https://x.example/sse"}


def _flip_after_first_read(monkeypatch, target: Path, second: bytes) -> list[int]:
    """Make ``target`` read as itself once, then as ``second``; return the read counter."""
    real = Path.read_bytes
    count = [0]

    def read_bytes(self: Path) -> bytes:
        if self == target:
            count[0] += 1
            if count[0] > 1:
                return second
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return count


@pytest.mark.parametrize("scope", ["project", "user", "local"])
def test_route_read_takes_entries_hash_and_notes_from_one_read(
    write_config, tmp_path, projects_root, monkeypatch, scope: str
) -> None:
    isolated = Path(os.environ["HOME"]) / ".claude.json"
    alpha = (projects_root / "alpha").resolve()
    if scope == "project":
        target = projects_root / "alpha" / ".mcp.json"
        target.write_text(json.dumps(_LEGACY_MAP), encoding="utf-8")
    elif scope == "user":
        target = isolated
        target.write_text(json.dumps(_LEGACY_MAP), encoding="utf-8")
    else:
        target = isolated
        target.write_text(
            json.dumps({"projects": {str(alpha): {"mcpServers": {"s": _LEGACY_LOCAL}}}}),
            encoding="utf-8",
        )
    original = target.read_bytes()
    with _client(write_config, tmp_path, _ON) as c:
        # Any second read of the file would see a clean entry and so lose the note.
        clean = b'{"mcpServers": {"s": {"type": "sse", "url": "https://x.example/sse"}}}'
        count = _flip_after_first_read(monkeypatch, target.resolve(), clean)
        params = {"scope": scope} if scope == "user" else {"scope": scope, "project": "alpha"}
        resp = c.get("/api/config-write/mcp", params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert count[0] == 1  # exactly one read of the scope's file per request
    assert body["servers"]["s"] == {"type": "sse", "url": "https://x.example/sse"}
    assert [(n["name"], n["field"]) for n in body["conversions"]] == [("s", "transport")]
    if scope == "project":
        assert body["hash"] == cw.hash_bytes(original)  # the hash of the bytes it showed
    else:
        assert body["hash"] is None


def test_read_servers_view_matches_the_individual_readers(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(json.dumps(_LEGACY_MAP), encoding="utf-8")
    servers, file_hash, notes = mcp.read_servers_view(
        "project", claude_json=tmp_path / "cj", project_dir=tmp_path
    )
    assert (servers, file_hash) == mcp.read_project_servers(tmp_path)
    assert notes == mcp.read_conversion_notes(
        "project", claude_json=tmp_path / "cj", project_dir=tmp_path
    )
