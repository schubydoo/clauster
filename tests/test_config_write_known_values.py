"""Known-value lists are suggestions with a warning, never refusals (TASK-524).

The config editor's lists of known event names, hook types, keys and values are copied by
hand from the Claude Code docs, so they lag behind every Claude Code release. A save with a
value outside one of them goes through, and its response carries ``warnings``. These tests
cover the shared seam (:func:`clauster.config_write.warn_unknown_value` and the route
decorator), one unknown value saved through the route of each tab, and the refusals that
stay: shape checks, the bypass and ``inherit`` gates, and masked-value binding.

Every route test runs under the autouse HOME-isolation fixture and writes only into the
isolated tmp home and the tmp projects root.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_hooks as hooks
from clauster import config_write_mcp as mcp
from clauster import config_write_permissions as perms
from clauster import config_write_skills as sk
from clauster import config_write_subagents as sub
from clauster.app import create_app
from clauster.config import load_config

_WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""
FAKE_CLAUDE = (
    Path(__file__).resolve().parent / "fixtures" / "fake_claude" / f"claude{_WIN_STUB_SUFFIX}"
)
_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_GROUP = {"hooks": [{"type": "command", "command": "echo hi"}]}


def _client(write_config, tmp_path: Path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    return TestClient(create_app(load_config(cfg)))


def _target(**body: object) -> dict:
    return {"scope": "project", "project": "alpha", "confirm": "alpha", **body}


def _settings(projects_root: Path) -> dict:
    return json.loads((projects_root / "alpha" / ".claude" / "settings.json").read_text())


# --- the shared seam -------------------------------------------------------------------


def test_a_warning_outside_a_save_is_dropped() -> None:
    # No route is collecting (a read path, a validator called directly): nothing is kept,
    # and nothing raises.
    cw.warn_unknown_value("x", "y", ("z",))
    with cw.collect_value_warnings() as warnings:
        cw.warn_unknown_value("x", "y", ("z",))
    assert warnings == [
        "x 'y' is not one of the known values (z). It saves as typed. "
        "The list comes from the Claude Code docs and can be out of date."
    ]


def test_the_same_warning_is_kept_once() -> None:
    with cw.collect_value_warnings() as warnings:
        for _ in range(3):
            cw.warn_unknown_value("x", "y", ("z",))
    assert len(warnings) == 1


def test_a_resent_mask_is_never_warned_about() -> None:
    # "********" means "keep the stored value", which the save cannot judge.
    with cw.collect_value_warnings() as warnings:
        cw.warn_unknown_value("x", cw.REDACTION_SENTINEL, ("z",))
    assert warnings == []


def test_a_warning_never_echoes_a_secret_shaped_value() -> None:
    # A merge can restore a stored value before validation runs, so the quote is redacted.
    with cw.collect_value_warnings() as warnings:
        cw.warn_unknown_value("x", "https://user:hunter2@example.com/", ("z",))
    assert "hunter2" not in warnings[0]
    assert cw.REDACTION_SENTINEL in warnings[0]


def test_a_long_value_is_cut_and_a_long_list_is_not_spelled_out() -> None:
    with cw.collect_value_warnings() as warnings:
        cw.warn_unknown_value("x", "v" * 500, [f"k{i}" for i in range(11)])
        cw.warn_unknown_value("y", "v", [f"k{i}" for i in range(10)])
    assert "v" * 100 not in warnings[0] and "…" in warnings[0]
    assert "known values. It saves" in warnings[0]  # 11 names: not listed
    assert "(k0, k1, k2" in warnings[1]  # 10 names: listed


def test_the_previous_sink_comes_back_after_a_block_that_raised() -> None:
    with cw.collect_value_warnings() as outer:
        with pytest.raises(RuntimeError), cw.collect_value_warnings():
            raise RuntimeError
        cw.warn_unknown_value("x", "y", ())
    assert len(outer) == 1
    assert cw.value_warning_sink.get() is None


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ({"a": [1, 2.5, "x", None, True]}, True),
        (float("inf"), False),
        ({"a": [float("nan")]}, False),
        ([], True),
    ],
)
def test_walkable_value(value: object, ok: bool) -> None:
    assert cw.walkable_value(value) is ok


def test_walkable_value_stops_at_the_depth_limit() -> None:
    # MAX_UNKNOWN_VALUE_DEPTH containers nest; one more is refused.
    value: object = 1
    for _ in range(cw.MAX_UNKNOWN_VALUE_DEPTH):
        value = [value]
    assert cw.walkable_value(value)
    assert not cw.walkable_value([value])


def test_an_unknown_key_must_be_text_and_walkable() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="key 1 must be text"):
        cw.warn_unknown_keys({1: "a"}, (), "key")
    with pytest.raises(cw.InvalidCandidateError, match="not finite"):
        cw.warn_unknown_keys({"a": float("inf")}, (), "key")


# --- one unknown value per tab, through its save route -----------------------------------


def test_hooks_route_unknown_event_type_and_field(write_config, tmp_path, projects_root) -> None:
    block = {
        "FutureEvent": [_GROUP],
        "Stop": [{"hooks": [{"type": "future", "anything": {"a": 1}}]}],
        "PreToolUse": [{"hooks": [{"type": "command", "command": "x", "newField": True}]}],
    }
    with _client(write_config, tmp_path) as c:
        resp = c.put("/api/config-write/hooks", json=_target(hooks=block))
        assert resp.status_code == 200, resp.text
        warnings = resp.json()["warnings"]
    assert sorted(w.split(" is not")[0] for w in warnings) == [
        "hooks 'PreToolUse'[0] hooks[0] 'command' hook field 'newField'",
        "hooks 'Stop'[0] hooks[0] 'type' 'future'",
        "hooks event 'FutureEvent'",
    ]
    assert _settings(projects_root)["hooks"] == block


def test_permissions_route_unknown_key_and_auto_mode_key(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path) as c:
        resp = c.put(
            "/api/config-write/permissions",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "permissions": {"futureList": ["a"], "disableAutoMode": "off"},
                "autoMode": {"newRules": ["be kind"]},
            },
        )
        assert resp.status_code == 200, resp.text
        warnings = resp.json()["warnings"]
    assert {w.split(" is not")[0] for w in warnings} == {
        "permissions key 'futureList'",
        "permissions 'disableAutoMode' 'off'",
        "autoMode key 'newRules'",
    }


def test_mcp_route_unknown_transport(write_config, tmp_path, projects_root) -> None:
    servers = {"pigeon": {"type": "carrier-pigeon", "url": "https://loft.example/"}}
    with _client(write_config, tmp_path) as c:
        resp = c.put("/api/config-write/mcp", json=_target(servers=servers))
        assert resp.status_code == 200, resp.text
        [warning] = resp.json()["warnings"]
    assert "server 'pigeon' 'type' 'carrier-pigeon' is not one of the known values" in warning
    stored = json.loads((projects_root / "alpha" / ".mcp.json").read_text())
    assert stored["mcpServers"] == servers


def test_mcp_server_route_carries_warnings(write_config, tmp_path, projects_root) -> None:
    # An env value keeps the entry off the CLI's argv, so the direct writer stores it.
    entry = {"command": "run-it", "env": {"A": "b"}, "cwd": "/srv/x"}
    with _client(write_config, tmp_path) as c:
        resp = c.post(
            "/api/config-write/mcp/server", json=_target(op="add", name="srv", entry=entry)
        )
        assert resp.status_code == 200, resp.text
        [warning] = resp.json()["warnings"]
    assert "stdio server 'srv' key 'cwd' is not one of the known values" in warning


def test_mcp_client_secret_on_an_unknown_transport_is_a_422_not_a_500(
    write_config, tmp_path
) -> None:
    entry = {"type": "carrier-pigeon", "url": "https://loft.example/"}
    with _client(write_config, tmp_path) as c:
        resp = c.post(
            "/api/config-write/mcp/server",
            json=_target(op="add", name="srv", entry=entry, client_secret="s"),
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "'client_secret' applies only to an http or sse server"


def test_subagent_route_unknown_color(write_config, tmp_path, projects_root) -> None:
    content = "---\nname: my-agent\ndescription: d\ncolor: teal\n---\nYou help.\n"
    with _client(write_config, tmp_path) as c:
        resp = c.put("/api/config-write/subagents/my-agent", json=_target(content=content))
        assert resp.status_code == 200, resp.text
        [warning] = resp.json()["warnings"]
    assert "'color' 'teal' is not one of the known values" in warning
    path = projects_root / "alpha" / ".claude" / "agents" / "my-agent.md"
    assert path.read_text() == content


def test_skill_route_unknown_shell(write_config, tmp_path, projects_root) -> None:
    content = "---\ndescription: d\nshell: fish\n---\nBody.\n"
    with _client(write_config, tmp_path) as c:
        resp = c.put(
            "/api/config-write/skills", json=_target(name="s", files={"SKILL.md": content})
        )
        assert resp.status_code == 200, resp.text
        warnings = resp.json()["warnings"]
    assert warnings and all("'shell' 'fish' is not one of the known values" in w for w in warnings)
    path = projects_root / "alpha" / ".claude" / "skills" / "s" / sk.SKILL_FILENAME
    assert path.read_text() == content


def test_a_refused_save_answers_no_warnings(write_config, tmp_path, projects_root) -> None:
    # The unknown event would warn, but the bad shape refuses the whole save first.
    block = {"FutureEvent": [_GROUP], "Stop": "not-a-list"}
    with _client(write_config, tmp_path) as c:
        resp = c.put("/api/config-write/hooks", json=_target(hooks=block))
    assert resp.status_code == 422
    assert "warnings" not in resp.json()
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


# --- the refusals that stay -------------------------------------------------------------


@pytest.mark.parametrize(
    "mode", ["bypassPermissions", "BypassPermissions", "bypass_permissions", " bypass-Permissions"]
)
def test_every_spelling_of_bypass_mode_stays_refused(mode: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="bypass"):
        perms.validate_permissions({"defaultMode": mode})
    with pytest.raises(cw.InvalidCandidateError, match="bypass"):
        sub.validate_frontmatter({"name": "x", "description": "d", "permissionMode": mode})


def test_inherit_stays_refused_as_a_mode() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="inherit"):
        perms.validate_permissions({"defaultMode": "inherit"})
    with pytest.raises(cw.InvalidCandidateError, match="inherit"):
        sub.validate_frontmatter({"name": "x", "description": "d", "permissionMode": "inherit"})


def test_auto_default_mode_stays_refused_at_project_scope(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="User scope"):
        perms.write_project_permissions(
            tmp_path, {"defaultMode": "auto"}, expected_hash=None, auto_mode=None
        )


def test_a_masked_value_in_an_unknown_hook_type_is_refused(tmp_path: Path) -> None:
    # A type outside the known list has no destination, so a mask cannot be bound.
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    stored = {"Stop": [{"hooks": [{"type": "future", "url": "https://h.example/?t=secret"}]}]}
    settings.write_text(json.dumps({"hooks": stored}))
    view, file_hash = hooks.read_user_hooks(settings)
    assert "secret" not in json.dumps(view)
    with pytest.raises(cw.InvalidCandidateError, match="does not know"):
        hooks.write_user_hooks(settings, view, file_hash)
    assert json.loads(settings.read_text())["hooks"] == stored


def test_a_masked_value_in_an_unknown_mcp_transport_is_refused(tmp_path: Path) -> None:
    stored = {"srv": {"type": "carrier-pigeon", "env": {"TOKEN": "s3cret"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": stored}))
    view, file_hash = mcp.read_project_servers(tmp_path)
    assert view["srv"]["env"]["TOKEN"] == cw.REDACTION_SENTINEL
    with pytest.raises(cw.InvalidCandidateError, match="not one Clauster knows"):
        mcp.write_project_servers(tmp_path, view, file_hash)
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"] == stored


def test_an_unknown_auto_mode_list_keeps_its_masked_entries(tmp_path: Path) -> None:
    # A mask as a direct list entry is restored by position, as in the known lists.
    settings = tmp_path / "settings.json"
    secret = "deploy with https://user:pw@example.com/"
    settings.write_text(json.dumps({"autoMode": {"newRules": ["plain", secret]}}))
    _p, auto_mode, file_hash = perms.read_user_permissions(settings)
    assert auto_mode == {"newRules": ["plain", cw.REDACTION_SENTINEL]}
    perms.write_user_permissions(settings, {}, file_hash, auto_mode=auto_mode)
    assert json.loads(settings.read_text())["autoMode"] == {"newRules": ["plain", secret]}


# --- shape checks on a type or key outside the known lists ------------------------------


def test_an_unknown_hook_type_still_checks_the_shape_of_each_field() -> None:
    entry = {"type": "future", "deep": float("inf")}
    with pytest.raises(cw.InvalidCandidateError, match="'deep' must be a text key"):
        hooks.validate_hooks({"Stop": [{"hooks": [entry]}]})


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"type": "pigeon", 1: "x"}, "key 1 must be text"),
        ({"type": "pigeon", "loft": float("inf")}, "not finite"),
        ({"type": "pigeon", "url": 5}, "'url' must be a string"),
        ({"type": "pigeon", "env": {"A": 1}}, "'env'"),
        ({"type": "pigeon", "args": "a b"}, "'args' must be a list of strings"),
        ({"type": "pigeon", "oauth": {"callbackPort": 0}}, "callbackPort"),
        ({"type": "pigeon", "request_timeout_ms": 0}, "request_timeout_ms"),
        ({"type": "pigeon", "toolPermissions": {"t": 1}}, "toolPermissions"),
        ({"type": "pigeon", "timeout": -1}, "timeout"),
    ],
)
def test_an_unknown_mcp_transport_still_checks_each_known_field(entry: dict, match: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match=match):
        mcp.validate_mcp_servers({"srv": entry})


def test_an_unknown_mcp_transport_with_well_typed_fields_warns_once() -> None:
    entry = {
        "type": "pigeon",
        "command": "fly",
        "args": ["north"],
        "url": "https://loft.example/",
        "headersHelper": "h",
        "oauth": {"clientId": "c"},
        "request_timeout_ms": 5,
        "tools": [{"name": "t"}],
    }
    with cw.collect_value_warnings() as warnings:
        mcp.validate_mcp_servers({"srv": entry})
    assert len(warnings) == 1 and "'type' 'pigeon'" in warnings[0]


def test_several_unknown_auto_mode_keys_are_each_checked_for_a_mask() -> None:
    with cw.collect_value_warnings() as warnings:
        perms.validate_auto_mode({"a": ["x"], "b": {"c": "d"}})
    assert len(warnings) == 2
    with pytest.raises(cw.InvalidCandidateError, match="'b' keeps a masked value"):
        perms.validate_auto_mode({"a": ["x"], "b": {"c": cw.REDACTION_SENTINEL}})


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "pigeon", "url": "https://loft.example/"},
        {"type": "http", "url": "https://x.example/", "vendorThing": "opaque"},
        {"type": "http", "url": "https://x.example/", "oauth": {"clientId": "c", "extra": "e"}},
        {"type": "http", "url": "https://x.example/", "tools": [{"name": "t", "x": 1}]},
        {"command": "run", "cwd": "/srv"},
    ],
)
def test_an_entry_with_an_unknown_field_never_reaches_the_cli_argv(entry: dict) -> None:
    # Nothing says whether an unknown value is a secret, so the direct writer stores it
    # and `claude mcp add-json` never sees it on its command line.
    from clauster import config_write_mcp_cli as mcp_cli

    assert mcp.has_unknown_fields(entry)
    assert mcp_cli.entry_needs_direct_write(entry)


def test_a_known_entry_still_takes_the_cli_path() -> None:
    from clauster import config_write_mcp_cli as mcp_cli

    entry = {"type": "http", "url": "https://x.example/", "tools": [{"name": "t"}]}
    assert not mcp.has_unknown_fields(entry)
    assert not mcp_cli.entry_needs_direct_write(entry)


# --- review round 1: a warning never quotes a restored value, masks stay bound -----------

_SECRET = "sk-live-REALSECRET123"


def test_a_subagent_save_never_warns_with_a_restored_stored_value(tmp_path: Path) -> None:
    # `read_token` is a secret-shaped key, so the read view masks its value; the save
    # puts the stored value back, and the check after that must not quote it.
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "probe.md").write_bytes(
        (
            "---\nname: probe\ndescription: d\nmcpServers:\n  - srv:\n      type: http\n"
            f"      url: https://example.com/mcp\n      toolPermissions:\n"
            f"        read_token: {_SECRET}\n---\nbody\n"
        ).encode()
    )
    doc = sub.read_project_agent(tmp_path, "probe")
    assert _SECRET not in doc["content"]
    with cw.collect_value_warnings() as warnings:
        sub.write_project_agent(tmp_path, "probe", doc["content"], doc["hash"])
    assert not any(_SECRET in w for w in warnings), warnings
    assert _SECRET in (agents / "probe.md").read_text()  # the stored value was kept


def test_a_skill_save_never_warns_with_a_restored_stored_value(tmp_path: Path) -> None:
    # An event named like a secret masks its whole value in the view; the save puts the
    # stored lines back, and the check after that would quote the masked hook type.
    stored = (
        "---\ndescription: d\nshell: zsh\nhooks:\n  password_hook:\n"
        "    - hooks: [{type: hidden_type_value, command: x}]\n---\nBody.\n"
    )
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: stored}, expected_hash=None)
    content, file_hash, _exists, _note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert "hidden_type_value" not in content
    with cw.collect_value_warnings() as warnings:
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: content}, expected_hash=file_hash)
    assert not any("hidden_type_value" in w for w in warnings), warnings
    assert (tmp_path / "skills" / "s" / sk.SKILL_FILENAME).read_text() == stored


def test_a_hooks_merge_never_warns_from_its_second_check() -> None:
    stored = {
        "FutureEvent": [{"hooks": [{"type": "http", "url": "https://h.example/?t=" + _SECRET}]}]
    }
    view = {"FutureEvent": [{"hooks": [{"type": "http", "url": "https://h.example/?t=********"}]}]}
    with cw.collect_value_warnings() as warnings:
        hooks._merge_hooks(view, stored)
    assert warnings == []


def test_an_unknown_auto_mode_key_restores_masks_by_the_view_positions(tmp_path: Path) -> None:
    # A secret-shaped key masks EVERY entry of its list in the view, not only the entries
    # that look secret: deleting the first mask must drop that entry, not "plain-rule".
    settings = tmp_path / "settings.json"
    stored = ["${A_SECRET}", "plain-rule", "${B_SECRET}"]
    settings.write_text(json.dumps({"autoMode": {"secret_rules": stored}}))
    _p, view, file_hash = perms.read_user_permissions(settings)
    assert view == {"secret_rules": [cw.REDACTION_SENTINEL] * 3}
    # Two masks for three masked entries: which one went is unknown, so the save refuses.
    # (Before the fix, it counted only two "secret-looking" entries and wrote them back.)
    with pytest.raises(cw.InvalidCandidateError, match="2 masked entries but 3 are stored"):
        perms.write_user_permissions(
            settings, {}, file_hash, auto_mode={"secret_rules": view["secret_rules"][1:]}
        )
    perms.write_user_permissions(settings, {}, file_hash, auto_mode=view)
    assert json.loads(settings.read_text())["autoMode"]["secret_rules"] == stored


def test_a_changed_unknown_mcp_key_refuses_a_kept_masked_header(tmp_path: Path) -> None:
    stored = {
        "srv": {
            "type": "http",
            "url": "https://good.example.com/mcp",
            "headers": {"Authorization": "Bearer REAL-SECRET"},
            "endpoint": "https://good.example.com/mcp",
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": stored}))
    view, file_hash = mcp.read_project_servers(tmp_path)
    edited = json.loads(json.dumps(view))
    edited["srv"]["endpoint"] = "https://evil.example.net/mcp"
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        mcp.write_project_servers(tmp_path, edited, file_hash)
    # Unchanged, the same masked header is kept.
    mcp.write_project_servers(tmp_path, view, file_hash)
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"] == stored


def test_a_changed_unknown_hook_field_refuses_a_kept_masked_header() -> None:
    hook = {"type": "http", "url": "https://h.example/x", "headers": {"A": "s3cret"}, "to": "a"}
    stored = {"Stop": [{"hooks": [hook]}]}
    view = hooks._redact_hooks(stored)
    view["Stop"][0]["hooks"][0]["to"] = "b"
    with pytest.raises(cw.InvalidCandidateError, match="cannot tell which"):
        hooks._merge_hooks(view, stored)
    assert hooks._merge_hooks(hooks._redact_hooks(stored), stored) == stored


@pytest.mark.parametrize("mode", ["bypassPermissions﻿", "bypass​Permissions", "ｂypassPermissions"])
def test_invisible_and_fullwidth_bypass_spellings_stay_refused(mode: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="bypass"):
        perms.validate_permissions({"defaultMode": mode})
    with pytest.raises(cw.InvalidCandidateError, match="bypass"):
        sub.validate_frontmatter({"name": "x", "description": "d", "permissionMode": mode})


def test_an_unknown_permissions_key_never_carries_bypass_mode() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="'defaultmode' cannot be"):
        perms.validate_permissions({"defaultmode": "bypassPermissions"})


def test_record_value_warnings_adds_to_the_active_save_only() -> None:
    cw.record_value_warnings(["dropped: no save is collecting"])
    with cw.collect_value_warnings() as warnings:
        cw.record_value_warnings(["a", "b", "a"])
    assert warnings == ["a", "b"]


def test_a_skill_member_with_no_restored_text_keeps_all_its_warnings() -> None:
    text = "---\ndescription: d\nshell: zsh\n---\nBody.\n"
    with cw.collect_value_warnings() as warnings:
        sk._validate_member(sk.SKILL_FILENAME, text, sent=text)
    assert len(warnings) == 1 and "'shell' 'zsh'" in warnings[0]
