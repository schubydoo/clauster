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
import shutil
import subprocess
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


@pytest.mark.parametrize("owned_key", sorted(cws.OWNED_KEYS))
def test_owned_key_error_names_its_tab(owned_key: str) -> None:
    # Every carved key maps to a real dashboard tab, and the refusal says which.
    tab = cws.OWNED_KEY_TABS[owned_key]
    assert tab in {"Permissions", "Hooks", "Plugins", "MCP"}
    with pytest.raises(cws.SettingsCarveError, match=f"{owned_key} on the {tab} tab"):
        cws.validate_misc_settings({owned_key: {}})


def test_auto_mode_is_carved_to_the_permissions_surface(tmp_path: Path) -> None:
    # autoMode is permission policy: the generic surface hides it on read and keeps it
    # verbatim on write, so the two surfaces never race on the key.
    assert "autoMode" in cws.OWNED_KEYS
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"autoMode": {"allow": ["$defaults", "x"]}, "model": "opus"}),
        encoding="utf-8",
    )
    view, h = cws.read_project_settings(tmp_path)
    assert view == {"model": "opus"}
    cws.write_project_settings(tmp_path, {"model": "sonnet"}, h)
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out == {"autoMode": {"allow": ["$defaults", "x"]}, "model": "sonnet"}


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


def test_local_write_gitignores_before_writing(tmp_path: Path) -> None:
    # Fail-closed ordering: the ignore entries must be in place BEFORE the write that
    # creates the secret-bearing `.bak`. Proven by making the write itself fail (stale
    # hash) and asserting the .gitignore was still updated — with the old write-then-ignore
    # order this file would not exist, and a failure between the two steps would leave the
    # previous env values trackable.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text('{"env": {"API": "old-secret"}}')
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        cws.write_project_local_settings(tmp_path, {"env": {"API": "new"}}, stale)
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert ".claude/settings.local.json" in lines
    assert ".claude/settings.local.json.bak" in lines


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


@pytest.mark.parametrize("key", ["extraKnownMarketplaces", "additionalMarketplaces"])
def test_marketplace_header_credentials_masked_on_read_and_kept_on_resend(
    tmp_path: Path, key: str
) -> None:
    # TASK-449.7: a plain-named header (`Cookie`) and a `headersHelper` command are not
    # secret-shaped by key name, so the generic heuristic alone showed them verbatim.
    stored = {
        "hosted": {
            "source": {
                "source": "url",
                "url": "https://plugins.example.com/m.json",
                "headers": {"Cookie": "session=C123"},
                "headersHelper": "/bin/h --t SEK",
            }
        }
    }
    _s, h0 = cws.read_project_settings(tmp_path)
    cws.write_project_settings(tmp_path, {key: stored}, h0)
    settings, h1 = cws.read_project_settings(tmp_path)
    assert "C123" not in json.dumps(settings) and "SEK" not in json.dumps(settings)
    source = settings[key]["hosted"]["source"]
    assert source["headers"] == {"Cookie": cw.REDACTION_SENTINEL}
    assert source["url"] == "https://plugins.example.com/m.json"
    # Resending the read view unchanged keeps the stored credentials.
    cws.write_project_settings(tmp_path, {key: settings[key], "model": "sonnet"}, h1)
    on_disk = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert on_disk[key] == stored


def test_write_sentinel_for_absent_key_is_refused(tmp_path: Path) -> None:
    # A resent sentinel for a var that was never stored has nothing to keep. It used to
    # be dropped and the save reported success (TASK-449.2): now the write is refused
    # whole, so the operator learns the value is gone and retypes it.
    with pytest.raises(cw.InvalidCandidateError, match=r"env\.GHOST holds a masked value"):
        cws.write_project_settings(tmp_path, {"env": {"GHOST": cw.REDACTION_SENTINEL}}, None)
    assert not (tmp_path / ".claude" / "settings.json").exists()


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
    # Count exact lines, not substrings: the ``.bak`` sibling entry (#F6) contains
    # ``.claude/settings.local.json`` as a substring, so the base entry is asserted
    # unduplicated by line, not by substring occurrence.
    assert gitignore.splitlines().count(".claude/settings.local.json") == 1


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


