"""Generic settings.json editor surface (#772) over the #347/#687 Foundation.

Covers the structural validator (owned-key rejection + env shape check), the
project/user/local read+write functions (round trip, owned-key preservation,
stale-hash guard), the #822-lesson env redaction (every env value masked on
read regardless of key name, keep-stored on a resent sentinel), the
scope-merge provenance computation (local > project > user precedence, per
top-level key), and the gated routes (capability/scope 404, the #819/#768
capability-before-scope-enum ordering, type-the-name 400, bad-shape/owned-key
422 writing nothing, stale-hash 409, path-escape 400).

Every test that touches ``~/.claude/settings.json`` runs under the autouse
HOME-isolation fixture and writes only into the isolated tmp home — the live
account is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_settings as cws
from clauster.app import create_app
from clauster.config import load_config

# --- structural validator -----------------------------------------------------------


def test_validate_accepts_env_and_model() -> None:
    cws.validate_misc_settings({"env": {"FOO": "bar"}, "model": "sonnet"})  # no raise


def test_validate_accepts_empty_object() -> None:
    cws.validate_misc_settings({})  # no raise


def test_validate_accepts_arbitrary_misc_key() -> None:
    # No exhaustive allowlist — an unenumerated documented key (or a future one)
    # passes through as opaque JSON (see module docstring for why).
    cws.validate_misc_settings({"apiKeyHelper": "/bin/gen.sh", "cleanupPeriodDays": 30})


def test_validate_rejects_non_dict_candidate() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cws.validate_misc_settings([])


@pytest.mark.parametrize("owned_key", sorted(cws.OWNED_KEYS))
def test_validate_rejects_each_owned_key(owned_key: str) -> None:
    with pytest.raises(cws.SettingsCarveError):
        cws.validate_misc_settings({owned_key: {}})


def test_validate_rejects_owned_key_alongside_valid_misc() -> None:
    # A single owned key rejects the WHOLE write, even with otherwise-valid misc.
    with pytest.raises(cw.InvalidCandidateError):
        cws.validate_misc_settings({"model": "sonnet", "hooks": {}})


def test_validate_rejects_non_dict_env() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cws.validate_misc_settings({"env": ["not", "a", "dict"]})


def test_validate_rejects_non_string_env_value() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cws.validate_misc_settings({"env": {"FOO": 123}})


def test_validate_rejects_empty_string_env_key() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cws.validate_misc_settings({"env": {"": "bar"}})


def test_validate_accepts_empty_env() -> None:
    cws.validate_misc_settings({"env": {}})  # no raise


# --- redaction: the #822-lesson env override -----------------------------------------


def test_redact_masks_every_env_value_unconditionally() -> None:
    # DEPLOY_KEY does NOT match the generic secret-key regex (no "token"/"secret"/
    # "apikey"/"auth"/"credential"/"bearer" substring, and a bare "key" never
    # matches) — exactly the #822 gap this surface's env override closes.
    misc = {"env": {"DEPLOY_KEY": "AKIA_super_secret", "HARMLESS": "1"}}
    redacted = cws._redact_misc(misc)
    assert redacted["env"] == {
        "DEPLOY_KEY": cw.REDACTION_SENTINEL,
        "HARMLESS": cw.REDACTION_SENTINEL,
    }


def test_redact_leaves_non_secret_misc_keys_in_clear() -> None:
    misc = {"model": "sonnet", "cleanupPeriodDays": 30}
    redacted = cws._redact_misc(misc)
    assert redacted == {"model": "sonnet", "cleanupPeriodDays": 30}


def test_redact_still_applies_generic_heuristic_to_non_env_keys() -> None:
    # apiKeyHelper's value is a shell path, not a secret, but the KEY matches the
    # generic api[-_]?key heuristic — over-masking a non-secret is the same
    # deliberately-conservative direction the rest of the Foundation takes.
    misc = {"apiKeyHelper": "/bin/generate_temp_api_key.sh"}
    redacted = cws._redact_misc(misc)
    assert redacted["apiKeyHelper"] == cw.REDACTION_SENTINEL


def test_redact_missing_env_is_a_noop() -> None:
    misc = {"model": "sonnet"}
    assert cws._redact_misc(misc) == {"model": "sonnet"}


# --- read/write round trip: project scope --------------------------------------------


def test_project_read_absent_is_empty(tmp_path: Path) -> None:
    settings, file_hash = cws.read_project_settings(tmp_path)
    assert settings == {}
    assert file_hash == cw.hash_bytes(b"")


def test_project_write_then_read_round_trip(tmp_path: Path) -> None:
    _s, h0 = cws.read_project_settings(tmp_path)
    cws.write_project_settings(tmp_path, {"model": "sonnet"}, h0)
    settings, _h1 = cws.read_project_settings(tmp_path)
    assert settings == {"model": "sonnet"}


def test_project_write_preserves_owned_keys(tmp_path: Path) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"permissions": {"defaultMode": "acceptEdits"}, "hooks": {"Stop": []}}),
        encoding="utf-8",
    )
    _s, h0 = cws.read_project_settings(tmp_path)
    cws.write_project_settings(tmp_path, {"model": "sonnet"}, h0)
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk["permissions"] == {"defaultMode": "acceptEdits"}
    assert on_disk["hooks"] == {"Stop": []}
    assert on_disk["model"] == "sonnet"


def test_project_write_drops_misc_keys_not_resent(tmp_path: Path) -> None:
    # Full-replacement semantics for the misc partition (mirrors permissions/hooks):
    # a key present on disk but omitted from the write is removed, not preserved.
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"model": "opus", "cleanupPeriodDays": 30}))
    _s, h0 = cws.read_project_settings(tmp_path)
    cws.write_project_settings(tmp_path, {"model": "sonnet"}, h0)
    settings, _h1 = cws.read_project_settings(tmp_path)
    assert settings == {"model": "sonnet"}


def test_project_write_stale_hash_raises(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"model": "opus"}')
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        cws.write_project_settings(tmp_path, {"model": "sonnet"}, stale)


def test_project_write_no_hash_on_existing_file_is_stale(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text('{"model": "opus"}')
    with pytest.raises(cw.StaleConfigWriteError):
        cws.write_project_settings(tmp_path, {"model": "sonnet"}, None)


def test_project_write_no_hash_on_absent_file_ok(tmp_path: Path) -> None:
    cws.write_project_settings(tmp_path, {"model": "sonnet"}, None)
    settings, _h = cws.read_project_settings(tmp_path)
    assert settings == {"model": "sonnet"}


def test_project_write_bad_shape_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cws.write_project_settings(tmp_path, {"env": {"FOO": 1}}, None)
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_project_write_owned_key_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cws.write_project_settings(tmp_path, {"permissions": {}}, None)
    assert not (tmp_path / ".claude" / "settings.json").exists()


# --- keep-stored sentinel merge (env values + coincidentally secret-shaped keys) ------


def test_write_resent_sentinel_keeps_stored_env_value(tmp_path: Path) -> None:
    _s, h0 = cws.read_project_settings(tmp_path)
    cws.write_project_settings(tmp_path, {"env": {"FOO": "real-secret", "BAR": "1"}}, h0)
    settings, h1 = cws.read_project_settings(tmp_path)
    assert settings["env"] == {"FOO": cw.REDACTION_SENTINEL, "BAR": cw.REDACTION_SENTINEL}
    # Operator edits BAR only, resending FOO's sentinel unchanged (as the editor
    # would round-trip whatever the read gave it).
    cws.write_project_settings(tmp_path, {"env": {"FOO": cw.REDACTION_SENTINEL, "BAR": "2"}}, h1)
    settings_path = tmp_path / ".claude" / "settings.json"
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk["env"] == {"FOO": "real-secret", "BAR": "2"}  # FOO's real value kept


def test_write_resent_sentinel_keeps_stored_apikeyhelper(tmp_path: Path) -> None:
    _s, h0 = cws.read_project_settings(tmp_path)
    cws.write_project_settings(tmp_path, {"apiKeyHelper": "/bin/gen.sh"}, h0)
    settings, h1 = cws.read_project_settings(tmp_path)
    assert settings["apiKeyHelper"] == cw.REDACTION_SENTINEL
    cws.write_project_settings(
        tmp_path, {"apiKeyHelper": cw.REDACTION_SENTINEL, "model": "sonnet"}, h1
    )
    settings_path = tmp_path / ".claude" / "settings.json"
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk["apiKeyHelper"] == "/bin/gen.sh"
    assert on_disk["model"] == "sonnet"


def test_write_sentinel_for_absent_key_is_dropped(tmp_path: Path) -> None:
    # A resent sentinel for a var that was never actually stored has nothing to
    # keep — it is dropped, never written as the literal "********" string.
    cws.write_project_settings(tmp_path, {"env": {"GHOST": cw.REDACTION_SENTINEL}}, None)
    settings_path = tmp_path / ".claude" / "settings.json"
    on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
    assert on_disk.get("env", {}) == {}


# --- user scope --------------------------------------------------------------------


def test_user_write_preserves_other_keys(tmp_path: Path) -> None:
    settings_json = tmp_path / "settings.json"
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    settings_json.write_text(json.dumps({"hooks": {"Stop": []}}))
    _s, h0 = cws.read_user_settings(settings_json)
    cws.write_user_settings(settings_json, {"model": "opus"}, h0)
    on_disk = json.loads(settings_json.read_text(encoding="utf-8"))
    assert on_disk["hooks"] == {"Stop": []}
    assert on_disk["model"] == "opus"


def test_user_read_missing_file_is_empty(tmp_path: Path) -> None:
    settings, file_hash = cws.read_user_settings(tmp_path / "settings.json")
    assert settings == {}
    assert file_hash == cw.hash_bytes(b"")


def test_user_write_rejects_bad_shape_without_writing(tmp_path: Path) -> None:
    settings_json = tmp_path / "settings.json"
    with pytest.raises(cw.InvalidCandidateError):
        cws.write_user_settings(settings_json, {"env": ["bad"]}, None)
    assert not settings_json.exists()


# --- local scope ---------------------------------------------------------------------


def test_local_write_then_read_round_trip(tmp_path: Path) -> None:
    _s, h0 = cws.read_project_local_settings(tmp_path)
    cws.write_project_local_settings(tmp_path, {"model": "sonnet"}, h0)
    settings, _h1 = cws.read_project_local_settings(tmp_path)
    assert settings == {"model": "sonnet"}


def test_local_write_targets_settings_local_json_not_settings_json(tmp_path: Path) -> None:
    cws.write_project_local_settings(tmp_path, {"model": "sonnet"}, None)
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_local_write_creates_gitignore_entry(tmp_path: Path) -> None:
    cws.write_project_local_settings(tmp_path, {"model": "sonnet"}, None)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore.splitlines()


def test_local_write_gitignore_idempotent_across_writes(tmp_path: Path) -> None:
    _s, h0 = cws.read_project_local_settings(tmp_path)
    cws.write_project_local_settings(tmp_path, {"model": "sonnet"}, h0)
    _s2, h1 = cws.read_project_local_settings(tmp_path)
    cws.write_project_local_settings(tmp_path, {"model": "opus"}, h1)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count(".claude/settings.local.json") == 1


def test_local_scope_is_independent_of_project_scope_file(tmp_path: Path) -> None:
    cws.write_project_settings(tmp_path, {"model": "opus"}, None)
    cws.write_project_local_settings(tmp_path, {"model": "sonnet"}, None)
    project_settings, _h1 = cws.read_project_settings(tmp_path)
    local_settings, _h2 = cws.read_project_local_settings(tmp_path)
    assert project_settings == {"model": "opus"}
    assert local_settings == {"model": "sonnet"}


def test_local_write_bad_shape_writes_nothing_and_no_gitignore(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cws.write_project_local_settings(tmp_path, {"hooks": {}}, None)
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_local_write_stale_hash_raises(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text('{"model": "opus"}')
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        cws.write_project_local_settings(tmp_path, {"model": "sonnet"}, stale)


# --- scope-merge provenance (the novel #772 part) -------------------------------------


def test_effective_local_overrides_project_overrides_user() -> None:
    effective = cws._compute_effective_settings(
        user_misc={"model": "haiku", "cleanupPeriodDays": 10},
        project_misc={"model": "opus"},
        local_misc={"model": "sonnet"},
    )
    assert effective["model"] == {"value": "sonnet", "source": "local"}
    # cleanupPeriodDays only defined at user scope — falls through to it.
    assert effective["cleanupPeriodDays"] == {"value": 10, "source": "user"}


def test_effective_project_overrides_user_when_local_silent_on_key() -> None:
    effective = cws._compute_effective_settings(
        user_misc={"model": "haiku"},
        project_misc={"model": "opus"},
        local_misc={},
    )
    assert effective["model"] == {"value": "opus", "source": "project"}


def test_effective_falls_through_to_user_when_others_absent() -> None:
    effective = cws._compute_effective_settings(
        user_misc={"model": "haiku"}, project_misc={}, local_misc={}
    )
    assert effective["model"] == {"value": "haiku", "source": "user"}


def test_effective_omits_key_absent_everywhere() -> None:
    effective = cws._compute_effective_settings(user_misc={}, project_misc={}, local_misc={})
    assert effective == {}


def test_effective_none_scope_never_participates() -> None:
    # user_misc=None (allow_user_scope off) must never be treated as an
    # authoritative empty layer — a key only user defines simply never surfaces,
    # rather than falling through to "nothing" and hiding the real gap.
    effective = cws._compute_effective_settings(
        user_misc=None, project_misc={"model": "opus"}, local_misc={}
    )
    assert effective == {"model": {"value": "opus", "source": "project"}}


def test_effective_whole_value_wins_no_deep_merge_inside_env() -> None:
    # A project-scope `env` is not key-by-key merged with a user-scope `env` —
    # the highest-precedence scope that defines `env` at all supplies it whole.
    effective = cws._compute_effective_settings(
        user_misc={"env": {"A": "1", "B": "2"}},
        project_misc={},
        local_misc={"env": {"A": "9"}},
    )
    assert effective["env"] == {"value": {"A": "9"}, "source": "local"}


# --- gated routes (full FastAPI lifespan) --------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path, extra: str) -> TestClient:
    # The autouse HOME-isolation fixture pins HOME to an isolated tmp dir, so the
    # user-scope settings path resolves under <isolated-home>/.claude/settings.json —
    # the real account is never touched by the user-scope routes.
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"

_URL = "/api/config-write/settings"
_EFFECTIVE_URL = "/api/config-write/settings/effective"


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
                    "settings": {},
                },
            ).status_code
            == 404
        )
        assert c.get(f"{_EFFECTIVE_URL}?project=alpha").status_code == 404


def test_route_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get(f"{_URL}?scope=user").status_code == 404
        assert (
            c.put(
                _URL,
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "settings": {}},
            ).status_code
            == 404
        )


def test_route_user_scope_404_when_runner_missing(write_config, tmp_path) -> None:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    app = create_app(load_config(cfg))
    with TestClient(app) as c:
        app.state.runner = None  # simulate an app started without a runner
        assert c.get(f"{_URL}?scope=user").status_code == 404
        assert (
            c.put(
                _URL,
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "settings": {}},
            ).status_code
            == 404
        )


def test_route_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=bogus").status_code == 422
        assert c.put(_URL, json={"scope": "bogus", "settings": {}}).status_code == 422


def test_route_bogus_scope_404_when_disabled(write_config, tmp_path) -> None:
    # Invisible-surface invariant (#819/#768 ordering fix): the capability gate
    # runs BEFORE the scope-enum check, so a bogus scope 404s (not 422) when
    # config-write is off — a differing 422 would leak that the endpoint exists.
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_URL}?scope=bogus").status_code == 404
        assert c.put(_URL, json={"scope": "bogus", "settings": {}}).status_code == 404


def test_route_confirm_mismatch_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "settings": {"model": "sonnet"},
            },
        )
        assert resp.status_code == 400


def test_route_confirm_runs_before_shape_check(write_config, tmp_path) -> None:
    # A bad shape AND a bad confirm together must 400 (confirm first), never 422 —
    # mirrors the hooks/permissions/CLAUDE.md ordering.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "settings": {"permissions": {}},
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
                "settings": {"env": {"FOO": 1}},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_owned_key_is_422_and_writes_nothing(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"permissions": {"defaultMode": "acceptEdits"}},
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
                "settings": {},
            },
        )
        assert resp.status_code == 400
        assert c.get(f"{_URL}?project=../escape").status_code == 400


def test_route_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"model": "opus"}', encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"model": "sonnet"},
                "hash": cw.hash_bytes(b"stale"),
            },
        )
        assert resp.status_code == 409


def test_route_project_write_read_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        r0 = c.get(f"{_URL}?project=alpha")
        assert r0.status_code == 200
        assert r0.json() == {
            "scope": "project",
            "project": "alpha",
            "settings": {},
            "hash": cw.hash_bytes(b""),
        }
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"model": "sonnet"},
                "hash": r0.json()["hash"],
            },
        )
        assert resp.status_code == 200
        r1 = c.get(f"{_URL}?project=alpha")
        assert r1.json()["settings"] == {"model": "sonnet"}


def test_route_user_write_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        r0 = c.get(f"{_URL}?scope=user")
        assert r0.status_code == 200
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "settings": {"model": "opus"},
                "hash": r0.json()["hash"],
            },
        )
        assert resp.status_code == 200
        r1 = c.get(f"{_URL}?scope=user")
        assert r1.json()["settings"] == {"model": "opus"}


def test_route_local_write_creates_gitignore_entry(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        r0 = c.get(f"{_URL}?scope=local&project=alpha")
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "settings": {"model": "sonnet"},
                "hash": r0.json()["hash"],
            },
        )
        assert resp.status_code == 200
    gitignore = (projects_root / "alpha" / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore.splitlines()


def test_route_local_scope_works_without_allow_user_scope(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        r0 = c.get(f"{_URL}?scope=local&project=alpha")
        assert r0.status_code == 200
        resp = c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "settings": {"model": "sonnet"},
                "hash": r0.json()["hash"],
            },
        )
        assert resp.status_code == 200


def test_route_write_missing_project_dir_is_404(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "does-not-exist",
                "confirm": "does-not-exist",
                "settings": {},
            },
        )
        assert resp.status_code == 404


def test_route_non_string_hash_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {},
                "hash": 123,
            },
        )
        assert resp.status_code == 422


def test_route_missing_settings_is_422_after_valid_confirm(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(_URL, json={"scope": "project", "project": "alpha", "confirm": "alpha"})
        assert resp.status_code == 422


def test_route_read_corrupt_project_file_is_422(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 422


def test_route_read_non_object_project_file_is_422(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("[]", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 422


def test_route_read_corrupt_user_file_is_422(write_config, tmp_path) -> None:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    app = create_app(load_config(cfg))
    with TestClient(app) as c:
        user_home = app.state.runner.claude_json.parent
        user_settings = user_home / ".claude" / "settings.json"
        user_settings.parent.mkdir(parents=True, exist_ok=True)
        user_settings.write_text("not json", encoding="utf-8")
        assert c.get(f"{_URL}?scope=user").status_code == 422


def test_route_read_corrupt_local_file_is_422(write_config, tmp_path, projects_root) -> None:
    local_settings = projects_root / "alpha" / ".claude" / "settings.local.json"
    local_settings.parent.mkdir(parents=True)
    local_settings.write_text("not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?scope=local&project=alpha").status_code == 422


def test_route_user_write_bad_shape_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "settings": {"env": {"FOO": 1}},
            },
        )
        assert resp.status_code == 422


def test_route_effective_project_read_error_is_422(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_EFFECTIVE_URL}?project=alpha").status_code == 422


def test_route_effective_local_read_error_is_422(write_config, tmp_path, projects_root) -> None:
    local_settings = projects_root / "alpha" / ".claude" / "settings.local.json"
    local_settings.parent.mkdir(parents=True)
    local_settings.write_text("not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_EFFECTIVE_URL}?project=alpha").status_code == 422


def test_route_effective_user_read_error_is_422(write_config, tmp_path, projects_root) -> None:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    app = create_app(load_config(cfg))
    with TestClient(app) as c:
        user_home = app.state.runner.claude_json.parent
        user_settings = user_home / ".claude" / "settings.json"
        user_settings.parent.mkdir(parents=True, exist_ok=True)
        user_settings.write_text("not json", encoding="utf-8")
        assert c.get(f"{_EFFECTIVE_URL}?project=alpha").status_code == 422


# --- effective/provenance route -------------------------------------------------------


def test_route_effective_reads_across_scopes(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        r0 = c.get(f"{_URL}?project=alpha")
        c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"model": "opus"},
                "hash": r0.json()["hash"],
            },
        )
        r_local0 = c.get(f"{_URL}?scope=local&project=alpha")
        c.put(
            _URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "settings": {"model": "sonnet"},
                "hash": r_local0.json()["hash"],
            },
        )
        eff = c.get(f"{_EFFECTIVE_URL}?project=alpha")
        assert eff.status_code == 200
        body = eff.json()
        assert body["project"] == "alpha"
        assert body["effective"]["model"] == {"value": "sonnet", "source": "local"}


def test_route_effective_omits_user_layer_when_allow_user_scope_off(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        r0 = c.get(f"{_URL}?project=alpha")
        c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"model": "opus"},
                "hash": r0.json()["hash"],
            },
        )
        eff = c.get(f"{_EFFECTIVE_URL}?project=alpha")
        assert eff.status_code == 200
        assert eff.json()["effective"]["model"] == {"value": "opus", "source": "project"}


def test_route_effective_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_EFFECTIVE_URL}?project=../escape").status_code == 400


def test_route_effective_missing_project_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(_EFFECTIVE_URL).status_code == 422
