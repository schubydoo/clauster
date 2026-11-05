"""Permission-rules config-write surface (#689) over the #347 Foundation.

Covers the pure structural validator (validate-never-execute — a rule string is never
parsed/run, and ``bypassPermissions`` can never be set as ``defaultMode``), the
project/user read+write routers (both write a real ``settings.json``), and the gated
routes (capability/scope 404, type-the-name 400, bad-shape/unknown-mode 422 writing
nothing, stale-hash 409, path-escape 400, corrupt-file 422, sibling-key preservation).

Every test that touches ``~/.claude/settings.json`` runs under the autouse
HOME-isolation fixture and writes only into the isolated tmp home — the live account
is never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_permissions as perms
from clauster.app import create_app
from clauster.config import PERMISSION_LABELS, load_config

# --- structural validator: accept the valid shapes ---------------------------------


def test_validate_accepts_allow_deny_lists() -> None:
    perms.validate_permissions({"allow": ["Bash(ls:*)"], "deny": ["Bash(rm:*)"]})  # no raise


def test_validate_accepts_ask_list() -> None:
    # `ask` is the third canonical decision bucket — same opaque-string shape as
    # allow/deny; the config editor's Permissions rows write it.
    perms.validate_permissions({"ask": ["Bash(git push:*)"]})  # no raise


def test_validate_accepts_default_mode() -> None:
    perms.validate_permissions({"defaultMode": "plan"})
    perms.validate_permissions({"defaultMode": "acceptEdits"})


def test_validate_accepts_empty_block() -> None:
    perms.validate_permissions({})  # clearing all rules is valid


def test_validate_accepts_full_block() -> None:
    perms.validate_permissions(
        {
            "allow": ["Read(*)"],
            "deny": ["Bash(curl:*)"],
            "ask": ["Bash(git push:*)"],
            "defaultMode": "default",
        }
    )


@pytest.mark.parametrize("mode", sorted(perms.RECOGNIZED_MODES))
def test_validate_accepts_every_recognized_mode(mode: str) -> None:
    perms.validate_permissions({"defaultMode": mode})


# --- structural validator: reject the bad shapes (→ InvalidCandidateError / 422) ----


@pytest.mark.parametrize(
    "candidate",
    [
        ["not", "a", "dict"],
        {"allow": "not-a-list"},
        {"deny": "not-a-list"},
        {"allow": [1, 2]},  # rule not a string
        {"allow": [""]},  # empty rule string
        {"deny": [None]},  # rule not a string
        {"ask": "not-a-list"},  # ask must be a list
        {"ask": [1]},  # ask rule not a string
        {"ask": [""]},  # empty ask rule string
        {"defaultMode": 5},  # mode not a string
        {"defaultMode": "nonsense"},  # unknown mode
        {"bogus": 1},  # unknown key
        {"allow": ["ok"], "extra": 2},  # one unknown key among valid ones
    ],
)
def test_validate_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        perms.validate_permissions(candidate)


# --- bypassPermissions can NEVER be set through this surface ------------------------


def test_validate_rejects_bypass_default_mode() -> None:
    """``bypassPermissions`` as ``defaultMode`` is rejected — it stays footgun-gated."""
    with pytest.raises(cw.InvalidCandidateError):
        perms.validate_permissions({"defaultMode": "bypassPermissions"})


def test_validate_rejects_inherit_default_mode() -> None:
    """#1231: ``inherit`` is a Clauster launch sentinel, never a claude settings value.

    It means "pass no ``--permission-mode`` flag"; writing it into a real
    ``settings.json`` would produce a ``defaultMode`` claude does not understand.
    """
    with pytest.raises(cw.InvalidCandidateError):
        perms.validate_permissions({"defaultMode": "inherit"})


def test_bypass_excluded_from_recognized_modes() -> None:
    assert "bypassPermissions" not in perms.RECOGNIZED_MODES
    # Assert the RELATIONSHIP to the source of truth, not a frozen copy: the recognized
    # set is exactly the canonical permission-label vocabulary minus the footgun mode and
    # the "inherit" launch sentinel (#1231, which is not a settings.json value at all),
    # so adding a label to PERMISSION_LABELS tracks here instead of failing on a stale
    # literal set (same brittleness class flagged on #731).
    assert perms.RECOGNIZED_MODES == frozenset(PERMISSION_LABELS) - {
        "bypassPermissions",
        "inherit",
    }


def test_bypass_as_rule_string_is_inert_data(tmp_path: Path) -> None:
    """The literal string elsewhere is harmless inert data — only the MODE is gated."""
    # A rule that mentions the word is just an opaque allow-rule, not a mode switch.
    perms.write_project_permissions(
        tmp_path,
        {"allow": ["Bash(echo bypassPermissions)"]},
        expected_hash=cw.hash_bytes(b""),
        auto_mode=None,
    )
    stored = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["permissions"]["allow"] == ["Bash(echo bypassPermissions)"]
    assert "defaultMode" not in stored["permissions"]


# --- validate-never-execute: a rule string is stored verbatim, never run -----------


def test_write_stores_rule_verbatim_never_parsed(tmp_path: Path) -> None:
    candidate = {"deny": ["Bash(rm -rf /:*)"], "allow": ["Read(/etc/passwd)"]}
    perms.validate_permissions(candidate)
    perms.write_project_permissions(
        tmp_path, candidate, expected_hash=cw.hash_bytes(b""), auto_mode=None
    )
    stored = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["permissions"]["deny"] == ["Bash(rm -rf /:*)"]
    assert stored["permissions"]["allow"] == ["Read(/etc/passwd)"]


def test_write_stores_ask_rules_verbatim(tmp_path: Path) -> None:
    """An `ask` bucket round-trips like allow/deny — stored verbatim, never parsed."""
    candidate = {"ask": ["Bash(git push:*)", "Write(/etc/*)"]}
    perms.validate_permissions(candidate)
    perms.write_project_permissions(
        tmp_path, candidate, expected_hash=cw.hash_bytes(b""), auto_mode=None
    )
    stored = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["permissions"]["ask"] == ["Bash(git push:*)", "Write(/etc/*)"]


# --- project read/write round-trip + stale-hash ------------------------------------


def test_project_write_then_read_round_trip(tmp_path: Path) -> None:
    block, _am, h0 = perms.read_project_permissions(tmp_path)
    assert block == {}
    perms.write_project_permissions(
        tmp_path, {"defaultMode": "plan"}, expected_hash=h0, auto_mode=None
    )
    block, _am, _h1 = perms.read_project_permissions(tmp_path)
    assert block == {"defaultMode": "plan"}


def test_project_write_preserves_sibling_keys(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"permissions": {"allow": ["a"]}, "hooks": {"X": 1}, "model": "opus"}),
        encoding="utf-8",
    )
    _b, _am, h = perms.read_project_permissions(tmp_path)
    perms.write_project_permissions(tmp_path, {"deny": ["b"]}, h, auto_mode=None)
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out["hooks"] == {"X": 1}  # untouched sibling preserved
    assert out["model"] == "opus"  # untouched sibling preserved
    assert out["permissions"] == {"deny": ["b"]}  # subtree replaced wholesale


def test_project_write_stale_hash_raises(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions": {}}', encoding="utf-8")
    stale = cw.hash_bytes(b'{"permissions": {"allow": ["old"]}}')
    with pytest.raises(cw.StaleConfigWriteError):
        perms.write_project_permissions(tmp_path, {"deny": ["new"]}, stale, auto_mode=None)


def test_project_write_no_hash_on_existing_file_is_stale(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions": {}}', encoding="utf-8")
    with pytest.raises(cw.StaleConfigWriteError):
        perms.write_project_permissions(
            tmp_path, {"allow": ["x"]}, expected_hash=None, auto_mode=None
        )


def test_project_write_no_hash_on_absent_file_ok(tmp_path: Path) -> None:
    perms.write_project_permissions(tmp_path, {"allow": ["x"]}, expected_hash=None, auto_mode=None)
    out = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert out["permissions"] == {"allow": ["x"]}


def test_project_read_rejects_non_object_file(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        perms.read_project_permissions(tmp_path)


def test_project_read_rejects_malformed_json(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        perms.read_project_permissions(tmp_path)


def test_project_write_bad_shape_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        perms.write_project_permissions(
            tmp_path, {"defaultMode": "nope"}, expected_hash=None, auto_mode=None
        )
    # Validation precedes the mkdir, so even the .claude dir is not created.
    assert not (tmp_path / ".claude").exists()


# --- user-scope writer (HOME-isolated, explicit tmp file) --------------------------


def test_user_write_preserves_other_keys(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "sonnet", "permissions": {"allow": ["a"]}}))
    _b, _am, h = perms.read_user_permissions(settings)
    perms.write_user_permissions(settings, {"defaultMode": "plan"}, h, auto_mode=None)
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out["model"] == "sonnet"  # sibling preserved
    assert out["permissions"] == {"defaultMode": "plan"}


def test_user_read_missing_file_is_empty(tmp_path: Path) -> None:
    block, _am, h = perms.read_user_permissions(tmp_path / "absent" / "settings.json")
    assert block == {}
    assert h == cw.hash_bytes(b"")


def test_user_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"  # absent
    with pytest.raises(cw.InvalidCandidateError):
        perms.write_user_permissions(
            settings, {"defaultMode": "bypassPermissions"}, None, auto_mode=None
        )
    assert not settings.exists()  # nothing written on a validation failure


# --- local-scope writer (settings.local.json, gitignore-on-create) -----------------


def test_local_write_then_read_round_trip(tmp_path: Path) -> None:
    _b, _am, h0 = perms.read_project_local_permissions(tmp_path)
    assert _b == {}
    perms.write_project_local_permissions(
        tmp_path, {"defaultMode": "plan"}, expected_hash=h0, auto_mode=None
    )
    block, _am, _h1 = perms.read_project_local_permissions(tmp_path)
    assert block == {"defaultMode": "plan"}


def test_local_write_targets_settings_local_json_not_settings_json(tmp_path: Path) -> None:
    perms.write_project_local_permissions(
        tmp_path, {"defaultMode": "plan"}, expected_hash=None, auto_mode=None
    )
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_local_write_creates_gitignore_entry(tmp_path: Path) -> None:
    perms.write_project_local_permissions(
        tmp_path, {"defaultMode": "plan"}, expected_hash=None, auto_mode=None
    )
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore


def test_local_write_gitignore_idempotent_across_writes(tmp_path: Path) -> None:
    _b, _am, h0 = perms.read_project_local_permissions(tmp_path)
    perms.write_project_local_permissions(
        tmp_path, {"defaultMode": "plan"}, expected_hash=h0, auto_mode=None
    )
    _b1, _am, h1 = perms.read_project_local_permissions(tmp_path)
    perms.write_project_local_permissions(
        tmp_path, {"defaultMode": "default"}, expected_hash=h1, auto_mode=None
    )
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # Count exact lines, not substrings: the ``.bak`` sibling entry (#F6) contains
    # ``.claude/settings.local.json`` as a substring, so the base entry is asserted
    # unduplicated by line, not by substring occurrence.
    assert gitignore.splitlines().count(".claude/settings.local.json") == 1


def test_local_write_bad_shape_writes_nothing_and_no_gitignore(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        perms.write_project_local_permissions(
            tmp_path, {"defaultMode": "nope"}, expected_hash=None, auto_mode=None
        )
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".gitignore").exists()  # validation failure never touches gitignore


def test_local_write_stale_hash_raises(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"permissions": {}}))
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        perms.write_project_local_permissions(
            tmp_path, {"defaultMode": "plan"}, stale, auto_mode=None
        )


def test_local_scope_is_independent_of_project_scope_file(tmp_path: Path) -> None:
    # Writing project scope must never touch the local-scope file, and vice versa.
    perms.write_project_permissions(
        tmp_path, {"defaultMode": "acceptEdits"}, expected_hash=None, auto_mode=None
    )
    perms.write_project_local_permissions(
        tmp_path, {"defaultMode": "plan"}, expected_hash=None, auto_mode=None
    )
    project_block, _am, _ = perms.read_project_permissions(tmp_path)
    local_block, _am, _ = perms.read_project_local_permissions(tmp_path)
    assert project_block == {"defaultMode": "acceptEdits"}
    assert local_block == {"defaultMode": "plan"}


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

_URL = "/api/config-write/permissions"


def test_route_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 404
        assert (
            c.put(
                _URL,
                json={
                    "scope": "project",
                    "project": "alpha",
                    "confirm": "alpha",
                    "permissions": {},
                },
            ).status_code
            == 404
        )


def test_route_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get(f"{_URL}?scope=user").status_code == 404
        assert (
            c.put(
                _URL,
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "permissions": {}},
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
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "permissions": {}},
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
                "permissions": {"allow": ["x"]},
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
                "permissions": {"bogus": 1},  # unknown key
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_unknown_mode_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "permissions": {"defaultMode": "nonsense"},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_bypass_default_mode_is_422(write_config, tmp_path, projects_root) -> None:
    """A request to set ``bypassPermissions`` as the mode is rejected (422), nothing written."""
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "permissions": {"defaultMode": "bypassPermissions"},
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
                "permissions": {},
            },
        )
        assert resp.status_code == 400
        assert c.get(f"{_URL}?project=../escape").status_code == 400


def test_route_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "permissions": {"allow": ["x"]},
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_no_hash_on_existing_file_is_409(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions": {}}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "permissions": {"allow": ["x"]},
                # no "hash" — must not silently overwrite an existing file
            },
        )
        assert resp.status_code == 409


def test_route_project_write_read_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get(f"{_URL}?project=alpha")
        assert read0.status_code == 200
        assert read0.json()["permissions"] == {}
        h0 = read0.json()["hash"]
        wr = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "permissions": {"allow": ["Bash(ls:*)"], "defaultMode": "plan"},
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_URL}?project=alpha")
        assert read1.json()["permissions"] == {"allow": ["Bash(ls:*)"], "defaultMode": "plan"}


def test_route_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=bogus").status_code == 422
        assert c.put(_URL, json={"scope": "bogus", "permissions": {}}).status_code == 422


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
                "permissions": {"deny": ["Bash(curl:*)"]},
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read = c.get(f"{_URL}?scope=user")
        assert read.json()["permissions"] == {"deny": ["Bash(curl:*)"]}
    # The write landed in the ISOLATED home (autouse fixture), never the real account,
    # and in ~/.claude/settings.json — NOT ~/.claude.json.
    isolated = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    assert out["permissions"] == {"deny": ["Bash(curl:*)"]}
    # The write must NOT have landed in ~/.claude.json (that file is the trust store).
    claude_json = Path(os.environ["HOME"]) / ".claude.json"
    if claude_json.exists():
        assert "permissions" not in json.loads(claude_json.read_text(encoding="utf-8"))


def test_route_user_bypass_default_mode_is_422(write_config, tmp_path) -> None:
    """User scope cannot set bypassPermissions either — rejected (422), nothing written."""
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "permissions": {"defaultMode": "bypassPermissions"},
            },
        )
        assert resp.status_code == 422


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
                "permissions": {"allow": ["x"]},
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
                "permissions": {"allow": ["x"]},
            },
        )
        assert resp.status_code == 404
    assert not (absent / ".claude").exists()  # nothing written


def test_route_confirm_runs_before_validate(write_config, tmp_path, projects_root) -> None:
    # Ordering: the type-the-name confirm is the FIRST semantic gate after capability. A
    # request that BOTH omits a valid confirm AND carries a malformed `permissions` must
    # fail at the confirm gate (400), never reach the structural validator (422).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "permissions": "not-a-dict",
            },
        )
        assert resp.status_code == 400
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(_URL, json={"scope": "user", "confirm": "WRONG", "permissions": 123})
        assert resp.status_code == 400


def test_route_missing_permissions_is_422_after_valid_confirm(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(_URL, json={"scope": "project", "project": "alpha", "confirm": "alpha"})
        assert resp.status_code == 422


def test_route_user_missing_permissions_is_422_after_valid_confirm(write_config, tmp_path) -> None:
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
                "permissions": {"allow": ["x"]},
                "hash": 123,  # not a string
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_user_non_string_hash_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "permissions": {"allow": ["x"]},
                "hash": 123,
            },
        )
        assert resp.status_code == 422


def test_route_project_missing_name_is_400(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={"scope": "project", "confirm": "", "permissions": {"allow": ["x"]}},
        )
        assert resp.status_code == 400


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
        assert body["permissions"] == {}
        assert body["hash"] == cw.hash_bytes(b"")  # local is a real file, like project scope
        wr = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "permissions": {"defaultMode": "plan"},
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_URL}?scope=local&project=alpha")
        assert read1.json()["permissions"] == {"defaultMode": "plan"}


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
                "permissions": {"defaultMode": "plan"},
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
                "permissions": {"defaultMode": "plan"},
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
                    "permissions": {},
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
                "permissions": {"defaultMode": "plan"},
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
                "permissions": {},
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
                "permissions": {"defaultMode": "bypassPermissions"},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.local.json").exists()


def test_route_local_write_missing_permissions_is_422(
    write_config, tmp_path, projects_root
) -> None:
    # Local scope, valid confirm, no `permissions` -> the local-branch shape check 422s
    # (the payload_key guard, distinct from the project/user branches' own copies).
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={"scope": "local", "project": "alpha", "confirm": "alpha (local)"},
        )
        assert resp.status_code == 422


def test_route_local_write_non_string_hash_is_422(write_config, tmp_path, projects_root) -> None:
    # A non-string `hash` on a local-scope write (has_hash=True path) is a 422 before
    # any write, same guard as the project-scope branch.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "permissions": {"defaultMode": "plan"},
                "hash": 123,
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.local.json").exists()


def test_route_local_read_corrupt_file_is_422(write_config, tmp_path, projects_root) -> None:
    # The local-scope read guard: a hand-edited settings.local.json that is not valid
    # JSON must surface as a clean 422 from the GET route, never an unhandled 500 (same
    # guard as the project/user reads above).
    settings = projects_root / "alpha" / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=local&project=alpha").status_code == 422


# --- every documented permissions key and mode (TASK-449.3) -----------------------
#
# Source: https://code.claude.com/docs/en/settings-reference, the `permissions` entry
# and its `permissions.*` children (checked 2026-11-04, Claude Code 2.1.318).


@pytest.mark.parametrize(
    "candidate",
    [
        {"additionalDirectories": ["../docs/", "~/shared"]},
        {"additionalDirectories": []},
        {"blockReadsOutsideWorkingDirectories": True},
        {"blockReadsOutsideWorkingDirectories": False},  # documented: same as unset
        {"blockWritesOutsideWorkingDirectories": True},
        {"blockWritesOutsideWorkingDirectories": False},
        {"disableBypassPermissionsMode": "disable"},
        {"disableAutoMode": "disable"},
        {"defaultMode": "manual"},  # documented alias for "default"
        {"defaultMode": "auto"},
        {"defaultMode": "dontAsk"},
    ],
)
def test_validate_accepts_documented_permissions_key(candidate: dict) -> None:
    perms.validate_permissions(candidate)


def test_validate_accepts_every_documented_key_together() -> None:
    perms.validate_permissions(
        {
            "allow": ["Bash(npm run *)"],
            "ask": ["Bash(git push *)"],
            "deny": ["Read(./.env)"],
            "additionalDirectories": ["../docs/"],
            "blockReadsOutsideWorkingDirectories": True,
            "blockWritesOutsideWorkingDirectories": True,
            "defaultMode": "acceptEdits",
            "disableBypassPermissionsMode": "disable",
            "disableAutoMode": "disable",
        }
    )


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "auto", "dontAsk", "manual"])
def test_every_documented_default_mode_but_bypass_is_settable(mode: str) -> None:
    # The documented list for permissions.defaultMode, minus bypassPermissions.
    assert mode in perms.SETTINGS_DEFAULT_MODES
    perms.validate_permissions({"defaultMode": mode})


def test_settings_default_modes_keep_the_bypass_and_inherit_gates() -> None:
    assert "bypassPermissions" not in perms.SETTINGS_DEFAULT_MODES
    assert "inherit" not in perms.SETTINGS_DEFAULT_MODES
    # "manual" is a settings-only alias: the subagent surface's set is unchanged.
    assert "manual" not in perms.RECOGNIZED_MODES


@pytest.mark.parametrize(
    "candidate",
    [
        {"additionalDirectories": "../docs/"},  # must be a list
        {"additionalDirectories": [""]},
        {"additionalDirectories": [1]},
        {"blockReadsOutsideWorkingDirectories": "true"},  # string, not a Boolean
        {"blockReadsOutsideWorkingDirectories": 1},
        {"blockWritesOutsideWorkingDirectories": None},
        {"disableBypassPermissionsMode": True},  # the documented value is "disable"
        {"disableBypassPermissionsMode": "enable"},
        {"disableAutoMode": "yes"},
        {"disableAutoMode": False},
    ],
)
def test_validate_rejects_bad_new_key_shape(candidate: dict) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        perms.validate_permissions(candidate)


def test_validate_explains_auto_mode_nested_inside_permissions() -> None:
    # The TASK-449.1 dialog path: autoMode pasted into the Permissions raw JSON.
    with pytest.raises(cw.InvalidCandidateError, match="top-level settings key"):
        perms.validate_permissions({"allow": ["Bash(ls)"], "autoMode": {"allow": ["x"]}})


# Every rule form the permissions docs show (https://code.claude.com/docs/en/permissions,
# "Permission rule syntax" and "Tool-specific permission rules"). The validator checks
# only "non-empty string", so none of them may be refused, and each is stored verbatim.
_DOCUMENTED_RULES = [
    "Bash",
    "Bash(*)",
    "Bash(npm run *)",
    "Bash(ls:*)",
    "Bash(git:* push)",
    "Bash(* --version)",
    "Bash(command:rm *)",
    "Bash(run_in_background:true)",
    "PowerShell(Get-ChildItem *)",
    "Read(./.env)",
    "Read(~/.zshrc)",
    "Edit(/src/**/*.ts)",
    "Edit(//tmp/scratch.txt)",
    "Edit(./Finance (2024)/**)",
    "Read(!**/.env)",
    "WebFetch",
    "WebFetch(domain:example.com)",
    "mcp__github",
    "mcp__github__get_*",
    "mcp__*",
    "*",
    "Agent(Explore)",
    "Agent(model:opus)",
    "Agent(isolation:worktree)",
    "Cd(~/code/**)",
    "Skill(deploy)",
]


def test_documented_rule_forms_are_accepted_and_stored_verbatim(tmp_path: Path) -> None:
    candidate = {
        "allow": _DOCUMENTED_RULES,
        "ask": _DOCUMENTED_RULES,
        "deny": _DOCUMENTED_RULES,
    }
    perms.validate_permissions(candidate)
    perms.write_project_permissions(tmp_path, candidate, expected_hash=None, auto_mode=None)
    block, _am, _h = perms.read_project_permissions(tmp_path)
    assert block == candidate


# --- autoMode: the auto mode classifier rules (TASK-449.1) ------------------------
#
# Source: https://code.claude.com/docs/en/settings-reference (`autoMode`,
# `autoMode.classifyAllShell`) and https://code.claude.com/docs/en/auto-mode-config.

#: The maintainer's exact block from the 2026-11-04 report.
_MAINTAINER_AUTO_MODE = {
    "allow": [
        "$defaults",
        "git push --force-with-lease from a worktree under /home/claude/worktrees/claustrum/ "
        "to an unmerged feature branch that this session opened is allowed. Never to main "
        "or master, ...",
    ]
}


@pytest.mark.parametrize(
    "candidate",
    [
        {},
        {"environment": ["$defaults", "Source control: github.example.com/acme-corp"]},
        {"allow": ["$defaults", "Deploying to the staging namespace is allowed"]},
        {"soft_deny": ["$defaults", "Never run terraform apply"]},
        {"hard_deny": ["$defaults", "Never send data to pastebin"]},
        {"allow": ["Replace the defaults entirely"]},  # no "$defaults": documented replace
        {"classifyAllShell": True},
        {"classifyAllShell": False},
        {
            "environment": ["$defaults"],
            "allow": ["$defaults"],
            "soft_deny": ["$defaults"],
            "hard_deny": ["$defaults"],
            "classifyAllShell": True,
        },
        _MAINTAINER_AUTO_MODE,
    ],
)
def test_validate_auto_mode_accepts_documented_shapes(candidate: dict) -> None:
    perms.validate_auto_mode(candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        ["$defaults"],  # not an object
        "$defaults",
        {"deny": ["x"]},  # unknown key (the docs name soft_deny / hard_deny)
        {"allow": "$defaults"},  # must be a list
        {"allow": [""]},
        {"soft_deny": [1]},
        {"classifyAllShell": "true"},
    ],
)
def test_validate_auto_mode_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        perms.validate_auto_mode(candidate)


def test_route_user_scope_saves_maintainer_auto_mode_payload(write_config, tmp_path) -> None:
    """Regression (TASK-449.1): the reported block saves beside permissions and reads back."""
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get(f"{_URL}?scope=user")
        assert read0.status_code == 200
        assert read0.json()["autoMode"] is None  # no block yet
        wr = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "hash": read0.json()["hash"],
                "permissions": {"allow": ["Bash(ls:*)"]},
                "autoMode": _MAINTAINER_AUTO_MODE,
            },
        )
        assert wr.status_code == 200, wr.text
        read1 = c.get(f"{_URL}?scope=user").json()
        assert read1["autoMode"] == _MAINTAINER_AUTO_MODE
        assert read1["permissions"] == {"allow": ["Bash(ls:*)"]}
    stored = json.loads(
        (Path(os.environ["HOME"]) / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    # A top-level sibling of permissions, exactly as Claude Code reads it.
    assert stored["autoMode"] == _MAINTAINER_AUTO_MODE
    assert "autoMode" not in stored["permissions"]


@pytest.mark.parametrize(
    ("scope", "confirm", "filename"),
    [
        ("project", "alpha", "settings.json"),
        ("local", "alpha (local)", "settings.local.json"),
    ],
)
def test_route_project_and_local_refuse_maintainer_auto_mode(
    write_config, tmp_path, projects_root, scope: str, confirm: str, filename: str
) -> None:
    """Claude Code ignores autoMode in repo files, so a save there is refused, not stored."""
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": scope,
                "project": "alpha",
                "confirm": confirm,
                "permissions": {"allow": ["Bash(ls:*)"]},
                "autoMode": _MAINTAINER_AUTO_MODE,
            },
        )
        assert resp.status_code == 422
        assert "User scope" in resp.json()["detail"]
    assert not (projects_root / "alpha" / ".claude" / filename).exists()  # nothing written


@pytest.mark.parametrize("scope", ["project", "local"])
def test_project_and_local_refuse_default_mode_auto(tmp_path: Path, scope: str) -> None:
    writer = (
        perms.write_project_permissions
        if scope == "project"
        else perms.write_project_local_permissions
    )
    with pytest.raises(cw.InvalidCandidateError, match="User scope"):
        writer(tmp_path, {"defaultMode": "auto"}, expected_hash=None, auto_mode=None)
    assert not (tmp_path / ".claude").exists()


def test_user_scope_accepts_default_mode_auto(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    perms.write_user_permissions(settings, {"defaultMode": "auto"}, None, auto_mode=None)
    stored = json.loads(settings.read_text(encoding="utf-8"))
    assert stored["permissions"] == {"defaultMode": "auto"}


def test_user_scope_auto_mode_bad_shape_writes_nothing(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    with pytest.raises(cw.InvalidCandidateError, match="unknown keys"):
        perms.write_user_permissions(settings, {}, None, auto_mode={"deny": ["x"]})
    assert not settings.exists()


def test_absent_auto_mode_leaves_stored_block_untouched(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"autoMode": {"allow": ["kept"]}}), encoding="utf-8")
    _block, auto_mode, h = perms.read_project_permissions(tmp_path)
    assert auto_mode == {"allow": ["kept"]}  # an existing repo block is shown, not hidden
    perms.write_project_permissions(tmp_path, {"deny": ["Bash(rm *)"]}, h, auto_mode=None)
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out == {"autoMode": {"allow": ["kept"]}, "permissions": {"deny": ["Bash(rm *)"]}}


def test_empty_auto_mode_removes_block_at_project_scope(tmp_path: Path) -> None:
    # The one autoMode write a repo file accepts: clearing an inert block.
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"autoMode": {"allow": ["inert"]}, "model": "opus"}), encoding="utf-8"
    )
    _b, _am, h = perms.read_project_permissions(tmp_path)
    perms.write_project_permissions(tmp_path, {}, h, auto_mode={})
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert "autoMode" not in out
    assert out["model"] == "opus"  # sibling preserved


def test_empty_auto_mode_removes_block_at_user_scope(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    perms.write_user_permissions(settings, {}, None, auto_mode={"allow": ["x"]})
    _b, _am, h = perms.read_user_permissions(settings)
    perms.write_user_permissions(settings, {}, h, auto_mode={})
    assert "autoMode" not in json.loads(settings.read_text(encoding="utf-8"))


def test_auto_mode_is_redacted_on_read_and_sentinel_keeps_stored(tmp_path: Path) -> None:
    """A credential-bearing URL in a prose entry never reaches the read view."""
    settings = tmp_path / ".claude" / "settings.json"
    secret = "https://deploy:hunter2@git.example.com/acme"
    perms.write_user_permissions(
        settings, {}, None, auto_mode={"environment": ["$defaults", secret]}
    )
    _b, auto_mode, h = perms.read_user_permissions(settings)
    assert auto_mode == {"environment": ["$defaults", cw.REDACTION_SENTINEL]}
    assert "hunter2" not in json.dumps(auto_mode)
    # Resending the view unchanged (plus a new entry) keeps the stored secret.
    auto_mode["environment"].append("Trusted bucket: s3://acme-builds")
    perms.write_user_permissions(settings, {}, h, auto_mode=auto_mode)
    stored = json.loads(settings.read_text(encoding="utf-8"))["autoMode"]
    assert stored == {"environment": ["$defaults", secret, "Trusted bucket: s3://acme-builds"]}


@pytest.mark.parametrize(
    "entry",
    [
        "Source control: https://ci:hunter2@git.example.com/acme",  # URL mid-prose
        "Pushing with ${GH_TOKEN} to the mirror is allowed",  # interpolation mid-prose
        "token: hunter2",  # secret-keyed line
        "Use ${GH\nTOKEN} here",  # an interpolation that spans a newline (structural check)
    ],
)
def test_auto_mode_masks_a_secret_anywhere_in_a_prose_entry(tmp_path: Path, entry: str) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    perms.write_user_permissions(
        settings, {}, None, auto_mode={"environment": ["$defaults", entry, "Plain rule"]}
    )
    _b, auto_mode, h = perms.read_user_permissions(settings)
    # The whole entry is masked (a partial mask could not round-trip); others stay clear.
    assert auto_mode == {"environment": ["$defaults", cw.REDACTION_SENTINEL, "Plain rule"]}
    # Resending the view unchanged keeps the stored entry: the merge counts the same
    # masked entries the read view masked.
    perms.write_user_permissions(settings, {}, h, auto_mode=auto_mode)
    stored = json.loads(settings.read_text(encoding="utf-8"))["autoMode"]
    assert stored == {"environment": ["$defaults", entry, "Plain rule"]}


def _seed_masked(settings: Path) -> tuple[list, str]:
    """Store two masked environment entries between clear ones; return (view, hash)."""
    perms.write_user_permissions(
        settings,
        {},
        None,
        auto_mode={
            "environment": [
                "$defaults",
                "Source control: https://a:one@git.example.com",
                "Plain rule",
                "Mirror: https://b:two@mirror.example.com",
            ]
        },
    )
    _b, auto_mode, h = perms.read_user_permissions(settings)
    return auto_mode["environment"], h


def test_auto_mode_moving_clear_entries_keeps_masked_secrets(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    view, h = _seed_masked(settings)
    assert view == ["$defaults", cw.REDACTION_SENTINEL, "Plain rule", cw.REDACTION_SENTINEL]
    # Insert a new entry first, so every mask moves to a new index: an index-based merge
    # would map the first mask to "Plain rule". Masks keep their stored secrets in order.
    edited = ["New rule", "$defaults", cw.REDACTION_SENTINEL, "Plain rule", cw.REDACTION_SENTINEL]
    perms.write_user_permissions(settings, {}, h, auto_mode={"environment": edited})
    stored = json.loads(settings.read_text(encoding="utf-8"))["autoMode"]["environment"]
    assert stored == [
        "New rule",
        "$defaults",
        "Source control: https://a:one@git.example.com",
        "Plain rule",
        "Mirror: https://b:two@mirror.example.com",
    ]


@pytest.mark.parametrize(
    "edited",
    [
        ["$defaults", cw.REDACTION_SENTINEL, "Plain rule"],  # one mask deleted: ambiguous
        [cw.REDACTION_SENTINEL] * 3,  # a mask added that has nothing stored behind it
    ],
)
def test_auto_mode_mask_count_mismatch_is_refused_whole(tmp_path: Path, edited: list) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    _view, h = _seed_masked(settings)
    before = settings.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="masked"):
        perms.write_user_permissions(settings, {}, h, auto_mode={"environment": edited})
    assert settings.read_bytes() == before  # nothing written, no secret dropped or swapped


def test_auto_mode_mask_without_stored_value_is_refused(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    with pytest.raises(cw.InvalidCandidateError, match="masked"):
        perms.write_user_permissions(
            settings, {}, None, auto_mode={"allow": [cw.REDACTION_SENTINEL]}
        )
    assert not settings.exists()


def test_auto_mode_removing_every_mask_deletes_the_secrets(tmp_path: Path) -> None:
    # Deleting the masked entries outright is an explicit choice, not a mismatch.
    settings = tmp_path / ".claude" / "settings.json"
    _view, h = _seed_masked(settings)
    perms.write_user_permissions(settings, {}, h, auto_mode={"environment": ["$defaults"]})
    stored = json.loads(settings.read_text(encoding="utf-8"))["autoMode"]
    assert stored == {"environment": ["$defaults"]}


def test_auto_mode_non_object_on_disk_reads_redacted(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"autoMode": ["${SECRET}"]}), encoding="utf-8")
    _b, auto_mode, _h = perms.read_user_permissions(settings)
    assert auto_mode == [cw.REDACTION_SENTINEL]


def test_auto_mode_non_string_leaves_read_unchanged(tmp_path: Path) -> None:
    # classifyAllShell is a documented Boolean, and a hand-edited list can hold a number
    # or null: the prose-secret pass must leave every non-string leaf as stored.
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    stored = {"classifyAllShell": True, "environment": ["$defaults", 3, None]}
    settings.write_text(json.dumps({"autoMode": stored}), encoding="utf-8")
    _b, auto_mode, _h = perms.read_user_permissions(settings)
    assert auto_mode == stored


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # A secret-named key masks its whole subtree (the structural key hint is kept).
        ({"apiToken": ["sk-live-abc123"]}, {"apiToken": [cw.REDACTION_SENTINEL]}),
        ({"token": "placeholder"}, {"token": cw.REDACTION_SENTINEL}),
        ({"auth": {"value": "sk-live-abc123"}}, {"auth": {"value": cw.REDACTION_SENTINEL}}),
        # A prose secret is masked whatever shape a hand-edited file gives it.
        (["Source: https://ci:pw@host"], [cw.REDACTION_SENTINEL]),
        ({"environment": "Source: https://ci:pw@host"}, {"environment": cw.REDACTION_SENTINEL}),
        (
            {"environment": [["Source: https://ci:pw@host"]]},
            {"environment": [[cw.REDACTION_SENTINEL]]},
        ),
    ],
)
def test_auto_mode_hand_edited_shapes_never_leak(
    tmp_path: Path, stored: object, expected: object
) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"autoMode": stored}), encoding="utf-8")
    _b, auto_mode, _h = perms.read_user_permissions(settings)
    assert auto_mode == expected
    for secret in ("sk-live-abc123", "ci:pw"):
        assert secret not in json.dumps(auto_mode)


def test_route_auto_mode_write_is_audited_by_key(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        wr = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "permissions": {},
                "autoMode": _MAINTAINER_AUTO_MODE,
            },
        )
        assert wr.status_code == 200
    last = (tmp_path / ".s" / "config_audit.log").read_text(encoding="utf-8").splitlines()[-1]
    entry = json.loads(last)
    assert entry["surface"] == "permissions"
    assert entry["keys"] == ["autoMode"]  # keys only, never the rule text
    assert "worktrees" not in last


def test_route_confirm_runs_before_auto_mode_validation(write_config, tmp_path) -> None:
    # A wrong confirm is still the first semantic error, whatever autoMode holds.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": "WRONG",
                "permissions": {},
                "autoMode": "not-an-object",
            },
        )
        assert resp.status_code == 400