def test_local_write_backup_sibling_is_gitignored(tmp_path: Path) -> None:
    # F6 regression: the overwrite drops a real settings.local.json.bak holding the
    # PREVIOUS (unredacted) env values; that plaintext sibling must be gitignored
    # alongside the file itself, or a later ``git add`` could publish the old secret.
    _s0, h0 = cws.read_project_local_settings(tmp_path)
    cws.write_project_local_settings(tmp_path, {"env": {"API_KEY": "sk-first"}}, h0)
    _s1, h1 = cws.read_project_local_settings(tmp_path)
    cws.write_project_local_settings(tmp_path, {"env": {"API_KEY": "sk-second"}}, h1)

    backup = tmp_path / ".claude" / "settings.local.json.bak"
    assert backup.exists()  # the second (overwrite) write really dropped a .bak
    assert "sk-first" in backup.read_text(encoding="utf-8")  # it holds the prior env value

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/settings.local.json.bak" in lines


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")
def test_local_write_backup_sibling_matched_by_git_check_ignore(tmp_path: Path) -> None:
    # Prove git itself treats the real .bak as ignored (not just that the entry is
    # present): check-ignore exits 0 when the path matches an ignore rule.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _s0, h0 = cws.read_project_local_settings(tmp_path)
    cws.write_project_local_settings(tmp_path, {"env": {"API_KEY": "sk-first"}}, h0)
    _s1, h1 = cws.read_project_local_settings(tmp_path)
    cws.write_project_local_settings(tmp_path, {"env": {"API_KEY": "sk-second"}}, h1)

    result = subprocess.run(
        ["git", "check-ignore", ".claude/settings.local.json.bak"],
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == 0  # 0 => git ignores the .bak sibling


# --- scope-merge provenance (the novel #772 part) -------------------------------------


def test_effective_local_overrides_project_overrides_user() -> None:
    effective = cws._compute_effective_settings(
        user_misc={"model": "haiku", "cleanupPeriodDays": 10},
        project_misc={"model": "opus"},
        local_misc={"model": "sonnet"},
    )
    assert effective["model"] == {"value": "sonnet", "source": "local", "sources": ["local"]}
    # cleanupPeriodDays only defined at user scope — falls through to it.
    assert effective["cleanupPeriodDays"] == {
        "value": 10,
        "source": "user",
        "sources": ["user"],
    }


def test_effective_project_overrides_user_when_local_silent_on_key() -> None:
    effective = cws._compute_effective_settings(
        user_misc={"model": "haiku"},
        project_misc={"model": "opus"},
        local_misc={},
    )
    assert effective["model"] == {"value": "opus", "source": "project", "sources": ["project"]}


def test_effective_falls_through_to_user_when_others_absent() -> None:
    effective = cws._compute_effective_settings(
        user_misc={"model": "haiku"}, project_misc={}, local_misc={}
    )
    assert effective["model"] == {"value": "haiku", "source": "user", "sources": ["user"]}


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
    assert effective == {"model": {"value": "opus", "source": "project", "sources": ["project"]}}


def test_effective_env_merges_key_by_key() -> None:
    # Claude Code deep-merges objects across scopes (TASK-449.2, its merge customizer
    # in 2.1.318): local's A overrides user's A, and user's B still applies.
    effective = cws._compute_effective_settings(
        user_misc={"env": {"A": "1", "B": "2"}},
        project_misc={},
        local_misc={"env": {"A": "9"}},
    )
    assert effective["env"] == {
        "value": {"A": "9", "B": "2"},
        "source": "local",
        "sources": ["local", "user"],
    }


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


def test_route_owned_key_error_names_the_owning_tab(write_config, tmp_path, projects_root) -> None:
    """TASK-449.1: the maintainer's Raw JSON paste names the tab, not a bare refusal."""
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {
                    "permissions": {"allow": []},
                    "autoMode": {"allow": ["$defaults", "rule"]},
                },
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
    assert "autoMode on the Permissions tab" in detail
    assert "permissions on the Permissions tab" in detail
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


def test_route_read_huge_int_project_file_is_422(write_config, tmp_path, projects_root) -> None:
    # A settings.json holding an over-large integer literal raised a plain ValueError out of
    # json.loads (past sys.get_int_max_str_digits()), which escaped the seam handler and 500ed
    # this GET (#1449). The seam now rejects it, so the route answers a clean 422 — not a 500.
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"x": ' + "9" * 5000 + "}", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_URL}?project=alpha").status_code == 422


def test_route_read_non_finite_float_project_file_is_422(
    write_config, tmp_path, projects_root
) -> None:
    # json.loads accepts the bare NaN literal, and the float reached the route's return dict,
    # which Starlette renders with allow_nan=False and 500s (#1449). The seam now rejects it, so
    # the GET answers a clean 422 — not a 500.
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"x": NaN}', encoding="utf-8")
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
        assert body["effective"]["model"] == {
            "value": "sonnet",
            "source": "local",
            "sources": ["local"],
        }


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
        assert eff.json()["effective"]["model"] == {
            "value": "opus",
            "source": "project",
            "sources": ["project"],
        }


