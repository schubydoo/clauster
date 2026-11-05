"""Generic settings surface audit against current Claude Code settings (TASK-449.2).

Covers the narrowed carve (only keys another tab writes), the credential-helper keys
masked whole on read and kept on a resent mask, the refusal of a mask that no longer
lines up with a stored value, the refusal of a key at a scope where Claude Code ignores
it, and the merged view's Claude Code merge rules (objects merge, lists combine).

Every test that touches ``~/.claude/settings.json`` runs under the autouse
HOME-isolation fixture and writes only into the isolated tmp home.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_settings as cws
from clauster.app import create_app
from clauster.config import load_config

S = cw.REDACTION_SENTINEL


def _seed(path: Path, data: dict[str, Any]) -> str:
    """Write ``data`` as a settings file and return its content hash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    return cw.hash_bytes(raw)


def _nested(dotted: str, value: Any) -> dict[str, Any]:
    """Build ``{"a": {"b": value}}`` for ``"a.b"``."""
    out: Any = value
    for part in reversed(dotted.split(".")):
        out = {part: out}
    return out


def _write(scope: str, tmp_path: Path, incoming: dict[str, Any]) -> None:
    """Write ``incoming`` at ``scope`` (a fresh file, so no hash)."""
    if scope == "user":
        cws.write_user_settings(tmp_path / "home" / "settings.json", incoming, None)
    elif scope == "project":
        cws.write_project_settings(tmp_path, incoming, None)
    else:
        cws.write_project_local_settings(tmp_path, incoming, None)


# --- the carve: only keys another tab writes ----------------------------------------


def test_owned_keys_are_exactly_the_keys_another_tab_writes() -> None:
    assert cws.OWNED_KEY_TABS == {
        "permissions": "Permissions",
        "autoMode": "Permissions",
        "hooks": "Hooks",
        "enabledPlugins": "Plugins",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # Not read by Claude Code 2.1.318 and not written by the Plugins tab.
        ("disabledPlugins", {"p@m": True}),
        # The MCP tab writes approvals to ~/.claude.json and shows these read-only.
        ("enabledMcpjsonServers", ["a"]),
        ("disabledMcpjsonServers", ["b"]),
        # The Skills tab has no editor for it yet (TASK-249).
        ("skillOverrides", {"x": "off"}),
    ],
)
def test_uncarved_key_round_trips_through_the_settings_tab(
    tmp_path: Path, key: str, value: Any
) -> None:
    h = _seed(cw.project_settings_path(tmp_path), {key: value, "hooks": {}})
    view, h2 = cws.read_project_settings(tmp_path)
    assert view == {key: value} and h2 == h
    cws.write_project_settings(tmp_path, {"model": "opus"}, h)  # the key is removed
    on_disk = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == {"hooks": {}, "model": "opus"}


# --- credential helpers: masked whole on read, kept on a resent mask -----------------


@pytest.mark.parametrize("key", sorted(cws.SECRET_HELPER_KEYS))
def test_helper_key_is_masked_on_read_and_kept_on_resend(tmp_path: Path, key: str) -> None:
    secret = "echo sk-live-4492"
    h = _seed(cw.project_settings_path(tmp_path), {key: secret, "model": "opus"})
    view, _ = cws.read_project_settings(tmp_path)
    assert view == {key: S, "model": "opus"}
    assert secret not in json.dumps(view)
    cws.write_project_settings(tmp_path, {**view, "model": "sonnet"}, h)
    on_disk = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == {key: secret, "model": "sonnet"}


def test_otel_headers_helper_is_masked_although_its_name_is_not_secret_shaped(
    tmp_path: Path,
) -> None:
    # Positive control for the explicit list: the key-name heuristic alone leaves it clear.
    misc = {"otelHeadersHelper": "printf 'Authorization=Bearer abc'"}
    assert cw.redact_secrets(misc) == misc
    _seed(cw.project_settings_path(tmp_path), misc)
    assert cws.read_project_settings(tmp_path)[0] == {"otelHeadersHelper": S}


