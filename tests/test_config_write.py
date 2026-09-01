"""Foundation plumbing for the config-write trust tier (#347/#687).

Covers the reusable seams (capability/scope 404 gate, type-the-name confirm,
validate-never-execute, stale-hash guard, path containment, structural redaction +
keep-stored, the subtree-merge writer) and the gated status route both on and off.

Any test that writes ~/.claude.json passes an explicit ``tmp_path`` file and runs
under the autouse HOME-isolation fixture — the live account is never touched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml
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


def test_require_capability_local_scope_404_when_disabled(projects_root: Path) -> None:
    cfg = _cfg(enabled=False, allow_user=False, projects_root=projects_root)
    with pytest.raises(HTTPException) as ei:
        cw.require_capability(cfg, "local")
    assert ei.value.status_code == 404


def test_require_capability_local_scope_ok_without_allow_user_scope(projects_root: Path) -> None:
    # Local scope carries NO extra opt-in (unlike user scope) — the base `enabled` flag
    # alone gates it, since (like project scope) it is confined to a single project.
    cfg = _cfg(enabled=True, allow_user=False, projects_root=projects_root)
    cw.require_capability(cfg, "local")  # no raise


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


def test_confirm_local_accepts_exact_suffixed_token() -> None:
    cw.require_confirm("local", "alpha", "alpha (local)")  # no raise


def test_confirm_local_rejects_plain_project_name() -> None:
    # The whole point of the third token: the plain project-scope confirm ("alpha")
    # must NOT also confirm a local-scope write on the same project.
    with pytest.raises(HTTPException) as ei:
        cw.require_confirm("local", "alpha", "alpha")
    assert ei.value.status_code == 400


def test_confirm_local_without_project_is_400() -> None:
    with pytest.raises(HTTPException) as ei:
        cw.require_confirm("local", None, "anything")
    assert ei.value.status_code == 400


def test_expected_token_local_is_suffixed_and_distinct_from_project() -> None:
    project_token = cw.expected_confirm_token("project", "alpha")
    local_token = cw.expected_confirm_token("local", "alpha")
    assert local_token == "alpha (local)"
    assert local_token != project_token
    assert cw.LOCAL_SCOPE_SUFFIX in local_token


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


# --- load_settings_json_obj (shared existing-file parse) ---------------------------


def test_load_settings_json_obj_parses_object_and_empty() -> None:
    assert cw.load_settings_json_obj(b'{"a": 1}') == {"a": 1}
    assert cw.load_settings_json_obj(b"   \n ") == {}


@pytest.mark.parametrize("raw", [b"[]", b"5", b'"x"', b"null"])
def test_load_settings_json_obj_rejects_non_object(raw: bytes) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        cw.load_settings_json_obj(raw)


def test_load_settings_json_obj_rejects_deeply_nested_json() -> None:
    # The contract is InvalidCandidateError only (the caller maps it to 422: "we will
    # not overwrite a file we could not parse"). A deeply-nested settings file — which
    # can arrive with a cloned repository — raises RecursionError out of CPython's
    # recursive JSON scanner before json can raise JSONDecodeError, and RecursionError
    # is not a ValueError, so it escaped the handler and left this code-executing write
    # tier raising outside its contract. It must fail closed as a structural error.
    with pytest.raises(cw.InvalidCandidateError, match="too deeply"):
        cw.load_settings_json_obj(b"[" * 100_000)


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


def test_redact_secret_hint_reaches_nested_dicts() -> None:
    # A nested dict used to re-derive the hint from its OWN keys, dropping the parent's
    # "this subtree is secret" signal — so {"auth": {"value": "..."}} came back unmasked.
    # That is not an exotic shape; it is how a lot of MCP server configs are written, and
    # this is the code-executing config-write tier, where under-masking leaks.
    for label, data in (
        ("one level", {"auth": {"value": "sk-live-AAA"}}),
        ("under api_key", {"api_key": {"inner": "sk-live-AAA"}}),
        ("three deep", {"auth": {"a": {"b": "sk-live-AAA"}}}),
        ("list inside", {"auth": {"v": ["sk-live-AAA"]}}),
    ):
        red = cw.redact_secrets(data)
        assert "sk-live-AAA" not in json.dumps(red), label


def test_redact_does_not_over_mask_outside_a_secret_subtree() -> None:
    # The hint must not bleed sideways: widening it to nested dicts should mask everything
    # UNDER a secret-shaped key and nothing else.
    red = cw.redact_secrets({"name": "hello", "port": 8080, "nested": {"k": "plain"}})
    assert red == {"name": "hello", "port": 8080, "nested": {"k": "plain"}}


def test_redact_top_level_scalar_masked_by_key_hint() -> None:
    # A scalar redacted directly (not via a dict) under a secret-shaped key.
    assert cw.redact_secrets("sk-live", "api_token") == cw.REDACTION_SENTINEL
    assert cw.redact_secrets("plain", "name") == "plain"


def test_redact_author_key_not_masked_but_auth_variants_are() -> None:
    # #958/DF-4: a bare ``auth`` substring over-matched ``author`` and masked a real
    # name/email. ``author`` is benign; ``auth`` and its credential-shaped relatives
    # must still mask — including COMPOUND keys joined by ``_``/camelCase (``auth_key``,
    # ``authHeader``, ``auth_cookie``), which an enumerated-suffix regex would silently
    # under-mask, so they are covered explicitly.
    assert (
        cw.redact_secrets("Jane Doe <jane@example.com>", "author") == "Jane Doe <jane@example.com>"
    )
    assert cw.redact_secrets("Jane Doe", "authors") == "Jane Doe"
    secret_keys = (
        "auth",
        "authn",
        "authentication",
        "authorization",
        "auth_token",
        "auth_key",
        "authKey",
        "AUTH_KEY",
        "authHeader",
        "auth_cookie",
        "unauthorized",
    )
    for secret_key in secret_keys:
        assert cw.redact_secrets("s3cr3t", secret_key) == cw.REDACTION_SENTINEL, secret_key


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


# --- #1368: a self-referential YAML alias ------------------------------------------------
#
# `yaml.safe_load` builds a recursive structure for `extra: &a [*a]` without complaint, so the
# parse seam accepted it and the crash landed on an unguarded consumer: `redact_secrets` walked
# it forever and a GET raised RecursionError -- a 500 on the code-executing tier whose parse
# errors are contracted to 422. Rejecting at the seam is the fail-closed direction and stops
# such a document reaching disk; a document an EARLIER release already wrote is already handled,
# because `_read_agent` catches InvalidCandidateError around the parse and degrades
# `frontmatter` to `{}` while still surfacing `content` (verified, not assumed).
_SELF_REF_HEADER = "name: x\ndescription: d\nextra: &a [*a]\n"


def test_the_self_referential_alias_reproducer_is_a_positive_control() -> None:
    """PIN: `safe_load` really does build a cycle here, and it really did break the walk."""
    value = yaml.safe_load(_SELF_REF_HEADER)
    assert value["extra"][0] is value["extra"]  # the list contains itself


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(_SELF_REF_HEADER, id="list-contains-itself"),
        pytest.param("a: &m {k: *m}\n", id="mapping-contains-itself"),
        pytest.param("a: &m {k: [{j: *m}]}\n", id="cycle-several-levels-down"),
        # `!!omap`/`!!pairs` construct a list of TUPLES, so the cycle runs through a tuple.
        # A walk that treats a tuple as a scalar accepts these -- and they do not even reach
        # `redact_secrets`: `jsonable_encoder` hits the cycle first, so the 500 lands on the
        # same route with a different traceback.
        pytest.param("extra: &a !!omap\n  - k: *a\n", id="omap-cycle-through-a-tuple"),
        pytest.param("extra: &a !!pairs\n  - k: *a\n", id="pairs-cycle-through-a-tuple"),
    ],
)
def test_load_frontmatter_yaml_rejects_a_self_referential_alias(header: str) -> None:
    # Fail closed at the parse seam: the write is refused, so the document never reaches disk.
    with pytest.raises(cw.InvalidCandidateError, match="self-referential"):
        cw.load_frontmatter_yaml(header, what="frontmatter")


def test_load_frontmatter_yaml_accepts_a_non_recursive_alias() -> None:
    # Cycle detection, NOT alias rejection. A plain alias is ordinary, legitimate YAML that
    # every consumer handles; refusing it would reject valid frontmatter -- `fuzz/seeds/
    # parse_frontmatter_fuzzer/alias_merge` is one. Asserting the IDENTITY, not just
    # not-None: a diamond really is the same object under two keys, which is exactly the
    # shape a naive "seen this id before" check would misread as a cycle.
    lists = cw.load_frontmatter_yaml("a: &x [1, 2]\nb: *x\n", what="frontmatter")
    assert lists["b"] is lists["a"]
    maps = cw.load_frontmatter_yaml("a: &x {k: v}\nb: *x\nc: *x\n", what="frontmatter")
    assert maps["b"] is maps["a"] and maps["c"] is maps["a"]
    omap = cw.load_frontmatter_yaml("extra: !!omap\n  - k: v\n", what="frontmatter")
    assert omap["extra"] == [("k", "v")]  # a tuple container, but acyclic
    deep = cw.load_frontmatter_yaml("[" * 40 + "]" * 40, what="frontmatter")
    assert deep is not None  # deep but finite


def test_load_frontmatter_yaml_does_not_blow_up_on_an_alias_pyramid() -> None:
    # A billion-laughs header: no cycle, so it must be ACCEPTED -- but each alias multiplies
    # the number of distinct paths, and a walk without a "fully explored" memo is O(paths).
    # Measured before the memo: safe_load parses this 304-byte header in 0.7ms while the
    # check took 1.64s, and a 424-byte one never finished. That runs at the WRITE seam,
    # synchronously, in a threadpool slot, on attacker-supplied bytes. The bound below is
    # ~4 orders of magnitude above the memoized cost (~20us), so it is not a timing flake.
    lines = ["a0: &a0 x"]
    for i in range(1, 8):
        lines.append(f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 8) + "]")
    header = "\n".join(lines) + "\n"

    started = time.perf_counter()
    assert cw.load_frontmatter_yaml(header, what="frontmatter") is not None
    assert time.perf_counter() - started < 0.5


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


# --- nested-subtree writer (local-scope MCP: projects[<path>].mcpServers) ----------


def test_write_nested_subtree_creates_missing_file(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"  # absent
    cw.write_nested_subtree(
        f, "projects", "/repo/alpha", "mcpServers", lambda current: {"s": {"command": "x"}}
    )
    out = json.loads(f.read_text(encoding="utf-8"))
    assert out == {"projects": {"/repo/alpha": {"mcpServers": {"s": {"command": "x"}}}}}


def test_write_nested_subtree_preserves_siblings_at_every_level(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    f.write_text(
        json.dumps(
            {
                "projects": {
                    "/repo/alpha": {"hasTrustDialogAccepted": True, "mcpServers": {"old": {}}},
                    "/repo/beta": {"hasTrustDialogAccepted": True},
                },
                "misc": 1,
            }
        ),
        encoding="utf-8",
    )

    def mutate(current: object) -> dict:
        servers = dict(current or {})
        servers["new"] = {"command": "/bin/x"}
        return servers

    cw.write_nested_subtree(f, "projects", "/repo/alpha", "mcpServers", mutate)

    out = json.loads(f.read_text(encoding="utf-8"))
    alpha = out["projects"]["/repo/alpha"]
    assert set(alpha["mcpServers"]) == {"old", "new"}  # merged, not replaced
    assert alpha["hasTrustDialogAccepted"] is True  # sibling subtree preserved
    assert out["projects"]["/repo/beta"]["hasTrustDialogAccepted"] is True  # other project intact
    assert out["misc"] == 1


def test_read_nested_subtree_missing_levels_are_none(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    assert cw.read_nested_subtree(f, "projects", "/repo/alpha", "mcpServers") is None
    f.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    assert cw.read_nested_subtree(f, "projects", "/repo/alpha", "mcpServers") is None
    f.write_text(json.dumps({"projects": {"/repo/alpha": {}}}), encoding="utf-8")
    assert cw.read_nested_subtree(f, "projects", "/repo/alpha", "mcpServers") is None


def test_read_nested_subtree_round_trip(tmp_path: Path) -> None:
    f = tmp_path / "claude.json"
    cw.write_nested_subtree(
        f, "projects", "/repo/alpha", "mcpServers", lambda current: {"s": {"command": "x"}}
    )
    assert cw.read_nested_subtree(f, "projects", "/repo/alpha", "mcpServers") == {
        "s": {"command": "x"}
    }


# --- gitignore-on-create (idempotent append, never rewritten/reordered) ------------


def test_ensure_gitignored_creates_missing_file(tmp_path: Path) -> None:
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".claude/settings.local.json\n"


def test_ensure_gitignored_appends_to_existing_content(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text == "node_modules/\n.claude/settings.local.json\n"


def test_ensure_gitignored_adds_missing_trailing_newline_before_append(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/", encoding="utf-8")  # no trailing \n
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text == "node_modules/\n.claude/settings.local.json\n"


def test_ensure_gitignored_idempotent_no_duplicate(tmp_path: Path) -> None:
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.count(".claude/settings.local.json") == 1


def test_ensure_gitignored_matches_entry_ignoring_surrounding_whitespace(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("  .claude/settings.local.json  \n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text.count(".claude/settings.local.json") == 1  # not double-appended


def test_ensure_gitignored_never_reorders_existing_lines(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("b\na\nc\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, "d")
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text == "b\na\nc\nd\n"  # existing order untouched, only appended


def test_ensure_gitignored_adds_backup_sibling_when_requested(tmp_path: Path) -> None:
    # F6: the backup-taking writers must also ignore the <name>.bak sibling.
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text == ".claude/settings.local.json\n.claude/settings.local.json.bak\n"


def test_ensure_gitignored_backup_sibling_off_by_default(tmp_path: Path) -> None:
    # Callers whose writer takes no backup get only the file entry -- no phantom
    # .bak line for a file that never exists (e.g. CLAUDE.local.md).
    cw.ensure_gitignored(tmp_path, "CLAUDE.local.md")
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text == "CLAUDE.local.md\n"


def test_ensure_gitignored_backup_sibling_idempotent(tmp_path: Path) -> None:
    # A second call adds nothing once both the file and its .bak are present.
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines.count(".claude/settings.local.json") == 1
    assert lines.count(".claude/settings.local.json.bak") == 1


def test_ensure_gitignored_adds_only_missing_backup_sibling(tmp_path: Path) -> None:
    # The base entry already present (an earlier pre-fix write), the .bak absent:
    # only the missing sibling is appended, existing content untouched.
    (tmp_path / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text == ".claude/settings.local.json\n.claude/settings.local.json.bak\n"


def test_project_local_settings_path() -> None:
    project_dir = Path("/repo/alpha")
    assert cw.project_local_settings_path(project_dir) == project_dir / ".claude" / (
        "settings.local.json"
    )


# --- line-oriented text redaction (the file/dir writer's read path) ----------------


def test_redact_secret_lines_masks_key_value_secret_line() -> None:
    text = "API_TOKEN=sk-live-deadbeef\nHOST=localhost\n"
    red = cw.redact_secret_lines(text)
    assert "sk-live-deadbeef" not in red
    assert f"API_TOKEN={cw.REDACTION_SENTINEL}\n" in red
    assert "HOST=localhost\n" in red  # non-secret key untouched


def test_redact_secret_lines_masks_colon_form() -> None:
    text = "password: hunter2\nname: alice\n"
    red = cw.redact_secret_lines(text)
    assert "hunter2" not in red
    assert f"password: {cw.REDACTION_SENTINEL}\n" in red
    assert "name: alice\n" in red


def test_redact_secret_lines_leaves_author_frontmatter_line() -> None:
    # #958/DF-4: an ``author:`` frontmatter line (e.g. a SKILL.md) is not a secret and
    # must survive the line-based redaction, while ``auth_token:`` is still masked.
    text = "author: Jane Doe\nauth_token: sk-live-deadbeef\n"
    red = cw.redact_secret_lines(text)
    assert "author: Jane Doe\n" in red
    assert "sk-live-deadbeef" not in red
    assert f"auth_token: {cw.REDACTION_SENTINEL}\n" in red


def test_redact_secret_lines_masks_interpolation_anywhere_in_line() -> None:
    text = "run with token ${SECRET_TOKEN} please\n"
    red = cw.redact_secret_lines(text)
    assert "${SECRET_TOKEN}" not in red
    assert cw.REDACTION_SENTINEL in red


def test_redact_secret_lines_masks_credential_url() -> None:
    text = "notify slack://T00000000@channel now\n"
    red = cw.redact_secret_lines(text)
    assert "T00000000" not in red
    assert cw.REDACTION_SENTINEL in red


def test_redact_secret_lines_preserves_line_count_and_endings() -> None:
    text = "a\nAPI_KEY=x\nb\n"
    red = cw.redact_secret_lines(text)
    assert red.count("\n") == text.count("\n")
    assert red.splitlines()[0] == "a"
    assert red.splitlines()[2] == "b"


def test_redact_secret_lines_passes_through_plain_text() -> None:
    text = "just a normal line\nanother one\n"
    assert cw.redact_secret_lines(text) == text


def test_redact_secret_lines_preserves_crlf_endings() -> None:
    text = "API_TOKEN=sk-live-x\r\nHOST=localhost\r\n"
    red = cw.redact_secret_lines(text)
    assert "sk-live-x" not in red
    assert red.count("\r\n") == 2
    assert f"API_TOKEN={cw.REDACTION_SENTINEL}\r\n" in red
    assert "HOST=localhost\r\n" in red


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


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", "/api/config-write/mcp?scope=bogus", None),
        ("POST", "/api/config-write/mcp/server", {"scope": "bogus"}),
        ("GET", "/api/config-write/permissions?scope=bogus", None),
        ("GET", "/api/config-write/hooks?scope=bogus", None),
        ("PUT", "/api/config-write/mcp", {"scope": "bogus"}),
        ("PUT", "/api/config-write/permissions", {"scope": "bogus"}),
        ("PUT", "/api/config-write/hooks", {"scope": "bogus"}),
    ],
)
def test_disabled_surface_404s_even_for_bogus_scope(write_config, tmp_path, method, url, body):
    # #819/#768 invisible-surface invariant: the capability gate runs BEFORE the scope-enum
    # check on every config-write handler, so a disabled surface 404s for ANY request (a
    # bogus scope included) instead of leaking that the endpoint exists via a differing 422.
    # (Regression: mcp/permissions/hooks + mcp/server + the shared PUT helper were scope-first.)
    with _client(write_config, tmp_path, "") as c:
        assert c.request(method, url, json=body).status_code == 404
    # When ENABLED the surface is reachable, so the same bogus scope is safely a 422.
    enabled = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
    with _client(write_config, tmp_path, enabled) as c:
        assert c.request(method, url, json=body).status_code == 422


def test_interp_scan_matches_the_regex_it_replaced_exactly():
    """The hand-rolled ``${…}`` scan is byte-equivalent to the regex it replaced.

    The old ``\\$\\{[^}]+\\}`` was quadratic on hostile input (CodeQL py/polynomial-redos),
    so it was replaced by a linear hand scan. Equivalence is the whole risk of that swap —
    a redaction helper that stops matching something the regex matched leaks — so it is
    asserted exhaustively over every short string built from the characters that can
    possibly matter, rather than a handful of chosen examples.
    """
    import itertools
    import re

    old = re.compile(r"\$\{[^}]+\}")
    sent = cw.REDACTION_SENTINEL
    for length in range(6):
        for combo in itertools.product("${}a$ ", repeat=length):
            s = "".join(combo)
            assert bool(old.search(s)) is cw._has_interp(s), f"bool differs for {s!r}"
            assert old.sub(sent, s) == cw._mask_interps(s, sent), f"sub differs for {s!r}"


def test_interp_scan_is_linear_on_hostile_input():
    """A pathological value must not stall the request that redacts it.

    `${` repeated with no closing brace is the shape that made the old regex restart a
    full-length scan at every opener: measured ~2s at 128 KB and ~2 minutes at 1 MB, on a
    value that arrives unbounded from a cloned repository's `.claude/settings.json`. The
    bound is deliberately loose (the scan is microseconds) so it flags a return to
    quadratic behaviour without flaking on a loaded CI runner.
    """
    import time

    hostile = "${" * 500_000  # 1 MB, no closing brace anywhere
    start = time.perf_counter()
    assert cw._has_interp(hostile) is False
    assert cw._mask_interps(hostile, cw.REDACTION_SENTINEL) == hostile
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"interpolation scan took {elapsed:.2f}s — quadratic behaviour is back"


def test_url_and_kv_scans_match_the_regexes_they_replaced():
    """The two hand-rolled line scans are byte-equivalent to the regexes they replaced.

    Both were quadratic (see the linearity test below). Equivalence is the whole risk of
    replacing them, and the failure mode is silent under-masking, so it is asserted
    exhaustively over the characters that can matter for each pattern rather than by
    example. Includes ``-https://u@h`` specifically: the obvious ``(?<![a-z0-9+.-])``
    lookbehind "fix" leaves that string completely unmasked, which is the leak direction.
    """
    import itertools
    import re

    old_url = re.compile(r"[a-z][a-z0-9+.\-]*://[^/@\s]+@", re.IGNORECASE)
    old_kv = re.compile(r"^(?P<prefix>\s*[\w.\-]+\s*[:=]\s*)(?P<value>\S.*?)(?P<trail>\s*)$")
    repl = f"{cw.REDACTION_SENTINEL}@"

    def kv_old(body: str):
        m = old_kv.match(body)
        return None if m is None else (m.group("prefix"), m.group("trail"))

    # The Unicode alphabet is NOT decorative. `[a-z]` under re.IGNORECASE is Unicode-aware:
    # U+212A KELVIN SIGN folds to `k`, U+017F LONG S to `s`, U+0130/U+0131 likewise. An
    # ASCII-only scan silently stopped masking `\u212a://user@host`, leaking the userinfo —
    # an earlier revision of this fix shipped exactly that, and an ASCII-only test alphabet
    # is what let it through.
    for alphabet, maxlen in (("a:/@.-", 7), ("k = v", 6), ("a\u212a:/@-", 6)):
        for length in range(maxlen):
            for combo in itertools.product(alphabet, repeat=length):
                s = "".join(combo)
                assert old_url.sub(repl, s) == cw._mask_url_creds(s, repl), f"url differs {s!r}"
                assert kv_old(s) == cw._split_kv_line(s), f"kv differs {s!r}"

    # The lookbehind trap, pinned explicitly so a future "simplification" cannot reintroduce it.
    assert cw._mask_url_creds("-https://u@h", repl) == f"-{cw.REDACTION_SENTINEL}@h"

    # EVERY codepoint the old pattern's `[a-z]` accepts under IGNORECASE, as a single-char
    # scheme — the shape that leaked. Enumerated rather than sampled: the class is small
    # (52 ASCII letters plus 4 case-folding characters) and this is the whole of it.
    for code in range(0x11000):
        ch = chr(code)
        if re.match(r"[a-z]", ch, re.IGNORECASE):
            probe = f"{ch}://u@h"
            assert old_url.sub(repl, probe) == cw._mask_url_creds(probe, repl), (
                f"scheme char U+{code:04X} diverges — under-masking leaks the userinfo"
            )


def test_redact_secret_lines_is_linear_on_every_hostile_shape():
    """No single line can stall the redaction pass, whichever pattern it targets.

    Three separate quadratics lived on three consecutive lines of this function. The
    ``${`` flood needed a crafted payload; the other two did not — an ordinary long
    alphanumeric run (a minified blob, a long base64 value, a one-line JSON file) cost
    0.18s at 8 KB and 11.25s at 64 KB, and ``key: value`` with a long internal whitespace
    run cost 8.6s at 64 KB. Bounds are loose because each shape now runs in milliseconds.
    """
    import time

    shapes = {
        "interpolation flood": "${" * 500_000,
        "alphanumeric run": "a" * 256_000,
        "internal whitespace run": "token: a" + " " * 64_000 + "b",
        "minified blob": "a." * 500_000,
    }
    for label, payload in shapes.items():
        start = time.perf_counter()
        cw.redact_secret_lines(payload)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"{label} took {elapsed:.2f}s — quadratic behaviour is back"