def test_route_effective_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_EFFECTIVE_URL}?project=../escape").status_code == 400


def test_route_effective_missing_project_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(_EFFECTIVE_URL).status_code == 422


# --- marketplace declarations on the Settings tab (TASK-449.7) ----------------------

_HOSTED = {
    "hosted": {
        "source": {
            "source": "url",
            "url": "https://plugins.example.com/m.json",
            "headers": {"Cookie": "session=C123"},
            "headersHelper": "/bin/h --t SEK",
        }
    }
}


def _store(tmp_path: Path, data: dict) -> str:
    _s, h0 = cws.read_project_settings(tmp_path)
    cws.write_project_settings(tmp_path, data, h0)
    return cws.read_project_settings(tmp_path)[1]


def _on_disk(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_marketplace_inline_plugin_args_mask_whole_and_round_trip(tmp_path: Path) -> None:
    # `args` shows as ONE sentinel, so a resend keeps the stored list whole (a
    # per-element mask would restore by position and shift values on an edit).
    stored = {
        "inline": {
            "source": {
                "source": "settings",
                "name": "inline",
                "plugins": [{"name": "p", "mcpServers": {"s": {"args": ["--v", "--key", "S1"]}}}],
            }
        }
    }
    h = _store(tmp_path, {"extraKnownMarketplaces": stored})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    assert view["inline"]["source"]["plugins"][0]["mcpServers"]["s"]["args"] == (
        cw.REDACTION_SENTINEL
    )
    cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == stored


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda v: {"hosted2": v["hosted"]}, id="entry-renamed"),
        pytest.param(
            lambda v: {
                "hosted": {
                    "source": {
                        **v["hosted"]["source"],
                        "headers": {"cookie": cw.REDACTION_SENTINEL},
                    }
                }
            },
            id="header-key-renamed",
        ),
    ],
)
def test_marketplace_write_with_orphaned_mask_is_rejected_whole(tmp_path: Path, edit) -> None:
    # merge_redacted would silently DROP a sentinel with nothing stored behind it, saving
    # a marketplace with its credential gone. Fail closed: refuse the write, change nothing.
    h = _store(tmp_path, {"extraKnownMarketplaces": _HOSTED})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    with pytest.raises(cw.InvalidCandidateError, match="masked value"):
        cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": edit(view)}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == _HOSTED