@pytest.mark.parametrize(
    "env",
    ["ANTHROPIC_API_KEY=sk-ant-4492 DEPLOY=abc", ["DEPLOY=abc", "TOKEN=sk-live-4492"], 4492],
)
def test_env_of_another_shape_is_masked_whole_in_the_view_and_merge(
    tmp_path: Path, env: Any
) -> None:
    # The key-name heuristic leaves these clear, so only the env rule masks them.
    _seed(cw.project_settings_path(tmp_path), {"env": env})
    view, h = cws.read_project_settings(tmp_path)
    assert view == {"env": S}
    assert "abc" not in json.dumps(view) and "4492" not in json.dumps(view)
    merged = cws._compute_effective_settings(user_misc={}, project_misc=view, local_misc={})
    assert merged["env"]["value"] == S
    # Resending the mask fails closed (env must be an object); the file is unchanged.
    with pytest.raises(cw.InvalidCandidateError, match="'env' must be an object"):
        cws.write_project_settings(tmp_path, view, h)
    on_disk = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == {"env": env}


def test_helper_mask_of_another_shape_is_masked_whole(tmp_path: Path) -> None:
    _seed(cw.project_settings_path(tmp_path), {"apiKeyHelper": {"cmd": "x"}})
    assert cws.read_project_settings(tmp_path)[0] == {"apiKeyHelper": S}


def test_helper_mask_sent_to_a_file_without_one_is_refused(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="apiKeyHelper holds a masked value"):
        cws.write_project_settings(tmp_path, {"apiKeyHelper": S}, None)
    assert not cw.project_settings_path(tmp_path).exists()


def test_env_rename_that_keeps_a_mask_is_refused(tmp_path: Path) -> None:
    # The mask was restored by key, so a renamed var used to be dropped silently.
    h = _seed(cw.project_settings_path(tmp_path), {"env": {"TOKEN": "s3cr3t"}})
    with pytest.raises(cw.InvalidCandidateError, match=r"env\.TOKEN2 holds a masked value"):
        cws.write_project_settings(tmp_path, {"env": {"TOKEN2": S}}, h)
    on_disk = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == {"env": {"TOKEN": "s3cr3t"}}


def test_reordered_list_holding_a_mask_is_refused(tmp_path: Path) -> None:
    # A heuristically masked list entry is restored by position, so a reorder would move
    # one entry's credential onto another.
    stored = [{"serverUrl": "https://u:p@a.example/x"}, {"serverName": "b"}]
    h = _seed(cw.project_settings_path(tmp_path), {"allowedMcpServers": stored})
    view, _ = cws.read_project_settings(tmp_path)
    assert S in json.dumps(view)
    with pytest.raises(cw.InvalidCandidateError, match="allowedMcpServers"):
        cws.write_project_settings(
            tmp_path, {"allowedMcpServers": list(reversed(view["allowedMcpServers"]))}, h
        )


def test_mask_moved_onto_a_clear_list_entry_is_refused(tmp_path: Path) -> None:
    # The list keeps its length and each masked element passes "unchanged apart from its
    # mask", but the mask now sits where the read showed a clear value: the positional
    # restore would copy "plain" twice and drop the credential.
    settings = tmp_path / "settings.json"
    h = _seed(settings, {"mirrors": ["plain", "https://user:tok@host/x"]})
    view, _ = cws.read_user_settings(settings)
    assert view == {"mirrors": ["plain", S]}
    with pytest.raises(cw.InvalidCandidateError, match="mirrors holds masked entries"):
        cws.write_user_settings(settings, {"mirrors": [S, "plain"]}, h)
    assert json.loads(settings.read_text(encoding="utf-8"))["mirrors"][1].startswith("https://")


