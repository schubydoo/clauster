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
        tmp_path, {"allow": ["Bash(echo bypassPermissions)"]}, expected_hash=cw.hash_bytes(b"")
    )
    stored = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["permissions"]["allow"] == ["Bash(echo bypassPermissions)"]
    assert "defaultMode" not in stored["permissions"]


# --- validate-never-execute: a rule string is stored verbatim, never run -----------


def test_write_stores_rule_verbatim_never_parsed(tmp_path: Path) -> None:
    candidate = {"deny": ["Bash(rm -rf /:*)"], "allow": ["Read(/etc/passwd)"]}
    perms.validate_permissions(candidate)
    perms.write_project_permissions(tmp_path, candidate, expected_hash=cw.hash_bytes(b""))
    stored = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["permissions"]["deny"] == ["Bash(rm -rf /:*)"]
    assert stored["permissions"]["allow"] == ["Read(/etc/passwd)"]


def test_write_stores_ask_rules_verbatim(tmp_path: Path) -> None:
    """An `ask` bucket round-trips like allow/deny — stored verbatim, never parsed."""
    candidate = {"ask": ["Bash(git push:*)", "Write(/etc/*)"]}
    perms.validate_permissions(candidate)
    perms.write_project_permissions(tmp_path, candidate, expected_hash=cw.hash_bytes(b""))
    stored = json.loads(cw.project_settings_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["permissions"]["ask"] == ["Bash(git push:*)", "Write(/etc/*)"]


# --- project read/write round-trip + stale-hash ------------------------------------


def test_project_write_then_read_round_trip(tmp_path: Path) -> None:
    block, h0 = perms.read_project_permissions(tmp_path)
    assert block == {}
    perms.write_project_permissions(tmp_path, {"defaultMode": "plan"}, expected_hash=h0)
    block, _h1 = perms.read_project_permissions(tmp_path)
    assert block == {"defaultMode": "plan"}


def test_project_write_preserves_sibling_keys(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"permissions": {"allow": ["a"]}, "hooks": {"X": 1}, "model": "opus"}),
        encoding="utf-8",
    )
    _b, h = perms.read_project_permissions(tmp_path)
    perms.write_project_permissions(tmp_path, {"deny": ["b"]}, h)
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
        perms.write_project_permissions(tmp_path, {"deny": ["new"]}, stale)


def test_project_write_no_hash_on_existing_file_is_stale(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions": {}}', encoding="utf-8")
    with pytest.raises(cw.StaleConfigWriteError):
        perms.write_project_permissions(tmp_path, {"allow": ["x"]}, expected_hash=None)


def test_project_write_no_hash_on_absent_file_ok(tmp_path: Path) -> None:
    perms.write_project_permissions(tmp_path, {"allow": ["x"]}, expected_hash=None)
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
        perms.write_project_permissions(tmp_path, {"defaultMode": "nope"}, expected_hash=None)
    # Validation precedes the mkdir, so even the .claude dir is not created.
    assert not (tmp_path / ".claude").exists()


# --- user-scope writer (HOME-isolated, explicit tmp file) --------------------------


def test_user_write_preserves_other_keys(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "sonnet", "permissions": {"allow": ["a"]}}))
    _b, h = perms.read_user_permissions(settings)
    perms.write_user_permissions(settings, {"defaultMode": "plan"}, h)
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out["model"] == "sonnet"  # sibling preserved
    assert out["permissions"] == {"defaultMode": "plan"}


def test_user_read_missing_file_is_empty(tmp_path: Path) -> None:
    block, h = perms.read_user_permissions(tmp_path / "absent" / "settings.json")
    assert block == {}
    assert h == cw.hash_bytes(b"")


def test_user_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"  # absent
    with pytest.raises(cw.InvalidCandidateError):
        perms.write_user_permissions(settings, {"defaultMode": "bypassPermissions"}, None)
    assert not settings.exists()  # nothing written on a validation failure


# --- local-scope writer (settings.local.json, gitignore-on-create) -----------------


def test_local_write_then_read_round_trip(tmp_path: Path) -> None:
    _b, h0 = perms.read_project_local_permissions(tmp_path)
    assert _b == {}
    perms.write_project_local_permissions(tmp_path, {"defaultMode": "plan"}, expected_hash=h0)
    block, _h1 = perms.read_project_local_permissions(tmp_path)
    assert block == {"defaultMode": "plan"}


def test_local_write_targets_settings_local_json_not_settings_json(tmp_path: Path) -> None:
    perms.write_project_local_permissions(tmp_path, {"defaultMode": "plan"}, expected_hash=None)
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_local_write_creates_gitignore_entry(tmp_path: Path) -> None:
    perms.write_project_local_permissions(tmp_path, {"defaultMode": "plan"}, expected_hash=None)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore


def test_local_write_gitignore_idempotent_across_writes(tmp_path: Path) -> None:
    _b, h0 = perms.read_project_local_permissions(tmp_path)
    perms.write_project_local_permissions(tmp_path, {"defaultMode": "plan"}, expected_hash=h0)
    _b1, h1 = perms.read_project_local_permissions(tmp_path)
    perms.write_project_local_permissions(tmp_path, {"defaultMode": "default"}, expected_hash=h1)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # Count exact lines, not substrings: the ``.bak`` sibling entry (#F6) contains
    # ``.claude/settings.local.json`` as a substring, so the base entry is asserted
    # unduplicated by line, not by substring occurrence.
    assert gitignore.splitlines().count(".claude/settings.local.json") == 1


def test_local_write_bad_shape_writes_nothing_and_no_gitignore(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        perms.write_project_local_permissions(
            tmp_path, {"defaultMode": "nope"}, expected_hash=None
        )
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".gitignore").exists()  # validation failure never touches gitignore


def test_local_write_stale_hash_raises(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"permissions": {}}))
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        perms.write_project_local_permissions(tmp_path, {"defaultMode": "plan"}, stale)


def test_local_scope_is_independent_of_project_scope_file(tmp_path: Path) -> None:
    # Writing project scope must never touch the local-scope file, and vice versa.
    perms.write_project_permissions(tmp_path, {"defaultMode": "acceptEdits"}, expected_hash=None)
    perms.write_project_local_permissions(tmp_path, {"defaultMode": "plan"}, expected_hash=None)
    project_block, _ = perms.read_project_permissions(tmp_path)
    local_block, _ = perms.read_project_local_permissions(tmp_path)
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