def _two_plugin_market() -> dict:
    return {
        "inline": {
            "source": {
                "source": "settings",
                "name": "inline",
                "plugins": [
                    {"name": "a", "mcpServers": {"s": {"env": {"K": "AAA"}}}},
                    {"name": "b", "mcpServers": {"s": {"env": {"K": "BBB"}}}},
                ],
            }
        }
    }


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda p: p[1:], id="delete-first"),
        pytest.param(lambda p: list(reversed(p)), id="reorder"),
        pytest.param(lambda p: [*p, p[0]], id="append-copy"),
        pytest.param(lambda p: [{**p[0], "description": "edited"}, p[1]], id="edit-masked-elem"),
    ],
)
def test_marketplace_list_edit_that_would_shift_a_mask_is_rejected(tmp_path: Path, edit) -> None:
    # merge_redacted restores a list by POSITION: deleting `a` would give `b` the
    # credential AAA, and a reorder would swap them. Refuse the write; change nothing.
    stored = _two_plugin_market()
    h = _store(tmp_path, {"extraKnownMarketplaces": stored})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    view["inline"]["source"]["plugins"] = edit(view["inline"]["source"]["plugins"])
    with pytest.raises(cw.InvalidCandidateError, match="masked value"):
        cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == stored


def test_marketplace_list_resend_unchanged_keeps_each_credential(tmp_path: Path) -> None:
    stored = _two_plugin_market()
    h = _store(tmp_path, {"extraKnownMarketplaces": stored})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == stored


def test_masked_whole_declaration_sent_under_a_new_key_is_rejected(tmp_path: Path) -> None:
    # A malformed string declaration that reads back as a bare sentinel, resent under the
    # alias key: the merge would drop it and save `{}`. Refuse instead.
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"extraKnownMarketplaces": "https://u:tok@h.example/m.git"}), encoding="utf-8"
    )
    view, h = cws.read_project_settings(tmp_path)
    assert view["extraKnownMarketplaces"] == cw.REDACTION_SENTINEL
    with pytest.raises(cw.InvalidCandidateError):
        cws.write_project_settings(tmp_path, {"additionalMarketplaces": cw.REDACTION_SENTINEL}, h)
    assert _on_disk(tmp_path) == {"extraKnownMarketplaces": "https://u:tok@h.example/m.git"}


def test_masked_element_with_a_shortened_nested_list_is_rejected(tmp_path: Path) -> None:
    # "Unchanged apart from its masks" covers a sentinel-free nested list too: an element
    # that holds a mask may not come back with a nested list shortened.
    stored = {
        "inline": {
            "source": {
                "source": "settings",
                "name": "inline",
                "plugins": [
                    {"name": "a", "tags": ["x", "y"], "mcpServers": {"s": {"env": {"K": "A"}}}}
                ],
            }
        }
    }
    h = _store(tmp_path, {"extraKnownMarketplaces": stored})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    view["inline"]["source"]["plugins"][0]["tags"] = ["x"]
    with pytest.raises(cw.InvalidCandidateError, match="masked value"):
        cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == stored


@pytest.mark.parametrize(
    "new_url",
    [
        pytest.param("https://evil.example.com/m.json", id="host"),
        pytest.param("http://plugins.example.com/m.json", id="scheme"),
        pytest.param("https://plugins.example.com:8443/m.json", id="port"),
        pytest.param("https://plugins.example.com/other.json", id="path"),
        pytest.param(None, id="url-removed"),
        # Python's urlsplit reads this host as plugins.example.com; a WHATWG parser
        # (what a Node fetch uses) reads evil.example.com. Only an exact compare is safe.
        pytest.param("https://evil.example.com\\@plugins.example.com/m.json", id="backslash"),
        pytest.param("https://Plugins.Example.com/m.json", id="host-case"),
        pytest.param("https://plugins.example.com:443/m.json", id="default-port"),
    ],
)
def test_marketplace_url_change_refuses_masked_credentials(tmp_path: Path, new_url) -> None:
    # A masked header kept by key while the url moves would send the stored credential
    # to the new host. Refuse the write whole; the operator retypes it for the new host.
    # Any change to the url text counts: a "same destination" parse is a second parser
    # that can disagree with Claude Code's.
    h = _store(tmp_path, {"extraKnownMarketplaces": _HOSTED})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    source = view["hosted"]["source"]
    if new_url is None:
        del source["url"]
    else:
        source["url"] = new_url
    with pytest.raises(cw.InvalidCandidateError, match="retype the credential for the new url"):
        cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == _HOSTED


