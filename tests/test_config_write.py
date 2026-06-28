"""Foundation plumbing for the config-write trust tier (#347/#687).

Covers the reusable seams (capability/scope 404 gate, type-the-name confirm,
validate-never-execute, stale-hash guard, path containment, structural redaction +
keep-stored, the subtree-merge writer) and the gated status route both on and off.

Any test that writes ~/.claude.json passes an explicit ``tmp_path`` file and runs
under the autouse HOME-isolation fixture — the live account is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster.app import create_app
from clauster.config import ClausterConfig, load_config

# --- capability + scope 404 gate (fail closed) -------------------------------------


def _cfg(*, enabled: bool, allow_user: bool, projects_root: Path) -> ClausterConfig:
    return ClausterConfig.model_validate(
        {
            "projects_root": str(projects_root),
            "config_write": {"enabled": enabled, "allow_user_scope": allow_user},
        }
    )


def test_flags_default_false(projects_root: Path) -> None:
    cfg = ClausterConfig.model_validate({"projects_root": str(projects_root)})
    assert cfg.config_write.enabled is False
    assert cfg.config_write.allow_user_scope is False


def test_require_capability_404_when_disabled(projects_root: Path) -> None:
    cfg = _cfg(enabled=False, allow_user=False, projects_root=projects_root)
    with pytest.raises(HTTPException) as ei:
        cw.require_capability(cfg, "project")
    assert ei.value.status_code == 404


def test_require_capability_project_ok_when_enabled(projects_root: Path) -> None:
    cfg = _cfg(enabled=True, allow_user=False, projects_root=projects_root)
    cw.require_capability(cfg, "project")  # no raise


def test_require_capability_user_scope_404_when_user_off(projects_root: Path) -> None:
    cfg = _cfg(enabled=True, allow_user=False, projects_root=projects_root)
    with pytest.raises(HTTPException) as ei:
        cw.require_capability(cfg, "user")
    assert ei.value.status_code == 404


def test_require_capability_user_scope_ok_when_both_on(projects_root: Path) -> None:
    cfg = _cfg(enabled=True, allow_user=True, projects_root=projects_root)
    cw.require_capability(cfg, "user")  # no raise


# --- type-the-name confirm (400 on mismatch, before any I/O) -----------------------


def test_confirm_project_accepts_exact_name() -> None:
    cw.require_confirm("project", "alpha", "alpha")  # no raise


def test_confirm_project_rejects_mismatch() -> None:
    with pytest.raises(HTTPException) as ei:
        cw.require_confirm("project", "alpha", "beta")
    assert ei.value.status_code == 400


def test_confirm_project_rejects_non_string() -> None:
    with pytest.raises(HTTPException) as ei:
        cw.require_confirm("project", "alpha", None)
    assert ei.value.status_code == 400


def test_confirm_user_requires_literal_token() -> None:
    cw.require_confirm("user", None, cw.USER_SCOPE_TOKEN)  # no raise
    with pytest.raises(HTTPException) as ei:
        cw.require_confirm("user", None, "alpha")
    assert ei.value.status_code == 400


def test_confirm_project_without_project_is_400() -> None:
    with pytest.raises(HTTPException) as ei:
        cw.require_confirm("project", None, "anything")
    assert ei.value.status_code == 400


def test_expected_token_is_server_derived() -> None:
    assert cw.expected_confirm_token("project", "alpha") == "alpha"
    assert cw.expected_confirm_token("user", None) == cw.USER_SCOPE_TOKEN


# --- path containment (reject escape before I/O) -----------------------------------


def test_resolve_project_dir_contained(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    resolved = cw.resolve_project_dir(tmp_path, "alpha")
    assert resolved == (tmp_path / "alpha").resolve()


def test_resolve_project_dir_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(cw.PathEscapeError):
        cw.resolve_project_dir(tmp_path, "../escape")


def test_resolve_project_dir_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "alpha"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported here")
    with pytest.raises(cw.PathEscapeError):
        cw.resolve_project_dir(root, "alpha")


# --- validate-never-execute (422; nothing written) --------------------------------


def test_validate_candidate_rejects_bad_shape() -> None:
    def validator(candidate: object) -> None:
        if not isinstance(candidate, dict):
            raise ValueError("must be an object")

    with pytest.raises(cw.InvalidCandidateError):
        cw.validate_candidate(["not", "a", "dict"], validator)


def test_validate_candidate_passes_good_shape() -> None:
    cw.validate_candidate({"ok": True}, lambda c: None)  # no raise


def test_validate_candidate_preserves_invalid_candidate_error() -> None:
    def validator(candidate: object) -> None:
        raise cw.InvalidCandidateError("explicit")

    with pytest.raises(cw.InvalidCandidateError, match="explicit"):
        cw.validate_candidate({}, validator)


# --- stale-hash external-edit guard (409) ------------------------------------------


def test_guard_unchanged_passes_on_match(tmp_path: Path) -> None:
    f = tmp_path / "settings.json"
    f.write_bytes(b'{"a": 1}')
    h = cw.hash_bytes(f.read_bytes())
    assert cw.guard_unchanged(f, h) == b'{"a": 1}'


def test_guard_unchanged_raises_on_drift(tmp_path: Path) -> None:
    f = tmp_path / "settings.json"
    f.write_bytes(b'{"a": 1}')
    stale = cw.hash_bytes(b'{"a": 0}')  # what we *thought* we loaded
    with pytest.raises(cw.StaleConfigWriteError):
        cw.guard_unchanged(f, stale)


def test_guard_unchanged_missing_file_is_empty_digest(tmp_path: Path) -> None:
    f = tmp_path / "missing.json"
    assert cw.guard_unchanged(f, cw.hash_bytes(b"")) == b""


# --- structural redaction (never assemble a live secret) ---------------------------


def test_redact_masks_secret_shaped_values() -> None:
    data = {
        "mcpServers": {
            "srv": {
                "command": "/bin/foo",
                "env": {"API_TOKEN": "sk-live-deadbeef", "HOST": "localhost"},
            }
        },
        "notify": "slack://T00000000@channel",
        "interp": "${SECRET}",
    }
    red = cw.redact_secrets(data)
    assert red["mcpServers"]["srv"]["command"] == "/bin/foo"  # non-secret kept
    assert red["mcpServers"]["srv"]["env"]["API_TOKEN"] == cw.REDACTION_SENTINEL
    assert red["mcpServers"]["srv"]["env"]["HOST"] == "localhost"
    assert red["notify"] == cw.REDACTION_SENTINEL  # token-bearing URL masked
    assert red["interp"] == cw.REDACTION_SENTINEL  # ${...} masked
    # The live secret never appears anywhere in the assembled view.
    assert "sk-live-deadbeef" not in json.dumps(red)


def test_redact_recurses_into_lists() -> None:
    data = {"args": ["--token", "${SECRET}", "plain"], "nested": [{"password": "p"}]}
    red = cw.redact_secrets(data)
    # List items recurse: a ${...} item is masked, a plain one passes through.
    assert red["args"] == ["--token", cw.REDACTION_SENTINEL, "plain"]
    assert red["nested"][0]["password"] == cw.REDACTION_SENTINEL


def test_redact_top_level_scalar_masked_by_key_hint() -> None:
    # A scalar redacted directly (not via a dict) under a secret-shaped key.
    assert cw.redact_secrets("sk-live", "api_token") == cw.REDACTION_SENTINEL
    assert cw.redact_secrets("plain", "name") == "plain"


def test_merge_redacted_keep_stored_on_unchanged_sentinel() -> None:
    stored = {"API_TOKEN": "sk-live-real", "HOST": "old"}
    incoming = {"API_TOKEN": cw.REDACTION_SENTINEL, "HOST": "new"}
    merged = cw.merge_redacted(incoming, stored)
    assert merged["API_TOKEN"] == "sk-live-real"  # kept (was sentinel)
    assert merged["HOST"] == "new"  # changed (real value)


def test_merge_redacted_sentinel_for_absent_key_is_dropped() -> None:
    merged = cw.merge_redacted({"API_TOKEN": cw.REDACTION_SENTINEL}, {})
    assert "API_TOKEN" not in merged  # nothing stored to keep ⇒ dropped


def test_merge_redacted_dict_over_none_stored_drops_sentinel() -> None:
    # write_subtree passes data.get(subtree_key) — None for an absent subtree. A sentinel
    # for a never-stored key must be DROPPED, never written verbatim as the literal sentinel.
    merged = cw.merge_redacted({"API_TOKEN": cw.REDACTION_SENTINEL, "HOST": "h"}, None)
    assert "API_TOKEN" not in merged
    assert merged["HOST"] == "h"
    assert cw.REDACTION_SENTINEL not in str(merged)


def test_merge_redacted_scalar_sentinel_keeps_stored() -> None:
    assert cw.merge_redacted(cw.REDACTION_SENTINEL, "kept") == "kept"


def test_merge_redacted_list_keeps_stored_secret_and_drops_orphan_sentinel() -> None:
    # redact_secrets masks secrets INSIDE lists (e.g. a token in an MCP `args` list), so
    # merge must restore from them symmetrically — a list sentinel must never be written
    # verbatim. A sentinel at index i keeps stored_list[i]; a sentinel past the stored list
    # (or over a non-list stored) is dropped, never written as the literal sentinel.
    stored = ["--token", "sk-live-secret", "--flag"]
    incoming = ["--token", cw.REDACTION_SENTINEL, "--flag"]
    assert cw.merge_redacted(incoming, stored) == ["--token", "sk-live-secret", "--flag"]
    # Orphan sentinel (no stored counterpart / stored is None) is dropped, not written.
    assert cw.merge_redacted([cw.REDACTION_SENTINEL], None) == []
    assert cw.REDACTION_SENTINEL not in str(cw.merge_redacted(["x", cw.REDACTION_SENTINEL], ["x"]))


def test_redact_then_merge_round_trip_list_secret_survives() -> None:
    # End-to-end: redact a config whose args list holds a secret, then merge the redacted
    # view back over the stored value with nothing else touched — the real secret survives.
    stored = {"mcpServers": {"s": {"args": ["--token", "${REAL_SECRET}"]}}}
    redacted = cw.redact_secrets(stored)
    assert redacted["mcpServers"]["s"]["args"][1] == cw.REDACTION_SENTINEL  # masked in the list
    merged = cw.merge_redacted(redacted, stored)
    assert merged["mcpServers"]["s"]["args"] == ["--token", "${REAL_SECRET}"]  # restored


def test_merge_redacted_sentinel_cannot_exfiltrate_sibling_secret() -> None:
    # The security-critical property: a sent-back sentinel restores ONLY the same key's
    # stored value — it can never be replayed to read a *different* stored secret out.
    merged = cw.merge_redacted({"a": cw.REDACTION_SENTINEL}, {"a": "secretA", "b": "secretB"})
    assert merged["a"] == "secretA"  # same key's stored value restored
    assert "secretB" not in str(merged)  # sibling secret never surfaced
    assert "b" not in merged  # and the untouched key is not echoed back at all


# --- subtree-merge writer round-trip (flock + atomic, sibling-preserving) ----------


def test_write_subtree_merges_one_key_preserving_others(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_text(
        json.dumps({"projects": {"/p": {"hasTrustDialogAccepted": True}}, "misc": 1}),
        encoding="utf-8",
    )

    def mutate(current: object) -> dict:
        servers = dict(current or {})
        servers["srv"] = {"command": "/bin/foo"}
        return servers

    cw.write_subtree(f, "mcpServers", mutate)

    out = json.loads(f.read_text(encoding="utf-8"))
    assert out["mcpServers"]["srv"]["command"] == "/bin/foo"  # subtree written
    assert out["projects"]["/p"]["hasTrustDialogAccepted"] is True  # sibling preserved
    assert out["misc"] == 1
    assert f.with_suffix(f.suffix + ".bak").exists()  # one-time backup


def test_write_subtree_creates_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"  # absent
    cw.write_subtree(f, "mcpServers", lambda current: {"srv": {"command": "/bin/x"}})
    out = json.loads(f.read_text(encoding="utf-8"))
    assert out == {"mcpServers": {"srv": {"command": "/bin/x"}}}


# --- the gated status route (404 off / flags on), with FastAPI lifespan ------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path, extra: str) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


def test_status_route_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get("/api/config-write/status").status_code == 404


def test_status_route_returns_flags_when_enabled(write_config, tmp_path) -> None:
    extra = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
    with _client(write_config, tmp_path, extra) as c:
        resp = c.get("/api/config-write/status")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True, "allow_user_scope": True}


def test_status_route_user_scope_flag_independent(write_config, tmp_path) -> None:
    extra = "config_write:\n  enabled: true\n"
    with _client(write_config, tmp_path, extra) as c:
        resp = c.get("/api/config-write/status")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True, "allow_user_scope": False}