@pytest.mark.parametrize(
    ("stored", "sent"),
    [
        # Delete the first masked entry and append a clear one: same length, slot 0 masked.
        (["https://u:one@h/x", "https://u:two@h/y"], [S, "plain"]),
        ([{"url": "https://u:one@h/x"}, {"url": "https://u:two@h/y"}], [{"url": S}, {"url": "n"}]),
    ],
)
def test_identical_masks_in_a_list_cannot_be_partly_replaced(
    tmp_path: Path, stored: list[Any], sent: list[Any]
) -> None:
    settings = tmp_path / "settings.json"
    h = _seed(settings, {"mirrors": stored})
    with pytest.raises(
        cw.InvalidCandidateError,
        match="mirrors holds masked entries that all look the same.*every masked entry in mirrors",
    ):
        cws.write_user_settings(settings, {"mirrors": sent}, h)
    # Retyping every masked entry is accepted.
    cws.write_user_settings(settings, {"mirrors": ["https://u:two@h/y", "plain"]}, h)


def test_identical_masks_in_a_nested_list_name_the_nested_path(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    h = _seed(settings, {"a": {"mirrors": ["https://u:one@h/x", "https://u:two@h/y"]}})
    with pytest.raises(cw.InvalidCandidateError, match=r"a\.mirrors holds masked entries"):
        cws.write_user_settings(settings, {"a": {"mirrors": [S, "plain"]}}, h)


def test_mask_under_a_value_masked_whole_is_refused(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    h = _seed(settings, {"apiKeyHelper": {"cmd": "secret"}})
    with pytest.raises(cw.InvalidCandidateError, match=r"apiKeyHelper\.cmd holds a masked"):
        cws.write_user_settings(settings, {"apiKeyHelper": {"cmd": S}}, h)


def test_mask_left_in_place_still_saves(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    h = _seed(settings, {"mirrors": ["plain", "https://user:tok@host/x"], "model": "a"})
    view, _ = cws.read_user_settings(settings)
    cws.write_user_settings(settings, {**view, "model": "b"}, h)
    on_disk = json.loads(settings.read_text(encoding="utf-8"))
    assert on_disk == {"mirrors": ["plain", "https://user:tok@host/x"], "model": "b"}


# --- keys Claude Code reads only at some scopes ----------------------------------------


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("allowedMarketplaces", "managed"),
        ("attributionProjectPolicy", "managed"),
        ("remoteTools.allowUnattendedServing", "user"),
    ],
)
def test_cli_parsed_scope_limited_keys_are_listed(path: str, kind: str) -> None:
    # Undocumented, but the 2.1.318 schema text of each one names its scope.
    assert cws.SCOPE_LIMITED_KEYS[path] == kind


_SCOPES = ("user", "project", "local")


@pytest.mark.parametrize("path", sorted(cws.SCOPE_LIMITED_KEYS))
@pytest.mark.parametrize("scope", _SCOPES)
def test_every_scope_limited_key_is_refused_exactly_where_it_is_ignored(
    tmp_path: Path, path: str, scope: str
) -> None:
    read_at = cws._READ_FROM[cws.SCOPE_LIMITED_KEYS[path]][0]
    candidate = _nested(path, True)
    if scope in read_at:
        _write(scope, tmp_path, candidate)  # accepted
        return
    if path in cws.DESKTOP_ONLY_KEYS:
        expected = f"Only Claude Desktop reads {path}, from "
    else:
        expected = f"Claude Code reads {path} only from"
    with pytest.raises(cw.InvalidCandidateError, match=expected):
        _write(scope, tmp_path, candidate)
    # Refused before any directory or file is created.
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / "home").exists()


# --- keys only Claude Desktop reads (TASK-490) ------------------------------------------

_MANAGED_DESKTOP_KEYS = (
    "browserExternalPageTools",
    "disableBrowserExternalNavigation",
    "disableDesktopLocalSessions",
    "disableMobileSimulatorTools",
    "sshHostAllowlist",
)


