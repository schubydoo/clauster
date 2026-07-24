"""Hooks config-write surface (#690) over the #347 Foundation.

Covers the pure structural validator (validate-NEVER-execute — a hook ``command`` is
never resolved/spawned/parsed/run, only its shape is checked, and only ``type:
"command"`` is accepted), the project/user read+write routers (both write a real
``settings.json``), and the gated routes (capability/scope 404, runner-missing 404,
type-the-name 400, bad-shape/unknown-event/non-command-type 422 writing nothing,
stale-hash 409, path-escape 400, corrupt-file 422, sibling-key preservation).

The RCE-negative test proves the invariant that makes this code-executing surface
safe: validating a hook whose ``command`` would create a marker file MUST NOT create
it — the validator stores the command as inert data and never executes it.

Every test that touches ``~/.claude/settings.json`` runs under the autouse
HOME-isolation fixture and writes only into the isolated tmp home — the live account
is never touched.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_hooks as hooks
from clauster.app import create_app
from clauster.config import load_config

# A structurally valid command-hook matcher group, reused across tests.
_GROUP = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}

# --- structural validator: accept the valid shapes ---------------------------------


def test_validate_accepts_minimal_command_hook() -> None:
    hooks.validate_hooks({"PreToolUse": [_GROUP]})  # no raise


def test_validate_accepts_omitted_matcher() -> None:
    # matcher is optional (match-all); a group without it is valid.
    hooks.validate_hooks({"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]})


def test_validate_accepts_timeout_int() -> None:
    hooks.validate_hooks(
        {"PostToolUse": [{"hooks": [{"type": "command", "command": "x", "timeout": 30}]}]}
    )


def test_validate_accepts_empty_block() -> None:
    hooks.validate_hooks({})  # clearing all hooks is valid


def test_validate_accepts_multiple_events_and_groups() -> None:
    hooks.validate_hooks(
        {
            "PreToolUse": [_GROUP, {"hooks": [{"type": "command", "command": "y", "timeout": 5}]}],
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "z"}]}],
        }
    )


@pytest.mark.parametrize("event", sorted(hooks.RECOGNIZED_EVENTS))
def test_validate_accepts_every_recognized_event(event: str) -> None:
    hooks.validate_hooks({event: [_GROUP]})


# --- structural validator: reject the bad shapes (→ InvalidCandidateError / 422) ----


@pytest.mark.parametrize(
    "candidate",
    [
        ["not", "a", "dict"],
        {"NotAnEvent": [_GROUP]},  # unknown event key
        {"PreToolUse": _GROUP},  # event value must be a list, not a dict
        {"PreToolUse": ["not-a-group"]},  # group not an object
        {"PreToolUse": [{"hooks": []}]},  # empty hooks list
        {"PreToolUse": [{"hooks": "nope"}]},  # hooks not a list
        {"PreToolUse": [{"matcher": 5, "hooks": [{"type": "command", "command": "x"}]}]},  # int
        {"PreToolUse": [{"bogus": 1, "hooks": [{"type": "command", "command": "x"}]}]},  # unknown
        {"PreToolUse": [{"hooks": ["not-an-object"]}]},  # entry not an object
        {"PreToolUse": [{"hooks": [{"command": "x"}]}]},  # missing type
        {"PreToolUse": [{"hooks": [{"type": "command"}]}]},  # missing command
        {"PreToolUse": [{"hooks": [{"type": "command", "command": ""}]}]},  # empty command
        {"PreToolUse": [{"hooks": [{"type": "command", "command": 5}]}]},  # command not a string
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "extra": 1}]}]},  # unknown
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "timeout": "30"}]}]},  # str
        {"PreToolUse": [{"hooks": [{"type": "command", "command": "x", "timeout": True}]}]},  # bl
    ],
)
def test_validate_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        hooks.validate_hooks(candidate)


@pytest.mark.parametrize("bad_type", ["prompt", "agent", "http", "mcp_tool", "Command", "", None])
def test_validate_rejects_non_command_type(bad_type: object) -> None:
    """Only ``type: "command"`` is managed by this surface; everything else is rejected."""
    entry = {"command": "x"} if bad_type is None else {"type": bad_type, "command": "x"}
    with pytest.raises(cw.InvalidCandidateError):
        hooks.validate_hooks({"PreToolUse": [{"hooks": [entry]}]})


# --- plugin hooks are read-only (#770): a plugin-owned command is rejected ----------


@pytest.mark.parametrize(
    "command",
    [
        '"${CLAUDE_PLUGIN_ROOT}"/scripts/format.sh',
        "${CLAUDE_PLUGIN_ROOT}/scripts/format.sh",
        "$CLAUDE_PLUGIN_ROOT/scripts/format.sh",
        "echo CLAUDE_PLUGIN_ROOT",  # bare substring, no interpolation syntax at all
    ],
)
def test_validate_rejects_plugin_root_marker(command: str) -> None:
    """A command referencing the plugin-root marker is plugin-owned, not editable here."""
    candidate = {"PostToolUse": [{"hooks": [{"type": "command", "command": command}]}]}
    with pytest.raises(cw.InvalidCandidateError, match="plugin"):
        hooks.validate_hooks(candidate)


def test_write_rejects_plugin_root_marker_writes_nothing(tmp_path: Path) -> None:
    """The write path re-runs validate_hooks, so a plugin-shaped candidate writes nothing."""
    candidate = {
        "PostToolUse": [
            {
                "matcher": "Write|Edit",
                "hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/x.sh"}],
            }
        ]
    }
    with pytest.raises(cw.InvalidCandidateError):
        hooks.write_project_hooks(tmp_path, candidate, expected_hash=None)
    assert not (tmp_path / ".claude").exists()


def test_local_write_rejects_plugin_root_marker(tmp_path: Path) -> None:
    """The local-scope writer applies the same plugin-owned-command guard."""
    candidate = {
        "Stop": [{"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/x.sh"}]}]
    }
    with pytest.raises(cw.InvalidCandidateError):
        hooks.write_project_local_hooks(tmp_path, candidate, expected_hash=None)
    assert not (tmp_path / ".claude").exists()


def test_user_write_rejects_plugin_root_marker(tmp_path: Path) -> None:
    """The user-scope writer applies the same plugin-owned-command guard."""
    settings = tmp_path / ".claude" / "settings.json"
    candidate = {
        "Stop": [{"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/x.sh"}]}]
    }
    with pytest.raises(cw.InvalidCandidateError):
        hooks.write_user_hooks(settings, candidate, expected_hash=None)
    assert not settings.exists()


def test_route_write_rejects_plugin_root_marker_is_422(
    write_config, tmp_path, projects_root
) -> None:
    """The full HTTP write path also rejects a plugin-owned command (422, nothing written)."""
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {
                    "PostToolUse": [
                        {"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/x.sh"}]}
                    ]
                },
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


# --- validate-never-execute: a command string is stored verbatim, never run --------


def test_write_stores_command_verbatim_never_parsed(tmp_path: Path) -> None:
    candidate = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rm -rf /"}]}]
    }
    hooks.validate_hooks(candidate)
    hooks.write_project_hooks(tmp_path, candidate, expected_hash=cw.hash_bytes(b""))
    stored = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "rm -rf /"


def test_validate_never_executes_the_command(tmp_path: Path) -> None:
    """RCE-NEGATIVE: a command that WOULD create a marker file must NOT create it.

    This is the load-bearing security property of the whole surface. We point a hook's
    ``command`` at a real shell command that, *if executed*, creates a sentinel marker
    file. Validating — and writing — the hook must leave that marker absent: the
    command is inert data, stored but never resolved, spawned, shell-parsed, or run.
    The marker only ever appears if the validator (or the writer) executed the string,
    which would be the browser→host RCE the gate exists to prevent.
    """
    marker = tmp_path / "PWNED"
    # A command that, if ever run through a shell, would create the marker file. The path
    # is shell-quoted so the proof stays valid even on a tmp path with spaces/metacharacters
    # (an unquoted path would make the sentinel a no-op and the assertions below vacuous).
    pwn = f"touch {shlex.quote(str(marker))}"
    candidate = {"PreToolUse": [{"hooks": [{"type": "command", "command": pwn}]}]}

    # 1) pure validation does not execute it
    hooks.validate_hooks(candidate)
    assert not marker.exists()

    # 2) the full write path (validate → mkdir → locked atomic replace) does not either
    hooks.write_project_hooks(tmp_path, candidate, expected_hash=cw.hash_bytes(b""))
    assert not marker.exists()

    # 3) the command landed verbatim as inert data — prove the sentinel itself is sound
    #    (a real interpreter CAN create the marker) so the assertions above are meaningful,
    #    not vacuous. We spawn the marker via the Python interpreter rather than a shell so
    #    the proof is portable (Windows has no `sh`); it asserts nothing about HOW the
    #    stored `command` would run — only that a marker at this path is genuinely creatable.
    stored = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == pwn
    make_marker = "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()"
    subprocess.run(  # noqa: S603 - prove the marker CAN be made (portable, no shell)
        [sys.executable, "-c", make_marker, str(marker)],
        check=True,
    )
    assert marker.exists()  # the marker is genuinely creatable; the validator never ran it


# --- project read/write round-trip + stale-hash ------------------------------------


def test_project_write_then_read_round_trip(tmp_path: Path) -> None:
    block, h0 = hooks.read_project_hooks(tmp_path)
    assert block == {}
    hooks.write_project_hooks(tmp_path, {"SessionStart": [_GROUP]}, expected_hash=h0)
    block, _h1 = hooks.read_project_hooks(tmp_path)
    assert block == {"SessionStart": [_GROUP]}


def test_project_write_preserves_sibling_keys(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {"hooks": {"Stop": [_GROUP]}, "permissions": {"allow": ["a"]}, "model": "opus"}
        ),
        encoding="utf-8",
    )
    _b, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, {"PreToolUse": [_GROUP]}, h)
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out["permissions"] == {"allow": ["a"]}  # untouched sibling preserved
    assert out["model"] == "opus"  # untouched sibling preserved
    assert out["hooks"] == {"PreToolUse": [_GROUP]}  # subtree replaced wholesale


def test_project_write_stale_hash_raises(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    stale = cw.hash_bytes(b'{"hooks": {"Stop": []}}')
    with pytest.raises(cw.StaleConfigWriteError):
        hooks.write_project_hooks(tmp_path, {"PreToolUse": [_GROUP]}, stale)


def test_project_write_no_hash_on_existing_file_is_stale(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    with pytest.raises(cw.StaleConfigWriteError):
        hooks.write_project_hooks(tmp_path, {"PreToolUse": [_GROUP]}, expected_hash=None)


def test_project_write_no_hash_on_absent_file_ok(tmp_path: Path) -> None:
    hooks.write_project_hooks(tmp_path, {"PreToolUse": [_GROUP]}, expected_hash=None)
    out = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert out["hooks"] == {"PreToolUse": [_GROUP]}


def test_project_read_rejects_non_object_file(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        hooks.read_project_hooks(tmp_path)


def test_project_read_rejects_malformed_json(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        hooks.read_project_hooks(tmp_path)


def test_project_write_bad_shape_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        hooks.write_project_hooks(tmp_path, {"NotAnEvent": [_GROUP]}, expected_hash=None)
    # Validation precedes the mkdir, so even the .claude dir is not created.
    assert not (tmp_path / ".claude").exists()


# --- user-scope writer (HOME-isolated, explicit tmp file) --------------------------


def test_user_write_preserves_other_keys(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "sonnet", "hooks": {"Stop": [_GROUP]}}))
    _b, h = hooks.read_user_hooks(settings)
    hooks.write_user_hooks(settings, {"PreToolUse": [_GROUP]}, h)
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out["model"] == "sonnet"  # sibling preserved
    assert out["hooks"] == {"PreToolUse": [_GROUP]}


def test_user_read_missing_file_is_empty(tmp_path: Path) -> None:
    block, h = hooks.read_user_hooks(tmp_path / "absent" / "settings.json")
    assert block == {}
    assert h == cw.hash_bytes(b"")


def test_user_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"  # absent
    with pytest.raises(cw.InvalidCandidateError):
        hooks.write_user_hooks(settings, {"PreToolUse": [{"hooks": [{"type": "agent"}]}]}, None)
    assert not settings.exists()  # nothing written on a validation failure


# --- local-scope writer (settings.local.json, gitignore-on-create) -----------------


def test_local_write_then_read_round_trip(tmp_path: Path) -> None:
    _b, h0 = hooks.read_project_local_hooks(tmp_path)
    assert _b == {}
    hooks.write_project_local_hooks(tmp_path, {"Stop": [_GROUP]}, expected_hash=h0)
    block, _h1 = hooks.read_project_local_hooks(tmp_path)
    assert block == {"Stop": [_GROUP]}


def test_local_write_targets_settings_local_json_not_settings_json(tmp_path: Path) -> None:
    hooks.write_project_local_hooks(tmp_path, {"Stop": [_GROUP]}, expected_hash=None)
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_local_write_creates_gitignore_entry(tmp_path: Path) -> None:
    hooks.write_project_local_hooks(tmp_path, {"Stop": [_GROUP]}, expected_hash=None)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore


def test_local_write_gitignore_idempotent_across_writes(tmp_path: Path) -> None:
    _b, h0 = hooks.read_project_local_hooks(tmp_path)
    hooks.write_project_local_hooks(tmp_path, {"Stop": [_GROUP]}, expected_hash=h0)
    _b1, h1 = hooks.read_project_local_hooks(tmp_path)
    hooks.write_project_local_hooks(tmp_path, {"PreToolUse": [_GROUP]}, expected_hash=h1)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # Count exact lines, not substrings: the ``.bak`` sibling entry (#F6) contains
    # ``.claude/settings.local.json`` as a substring, so the base entry is asserted
    # unduplicated by line, not by substring occurrence.
    assert gitignore.splitlines().count(".claude/settings.local.json") == 1


def test_local_write_bad_shape_writes_nothing_and_no_gitignore(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        hooks.write_project_local_hooks(
            tmp_path, {"PreToolUse": [{"hooks": [{"type": "agent"}]}]}, expected_hash=None
        )
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".gitignore").exists()  # validation failure never touches gitignore


def test_local_write_stale_hash_raises(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {}}))
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        hooks.write_project_local_hooks(tmp_path, {"Stop": [_GROUP]}, stale)


def test_local_scope_is_independent_of_project_scope_file(tmp_path: Path) -> None:
    hooks.write_project_hooks(tmp_path, {"Stop": [_GROUP]}, expected_hash=None)
    hooks.write_project_local_hooks(tmp_path, {"PreToolUse": [_GROUP]}, expected_hash=None)
    project_block, _ = hooks.read_project_hooks(tmp_path)
    local_block, _ = hooks.read_project_local_hooks(tmp_path)
    assert project_block == {"Stop": [_GROUP]}
    assert local_block == {"PreToolUse": [_GROUP]}


def test_local_write_never_executes_command(tmp_path: Path) -> None:
    marker = tmp_path / "MARKER_SHOULD_NOT_EXIST"
    evil = {"Stop": [{"hooks": [{"type": "command", "command": f"touch {marker}"}]}]}
    hooks.write_project_local_hooks(tmp_path, evil, expected_hash=None)
    assert not marker.exists(), "local-scope write executed the command (RCE!)"
    stored, _ = hooks.read_project_local_hooks(tmp_path)
    assert stored["Stop"][0]["hooks"][0]["command"] == f"touch {marker}"


# --- gated routes (full FastAPI lifespan) ------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path, extra: str) -> TestClient:
    # The autouse HOME-isolation fixture pins HOME to an isolated tmp dir, so the
    # user-scope settings path resolves under <isolated-home>/.claude/settings.json —
    # the real account is never touched by the user-scope routes.
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"

_URL = "/api/config-write/hooks"


def test_route_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 404
        assert (
            c.put(
                _URL,
                json={"scope": "project", "project": "alpha", "confirm": "alpha", "hooks": {}},
            ).status_code
            == 404
        )


def test_route_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get(f"{_URL}?scope=user").status_code == 404
        assert (
            c.put(
                _URL,
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "hooks": {}},
            ).status_code
            == 404
        )


def test_route_user_scope_404_when_runner_missing(write_config, tmp_path) -> None:
    # Fail-closed guard: user scope is enabled but no runner is wired (app.state.runner
    # is None). The user-scope settings path can't be resolved, so GET/PUT must return
    # the 404-invisible shape — NEVER an unhandled 500 from runner.claude_json.
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    app = create_app(load_config(cfg))
    with TestClient(app) as c:
        app.state.runner = None  # simulate an app started without a runner
        assert c.get(f"{_URL}?scope=user").status_code == 404
        assert (
            c.put(
                _URL,
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "hooks": {}},
            ).status_code
            == 404
        )


def test_route_confirm_mismatch_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "hooks": {"PreToolUse": [_GROUP]},
            },
        )
        assert resp.status_code == 400


def test_route_bad_shape_is_422_and_writes_nothing(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"PreToolUse": [{"hooks": [{"type": "command"}]}]},  # missing command
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_unknown_event_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"NotAnEvent": [_GROUP]},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_non_command_type_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    """A non-``command`` hook type (e.g. ``prompt``) is rejected (422), nothing written."""
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"Stop": [{"hooks": [{"type": "prompt", "command": "x"}]}]},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "hooks": {},
            },
        )
        assert resp.status_code == 400
        assert c.get(f"{_URL}?project=../escape").status_code == 400


def test_route_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"PreToolUse": [_GROUP]},
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_no_hash_on_existing_file_is_409(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"PreToolUse": [_GROUP]},
                # no "hash" — must not silently overwrite an existing file
            },
        )
        assert resp.status_code == 409


def test_route_project_write_read_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get(f"{_URL}?project=alpha")
        assert read0.status_code == 200
        assert read0.json()["hooks"] == {}
        h0 = read0.json()["hash"]
        wr = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"PreToolUse": [_GROUP]},
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_URL}?project=alpha")
        assert read1.json()["hooks"] == {"PreToolUse": [_GROUP]}


def test_route_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=bogus").status_code == 422
        assert c.put(_URL, json={"scope": "bogus", "hooks": {}}).status_code == 422


def test_route_user_write_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get(f"{_URL}?scope=user")
        assert read0.status_code == 200
        h0 = read0.json()["hash"]
        wr = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "hooks": {"SessionStart": [_GROUP]},
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read = c.get(f"{_URL}?scope=user")
        assert read.json()["hooks"] == {"SessionStart": [_GROUP]}
    # The write landed in the ISOLATED home (autouse fixture), never the real account,
    # and in ~/.claude/settings.json — NOT ~/.claude.json.
    isolated = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    assert out["hooks"] == {"SessionStart": [_GROUP]}
    # The write must NOT have landed in ~/.claude.json (that file is the trust store).
    claude_json = Path(os.environ["HOME"]) / ".claude.json"
    if claude_json.exists():
        assert "hooks" not in json.loads(claude_json.read_text(encoding="utf-8"))


# --- route-level read/error guards (corrupt files → 422 on BOTH scopes) -------------


def test_route_read_corrupt_project_file_is_422(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 422


def test_route_read_non_object_project_file_is_422(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[1, 2, 3]", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 422


def test_route_read_non_utf8_project_file_is_422(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"\xff\xfe not utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 422


def test_route_read_corrupt_user_file_is_422(write_config, tmp_path) -> None:
    # The user-scope read guard: a hand-edited ~/.claude/settings.json that is not valid
    # JSON must surface as a clean 422, never an unhandled 500.
    user_settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    user_settings.parent.mkdir(parents=True, exist_ok=True)
    user_settings.write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=user").status_code == 422


def test_route_write_over_non_utf8_project_file_is_422(
    write_config, tmp_path, projects_root
) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"\xff\xfe not utf-8")
    file_hash = cw.hash_bytes(b"\xff\xfe not utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"PreToolUse": [_GROUP]},
                "hash": file_hash,
            },
        )
        assert resp.status_code == 422


def test_route_write_missing_project_dir_is_404(write_config, tmp_path, projects_root) -> None:
    absent = projects_root / "noexist"
    assert not absent.exists()
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "hooks": {"PreToolUse": [_GROUP]},
            },
        )
        assert resp.status_code == 404
    assert not (absent / ".claude").exists()  # nothing written


def test_route_confirm_runs_before_validate(write_config, tmp_path, projects_root) -> None:
    # Ordering: the type-the-name confirm is the FIRST semantic gate after capability. A
    # request that BOTH omits a valid confirm AND carries a malformed `hooks` must fail
    # at the confirm gate (400), never reach the structural validator (422).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "hooks": "not-a-dict",
            },
        )
        assert resp.status_code == 400
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(_URL, json={"scope": "user", "confirm": "WRONG", "hooks": 123})
        assert resp.status_code == 400


def test_route_missing_hooks_is_422_after_valid_confirm(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(_URL, json={"scope": "project", "project": "alpha", "confirm": "alpha"})
        assert resp.status_code == 422


def test_route_user_missing_hooks_is_422_after_valid_confirm(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(_URL, json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN})
        assert resp.status_code == 422


def test_route_read_empty_project_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=").status_code == 422


def test_route_project_non_string_hash_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"PreToolUse": [_GROUP]},
                "hash": 123,  # not a string
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_user_write_stale_hash_is_409(write_config, tmp_path) -> None:
    # A user-scope write whose echoed hash no longer matches the on-disk file raises a
    # StaleConfigWriteError inside write_user_hooks; the route must map it to a 409 via
    # _map_config_write_error (the user-scope write error path), never an unhandled 500.
    user_settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    user_settings.parent.mkdir(parents=True, exist_ok=True)
    user_settings.write_text('{"hooks": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "hooks": {"PreToolUse": [_GROUP]},
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_user_write_bad_shape_is_422(write_config, tmp_path) -> None:
    # A user-scope write with a structurally invalid hooks block raises
    # InvalidCandidateError inside write_user_hooks (validate_candidate), which the route
    # maps to 422 via _map_config_write_error — the user-scope write error path.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "hooks": {"NotAnEvent": [_GROUP]},
            },
        )
        assert resp.status_code == 422


def test_route_user_non_string_hash_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "hooks": {"PreToolUse": [_GROUP]},
                "hash": 123,
            },
        )
        assert resp.status_code == 422


def test_route_project_missing_name_is_400(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={"scope": "project", "confirm": "", "hooks": {"PreToolUse": [_GROUP]}},
        )
        assert resp.status_code == 400


def test_route_validate_never_executes_command(write_config, tmp_path, projects_root) -> None:
    """RCE-NEGATIVE at the ROUTE: a hook whose command would touch a marker must not.

    Drive the full HTTP write path with a command that, if executed, creates a sentinel
    file. After a successful 200 write the marker MUST be absent — the route stores the
    command as inert data and never resolves/spawns/runs it.
    """
    marker = tmp_path / "ROUTE_PWNED"
    pwn = f"touch {marker}"
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": pwn}]}]},
            },
        )
        assert resp.status_code == 200
    assert not marker.exists()  # the command was stored, never executed
    stored = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert stored["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == pwn


# --- local scope routes (own confirm token, project-only gate, gitignore-on-create) -


def test_route_local_scope_works_without_allow_user_scope(
    write_config, tmp_path, projects_root
) -> None:
    # Local scope needs no allow_user_scope opt-in — the base `enabled` flag alone
    # gates it, same as project scope (#766 scope decision).
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        read0 = c.get(f"{_URL}?scope=local&project=alpha")
        assert read0.status_code == 200
        body = read0.json()
        assert body["scope"] == "local"
        assert body["project"] == "alpha"
        assert body["hooks"] == {}
        assert body["hash"] == cw.hash_bytes(b"")
        wr = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "hooks": {"Stop": [_GROUP]},
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_URL}?scope=local&project=alpha")
        assert read1.json()["hooks"] == {"Stop": [_GROUP]}


def test_route_local_confirm_rejects_plain_project_name(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha",  # the PROJECT-scope token, not the local one
                "hooks": {"Stop": [_GROUP]},
            },
        )
        assert resp.status_code == 400


def test_route_local_write_creates_gitignore_entry(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        wr = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "hooks": {"Stop": [_GROUP]},
            },
        )
        assert wr.status_code == 200
    gitignore = (projects_root / "alpha" / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore


def test_route_local_scope_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_URL}?scope=local&project=alpha").status_code == 404
        assert (
            c.put(
                _URL,
                json={
                    "scope": "local",
                    "project": "alpha",
                    "confirm": "alpha (local)",
                    "hooks": {},
                },
            ).status_code
            == 404
        )


def test_route_local_write_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "noexist",
                "confirm": "noexist (local)",
                "hooks": {"Stop": [_GROUP]},
            },
        )
        assert resp.status_code == 404


def test_route_local_write_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "../escape",
                "confirm": "../escape (local)",
                "hooks": {},
            },
        )
        assert resp.status_code == 400
        assert c.get(f"{_URL}?scope=local&project=../escape").status_code == 400


def test_route_local_write_bad_shape_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "hooks": {"PreToolUse": [{"hooks": [{"type": "agent"}]}]},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.local.json").exists()


def test_route_local_write_never_executes_command(write_config, tmp_path, projects_root) -> None:
    marker = tmp_path / "LOCAL_ROUTE_PWNED"
    pwn = f"touch {marker}"
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": pwn}]}]},
            },
        )
        assert resp.status_code == 200
    assert not marker.exists()  # the command was stored, never executed
    stored = json.loads(
        (projects_root / "alpha" / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert stored["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == pwn


def test_route_local_read_corrupt_file_is_422(write_config, tmp_path, projects_root) -> None:
    # The local-scope read guard: a hand-edited settings.local.json that is not valid
    # JSON must surface as a clean 422 from the GET route, never an unhandled 500 (same
    # guard as the project/user reads above).
    settings = projects_root / "alpha" / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=local&project=alpha").status_code == 422