def test_marketplace_url_unchanged_restores_masked_credentials(tmp_path: Path) -> None:
    # Same url, another field edited: the masked header and headersHelper are restored.
    h = _store(tmp_path, {"extraKnownMarketplaces": _HOSTED})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    view["hosted"]["autoUpdate"] = True
    cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    entry = _on_disk(tmp_path)["extraKnownMarketplaces"]["hosted"]
    assert entry["source"] == _HOSTED["hosted"]["source"]
    assert entry["autoUpdate"] is True


def test_nested_plugin_url_change_in_a_dict_shaped_plugins_is_refused(tmp_path: Path) -> None:
    # The binding covers every `url` in the entry, not only `source.url`: a malformed
    # dict-shaped `plugins` escapes the list rule, so the url check must catch it.
    stored = {
        "inline": {
            "source": {
                "source": "settings",
                "name": "inline",
                "plugins": {
                    "p": {
                        "source": {"source": "archive", "url": "https://r.example/p.zip"},
                        "headers": {"Cookie": "C"},
                    }
                },
            }
        }
    }
    h = _store(tmp_path, {"extraKnownMarketplaces": stored})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    view["inline"]["source"]["plugins"]["p"]["source"]["url"] = "https://evil.example/m.json"
    with pytest.raises(cw.InvalidCandidateError, match="url changed"):
        cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == stored


def test_masked_credential_url_resent_unchanged_keeps_url_and_headers(tmp_path: Path) -> None:
    # The url itself reads back masked (it carries user:pass), so it cannot have moved.
    stored = {
        "hosted": {
            "source": {
                "source": "url",
                "url": "https://bot:tok@plugins.example.com/m.json",
                "headers": {"Cookie": "session=C123"},
            }
        }
    }
    h = _store(tmp_path, {"extraKnownMarketplaces": stored})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    assert view["hosted"]["source"]["url"] == cw.REDACTION_SENTINEL
    cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == stored


def test_list_shaped_declaration_resent_unchanged_is_accepted(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"extraKnownMarketplaces": [_HOSTED["hosted"]]}), "utf-8")
    view, h = cws.read_project_settings(tmp_path)
    cws.write_project_settings(
        tmp_path, {"extraKnownMarketplaces": view["extraKnownMarketplaces"]}, h
    )
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == [_HOSTED["hosted"]]


def test_marketplace_url_change_without_masks_is_accepted(tmp_path: Path) -> None:
    # Retyping the credential for the new host is the documented way forward.
    h = _store(tmp_path, {"extraKnownMarketplaces": _HOSTED})
    moved = {
        "hosted": {
            "source": {
                "source": "url",
                "url": "https://new.example.com/m.json",
                "headers": {"Cookie": "session=NEW"},
            }
        }
    }
    cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": moved}, h)
    assert _on_disk(tmp_path)["extraKnownMarketplaces"] == moved


def test_marketplace_moved_to_alias_with_masks_is_rejected(tmp_path: Path) -> None:
    h = _store(tmp_path, {"extraKnownMarketplaces": _HOSTED})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    with pytest.raises(cw.InvalidCandidateError):
        cws.write_project_settings(tmp_path, {"additionalMarketplaces": view}, h)


def test_marketplace_secret_shaped_name_keeps_its_entry_editable(tmp_path: Path) -> None:
    stored = {"auth-tools": {"source": {"source": "github", "repo": "acme/auth-tools"}}}
    _store(tmp_path, {"extraKnownMarketplaces": stored})
    assert cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"] == stored


def test_malformed_marketplace_declaration_is_still_masked(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"extraKnownMarketplaces": [_HOSTED["hosted"]]}), encoding="utf-8"
    )
    view = cws.read_project_settings(tmp_path)[0]
    assert "C123" not in json.dumps(view) and "SEK" not in json.dumps(view)