def test_desktop_only_keys_keep_the_scope_the_docs_give_them() -> None:
    # The docs say User or managed for sshConfigs and Managed for the other five.
    assert set(cws.DESKTOP_ONLY_KEYS) == {*_MANAGED_DESKTOP_KEYS, "sshConfigs"}
    assert cws.SCOPE_LIMITED_KEYS["sshConfigs"] == "user"
    assert cws.DESKTOP_ONLY_KEYS["sshConfigs"] == "user or managed settings"
    for key in _MANAGED_DESKTOP_KEYS:
        assert cws.SCOPE_LIMITED_KEYS[key] == "managed"
        assert cws.DESKTOP_ONLY_KEYS[key] == "managed settings"


@pytest.mark.parametrize("key", _MANAGED_DESKTOP_KEYS)
@pytest.mark.parametrize("scope", _SCOPES)
def test_managed_desktop_key_refusal_says_only_desktop_reads_it(
    tmp_path: Path, key: str, scope: str
) -> None:
    with pytest.raises(cw.InvalidCandidateError) as exc:
        _write(scope, tmp_path, {key: []})
    msg = str(exc.value)
    assert msg.startswith(f"Only Claude Desktop reads {key}, from managed settings, and the ")
    assert "Claude Code CLI ignores it." in msg
    assert "which clauster does not write; remove it." in msg


@pytest.mark.parametrize("scope", ["project", "local"])
def test_ssh_configs_saves_at_user_scope_only(tmp_path: Path, scope: str) -> None:
    configs = [{"id": "dev", "name": "Dev", "sshHost": "dev.example"}]
    _write("user", tmp_path, {"sshConfigs": configs})
    with pytest.raises(cw.InvalidCandidateError) as exc:
        _write(scope, tmp_path, {"sshConfigs": configs})
    msg = str(exc.value)
    assert msg.startswith("Only Claude Desktop reads sshConfigs, from user or managed settings")
    assert "switch to User scope" in msg


def test_desktop_only_keys_are_named_and_left_out_of_the_merged_view() -> None:
    user = {"sshConfigs": [{"id": "d"}], "model": "opus"}
    assert cws.desktop_only_keys(user) == ["sshConfigs"]
    assert cws.desktop_only_keys({"model": "opus"}) == []
    # A hand-edited project file can hold a managed one too; neither is a CLI value.
    merged = cws._compute_effective_settings(
        user_misc=user, project_misc={"sshHostAllowlist": ["*"]}, local_misc={}
    )
    assert merged == {"model": {"value": "opus", "source": "user", "sources": ["user"]}}


def test_settings_read_route_names_the_desktop_only_keys(
    write_config, tmp_path, projects_root
) -> None:
    fake = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"
    cfg = write_config(
        f"claude:\n  binary: {fake}\nstate_dir: {tmp_path}/.s\n"
        "config_write:\n  enabled: true\n  allow_user_scope: true\n"
    )
    _seed(
        cw.project_settings_path(projects_root / "alpha"),
        {"sshHostAllowlist": ["*"], "model": "opus"},
    )
    # A hand-edited local file can hold a managed one; the read still names it.
    _seed(
        projects_root / "alpha" / ".claude" / "settings.local.json",
        {"disableMobileSimulatorTools": True},
    )
    with TestClient(create_app(load_config(cfg))) as c:
        r = c.get("/api/config-write/settings?project=alpha")
        assert r.status_code == 200
        assert r.json()["desktop_only"] == ["sshHostAllowlist"]
        r2 = c.get("/api/config-write/settings?project=alpha&scope=local")
        assert r2.json()["desktop_only"] == ["disableMobileSimulatorTools"]
        # sshConfigs saves at User scope (under the isolated HOME), and the read names it.
        r3 = c.get("/api/config-write/settings?scope=user")
        put = c.put(
            "/api/config-write/settings",
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "settings": {"sshConfigs": [{"id": "dev", "name": "Dev", "sshHost": "dev"}]},
                "hash": r3.json()["hash"],
            },
        )
        assert put.status_code == 200
        assert c.get("/api/config-write/settings?scope=user").json()["desktop_only"] == [
            "sshConfigs"
        ]


def test_scope_limited_keys_do_not_overlap_the_carve() -> None:
    assert not {p.split(".")[0] for p in cws.SCOPE_LIMITED_KEYS} & cws.OWNED_KEYS


def test_user_only_refusal_names_user_scope(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError) as exc:
        cws.write_project_settings(tmp_path, {"skipAutoPermissionPrompt": True}, None)
    msg = str(exc.value)
    assert "only from user settings" in msg
    assert "ignores it in project settings (.claude/settings.json)" in msg
    assert "switch to User scope" in msg


def test_user_and_local_refusal_names_both_scopes(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="switch to User or Local scope"):
        cws.write_project_settings(tmp_path, {"useAutoModeDuringPlan": False}, None)
    cws.write_project_local_settings(tmp_path, {"useAutoModeDuringPlan": False}, None)


def test_managed_and_global_refusals_say_where_the_key_belongs(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="only from managed settings"):
        cws.write_project_settings(tmp_path, {"strictKnownMarketplaces": []}, None)
    with pytest.raises(cw.InvalidCandidateError, match=r"~/\.claude\.json \(global config\)"):
        cws.write_user_settings(tmp_path / "s.json", {"diffTool": "auto"}, None)


def test_refusal_lists_every_ignored_key(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError) as exc:
        cws.write_project_settings(tmp_path, {"spellcheck": {}, "modelPicker": {}}, None)
    assert "reads modelPicker only" in str(exc.value)
    assert "reads spellcheck only" in str(exc.value)


def test_nested_scope_limited_key_leaves_its_siblings_accepted(tmp_path: Path) -> None:
    cws.write_project_settings(tmp_path, {"sandbox": {"enabled": True}, "worktree": {}}, None)
    h = cw.hash_bytes(cw.project_settings_path(tmp_path).read_bytes())
    with pytest.raises(cw.InvalidCandidateError, match="sandbox.allowAppleEvents"):
        cws.write_project_settings(
            tmp_path, {"sandbox": {"enabled": True, "allowAppleEvents": True}}, h
        )


def test_a_file_already_holding_an_ignored_key_must_drop_it_to_save(tmp_path: Path) -> None:
    h = _seed(cw.project_settings_path(tmp_path), {"skipAutoPermissionPrompt": True})
    view, _ = cws.read_project_settings(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="skipAutoPermissionPrompt"):
        cws.write_project_settings(tmp_path, {**view, "model": "opus"}, h)
    cws.write_project_settings(tmp_path, {"model": "opus"}, h)  # removing it saves


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # Auto-mode and hook keys Claude Code reads from every settings file.
        ("disableAutoMode", "disable"),
        ("disableAllHooks", True),
        ("allowedHttpHookUrls", ["https://hooks.example/*"]),
        ("httpHookAllowedEnvVars", ["MY_TOKEN"]),
    ],
)
@pytest.mark.parametrize("scope", _SCOPES)
def test_any_file_auto_mode_and_hook_keys_pass_through(
    tmp_path: Path, key: str, value: Any, scope: str
) -> None:
    _write(scope, tmp_path, {key: value})


# The docs give these two keys User scope, but Claude Code 2.1.318 honors a false from
# project and local settings too: its Bash edit-diff gate returns off when the merged
# settings hold bashEditDiffEnabled: false, and its sandbox config takes
# allowNestedUserNamespaces: false from the merged settings. Only true is read from user,
# --settings and managed settings alone. So the key passes at every scope and only true
# is refused at Project and Local scope (TASK-490).
_FALSE_READ_EVERYWHERE = ("bashEditDiffEnabled", "sandbox.allowNestedUserNamespaces")


def test_value_level_scope_rules_are_exactly_the_two_keys() -> None:
    assert cws.SCOPE_LIMITED_VALUES == {path: (True, "user") for path in _FALSE_READ_EVERYWHERE}
    assert not set(cws.SCOPE_LIMITED_VALUES) & set(cws.SCOPE_LIMITED_KEYS)


@pytest.mark.parametrize("path", _FALSE_READ_EVERYWHERE)
@pytest.mark.parametrize("scope", _SCOPES)
def test_false_that_every_file_sets_saves_at_every_scope(
    tmp_path: Path, path: str, scope: str
) -> None:
    _write(scope, tmp_path, _nested(path, False))


@pytest.mark.parametrize("path", _FALSE_READ_EVERYWHERE)
@pytest.mark.parametrize("scope", _SCOPES)
def test_true_is_refused_exactly_where_claude_code_ignores_it(
    tmp_path: Path, path: str, scope: str
) -> None:
    if scope == "user":
        _write(scope, tmp_path, _nested(path, True))  # accepted
        return
    with pytest.raises(
        cw.InvalidCandidateError, match=f"Claude Code reads {path}: true only from user settings"
    ):
        _write(scope, tmp_path, _nested(path, True))
    assert not (tmp_path / ".claude").exists()


def test_a_file_already_holding_true_saves_once_it_is_false(tmp_path: Path) -> None:
    h = _seed(cw.project_settings_path(tmp_path), {"bashEditDiffEnabled": True})
    view, _ = cws.read_project_settings(tmp_path)
    with pytest.raises(cw.InvalidCandidateError) as exc:
        cws.write_project_settings(tmp_path, {**view, "model": "opus"}, h)
    assert "set it to false or remove it, or switch to User scope" in str(exc.value)
    cws.write_project_settings(tmp_path, {"bashEditDiffEnabled": False, "model": "opus"}, h)
    on_disk = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == {"bashEditDiffEnabled": False, "model": "opus"}


@pytest.mark.parametrize("path", _FALSE_READ_EVERYWHERE)
def test_value_rule_matches_the_json_type_only(tmp_path: Path, path: str) -> None:
    # 1 == True in Python, but a JSON 1 is not the boolean Claude Code compares with.
    _write("project", tmp_path, _nested(path, 1))


@pytest.mark.parametrize("path", _FALSE_READ_EVERYWHERE)
def test_project_false_is_part_of_the_merged_view(path: str) -> None:
    top = path.split(".")[0]
    merged = cws._compute_effective_settings(
        user_misc={}, project_misc=_nested(path, False), local_misc={}
    )
    assert merged[top] == {
        "value": _nested(path, False)[top],
        "source": "project",
        "sources": ["project"],
    }


@pytest.mark.parametrize("path", _FALSE_READ_EVERYWHERE)
def test_local_true_is_left_out_of_the_merged_view(path: str) -> None:
    # Claude Code applies the user's false here: a local true is read from no file but
    # user, --settings or managed settings.
    top = path.split(".")[0]
    merged = cws._compute_effective_settings(
        user_misc=_nested(path, False), project_misc={}, local_misc=_nested(path, True)
    )
    assert merged[top]["value"] == _nested(path, False)[top]
    assert merged[top]["sources"] == ["user"]


def test_route_refuses_an_ignored_key_with_its_message(write_config, tmp_path, projects_root):
    fake = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"
    cfg = write_config(
        f"claude:\n  binary: {fake}\nstate_dir: {tmp_path}/.s\nconfig_write:\n  enabled: true\n"
    )
    with TestClient(create_app(load_config(cfg))) as c:
        r0 = c.get("/api/config-write/settings?project=alpha")
        r = c.put(
            "/api/config-write/settings",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"skipAutoPermissionPrompt": True},
                "hash": r0.json()["hash"],
            },
        )
        assert r.status_code == 422
        assert "only from user settings" in r.json()["detail"]
        assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


# --- the merged view follows Claude Code's merge -------------------------------------


def _eff(**layers: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return cws._compute_effective_settings(
        user_misc=layers.get("user"),
        project_misc=layers.get("project"),
        local_misc=layers.get("local"),
    )


def test_effective_lists_combine_lower_first_without_repeats() -> None:
    eff = _eff(user={"claudeMdExcludes": ["a", "b"]}, local={"claudeMdExcludes": ["b", "c"]})
    assert eff["claudeMdExcludes"] == {
        "value": ["a", "b", "c"],
        "source": "local",
        "sources": ["local", "user"],
    }


def test_effective_list_dedupe_is_typed_and_never_collapses_masks() -> None:
    eff = _eff(user={"x": [1, S]}, project={"x": [True, S]})
    assert eff["x"]["value"] == [1, S, True, S]


def test_effective_list_dedupe_treats_int_and_float_as_one_number() -> None:
    eff = _eff(user={"x": [1]}, project={"x": [1.0, 2]})
    assert eff["x"]["value"] == [1, 2]


def test_effective_reads_the_marketplace_alias_as_the_canonical_key() -> None:
    eff = _eff(
        user={"additionalMarketplaces": {"m": {"a": 1}}},
        project={
            "extraKnownMarketplaces": {"n": {"b": 2}},
            "additionalMarketplaces": {"ignored": {}},
        },
    )
    assert "additionalMarketplaces" not in eff
    assert eff["extraKnownMarketplaces"]["value"] == {"m": {"a": 1}, "n": {"b": 2}}


def test_effective_never_mutates_its_inputs() -> None:
    project = {"sandbox": {"enabled": True, "ripgrep": {"c": 1}, "allowAppleEvents": True}}
    before = json.loads(json.dumps(project))
    eff = _eff(project=project)
    assert project == before
    assert eff["sandbox"]["value"] == {"enabled": True}


def test_effective_drops_an_object_whose_every_part_is_ignored() -> None:
    eff = _eff(user={"model": "m"}, project={"sandbox": {"ripgrep": {"c": 1}}})
    assert "sandbox" not in eff
    eff = _eff(project={"sandbox": {"credentials": {"awsPairs": []}, "enabled": True}})
    assert eff["sandbox"]["value"] == {"enabled": True}  # the emptied middle level goes too


def test_effective_null_marketplace_alias_is_dropped_and_fills_a_null_canonical() -> None:
    lower = {"extraKnownMarketplaces": {"m": {}}}
    eff = _eff(user=lower, project={"additionalMarketplaces": None})
    assert eff["extraKnownMarketplaces"]["value"] == {"m": {}}
    eff = _eff(project={"extraKnownMarketplaces": None, "additionalMarketplaces": {"n": {}}})
    assert eff["extraKnownMarketplaces"]["value"] == {"n": {}}


def test_effective_fallback_model_chain_is_replaced_whole() -> None:
    eff = _eff(user={"fallbackModel": ["a", "b"]}, project={"fallbackModel": ["c"]})
    assert eff["fallbackModel"] == {"value": ["c"], "source": "project", "sources": ["project"]}


def test_effective_marketplaces_merge_one_entry_deep() -> None:
    eff = _eff(
        user={"extraKnownMarketplaces": {"m": {"source": {"a": 1}}, "n": {}}},
        project={"extraKnownMarketplaces": {"m": {"source": {"b": 2}}}},
    )
    assert eff["extraKnownMarketplaces"]["value"] == {"m": {"source": {"b": 2}}, "n": {}}


def test_effective_nested_objects_merge_and_scalars_replace() -> None:
    eff = _eff(
        user={"sandbox": {"enabled": True, "network": {"allowedDomains": ["a"]}}},
        local={"sandbox": {"enabled": False, "network": {"allowedDomains": ["b"]}}},
    )
    assert eff["sandbox"]["value"] == {"enabled": False, "network": {"allowedDomains": ["a", "b"]}}


def test_effective_override_that_replaces_everything_names_one_scope() -> None:
    eff = _eff(user={"env": {"A": "1"}}, local={"env": {"A": "2"}})
    assert eff["env"] == {"value": {"A": "2"}, "source": "local", "sources": ["local"]}


# Sources name a scope only while one of its entries survives in the merged value: an
# object merges key by key, a list combines (an entry that two scopes repeat names both),
# and a scalar, fallbackModel or a type change replaces the lower value whole.


def test_effective_sources_drop_a_scope_whose_object_entries_were_all_overwritten() -> None:
    eff = _eff(
        user={"env": {"A": "one"}}, project={"env": {"B": "two"}}, local={"env": {"B": "three"}}
    )
    assert eff["env"] == {
        "value": {"A": "one", "B": "three"},
        "source": "local",
        "sources": ["local", "user"],
    }


def test_effective_sources_follow_the_leaves_of_a_nested_object() -> None:
    eff = _eff(
        user={"x": {"a": {"p": 1, "q": 1}}},
        project={"x": {"a": {"p": 2}}},
        local={"x": {"a": {"q": 3}}},
    )
    assert eff["x"] == {
        "value": {"a": {"p": 2, "q": 3}},
        "source": "local",
        "sources": ["local", "project"],
    }


def test_effective_sources_name_every_scope_whose_list_entries_survive() -> None:
    # Lists combine; "a" is in both files, so both supply it after the dedupe.
    eff = _eff(user={"claudeMdExcludes": ["a"]}, project={"claudeMdExcludes": ["a", "b"]})
    assert eff["claudeMdExcludes"] == {
        "value": ["a", "b"],
        "source": "project",
        "sources": ["project", "user"],
    }


def test_effective_sources_drop_a_list_that_a_scalar_or_chain_replaced() -> None:
    eff = _eff(user={"x": {"l": ["a"], "k": 1}}, local={"x": {"l": "s"}})
    assert eff["x"]["value"] == {"l": "s", "k": 1}
    assert eff["x"]["sources"] == ["local", "user"]  # user still supplies x.k
    eff = _eff(user={"x": {"l": ["a"]}}, local={"x": {"l": "s"}})
    assert eff["x"]["sources"] == ["local"]


def test_effective_source_is_the_highest_scope_that_still_supplies_a_value() -> None:
    # An empty object adds no entry, so the lower scope alone supplies the value.
    eff = _eff(user={"env": {"A": "1"}}, local={"env": {}})
    assert eff["env"] == {"value": {"A": "1"}, "source": "user", "sources": ["user"]}
    # When every scope is empty, the highest one is named.
    eff = _eff(user={"env": {}}, project={"env": {}})
    assert eff["env"] == {"value": {}, "source": "project", "sources": ["project"]}


def test_effective_shallow_merge_key_replaces_a_whole_entry() -> None:
    eff = _eff(
        user={"extraKnownMarketplaces": {"m": {"a": 1}, "n": {}}},
        project={"extraKnownMarketplaces": {"m": {"b": 2}}},
    )
    assert eff["extraKnownMarketplaces"]["sources"] == ["project", "user"]  # user keeps "n"
    eff = _eff(
        user={"extraKnownMarketplaces": {"m": {"a": 1}}},
        project={"extraKnownMarketplaces": {"m": {"b": 2}}},
    )
    assert eff["extraKnownMarketplaces"]["sources"] == ["project"]


def test_effective_scalar_over_object_replaces_it() -> None:
    eff = _eff(user={"statusLine": {"type": "command"}}, project={"statusLine": "x"})
    assert eff["statusLine"]["value"] == "x"


def test_effective_leaves_out_a_key_ignored_at_its_scope() -> None:
    # modelPicker is read from user settings only, so a project value never applies.
    eff = _eff(user={"modelPicker": {"u": 1}}, project={"modelPicker": {"p": 1}, "model": "x"})
    assert eff["modelPicker"] == {"value": {"u": 1}, "source": "user", "sources": ["user"]}
    eff = _eff(project={"sandbox": {"enabled": True, "ripgrep": {"command": "rg"}}})
    assert eff["sandbox"]["value"] == {"enabled": True}
