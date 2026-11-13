"""Foundation plumbing for the config-write trust tier (#347/#687).

Covers the reusable seams (capability/scope 404 gate, type-the-name confirm,
validate-never-execute, stale-hash guard, path containment, structural redaction +
keep-stored, the subtree-merge writer) and the gated status route both on and off.

Any test that writes ~/.claude.json passes an explicit ``tmp_path`` file and runs
under the autouse HOME-isolation fixture — the live account is never touched.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import json
import math
import time
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_plugins, config_write_skills, config_write_subagents
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
    # recursive JSON scanner on every supported interpreter (the scanner's message
    # changed from "maximum recursion depth exceeded" on <=3.13 to "Stack overflow" on
    # 3.14+, but not the type), and RecursionError is not a ValueError, so without the
    # handler's arm it escapes and leaves this code-executing write tier raising outside
    # its contract. It must fail closed as a structural error. The type the test pins is
    # InvalidCandidateError; the RecursionError arm maps this input to the fixed "nested
    # too deeply to parse" message, so "too deeply" is what matches. The "|not valid JSON"
    # alternative is a defensive allowance, not an asserted behavior.
    with pytest.raises(cw.InvalidCandidateError, match="too deeply|not valid JSON"):
        cw.load_settings_json_obj(b"[" * 100_000)


def test_load_settings_json_obj_rejects_recursion_overflow_on_any_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins the RecursionError handler directly, independent of the scanner's exact error
    # text: the real deep-nesting input above raises RecursionError on every supported
    # interpreter, and this arm is what maps it to the structural contract.
    def _overflow(_text: str) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(cw.json, "loads", _overflow)
    with pytest.raises(cw.InvalidCandidateError, match="too deeply"):
        cw.load_settings_json_obj(b"[[1]]")


def test_load_settings_json_obj_rejects_huge_integer() -> None:
    # Same class as #1415's frontmatter seam, on the JSON sibling (#1449): json builds an int
    # literal with int(<digits>), which raises a plain ValueError — NOT a JSONDecodeError — past
    # sys.get_int_max_str_digits(). That escaped the handler and 500ed the read route where the
    # contract is 422. Reject as a structural error, class name only, never the value.
    raw = b'{"x": ' + b"9" * 5000 + b"}"
    with pytest.raises(cw.InvalidCandidateError, match="integer too large"):
        cw.load_settings_json_obj(raw)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"x": NaN}', id="nan"),
        pytest.param(b'{"x": Infinity}', id="infinity"),
        pytest.param(b'{"x": -Infinity}', id="neg-infinity"),
        pytest.param(b'{"x": 1e400}', id="overflow-float"),
        pytest.param(b'{"a": [1, NaN]}', id="nested-nan"),
    ],
)
def test_load_settings_json_obj_rejects_non_finite_float(raw: bytes) -> None:
    # json.loads accepts the bare NaN/Infinity/-Infinity literals by default, and an overflowing
    # magnitude like 1e400 parses straight to inf. Each reaches the route's return dict, which
    # Starlette renders with allow_nan=False and 500s (#1449). Reject at the seam as a 422.
    with pytest.raises(cw.InvalidCandidateError, match="non-finite float"):
        cw.load_settings_json_obj(raw)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"x": ' + b"9" * 100 + b"}", id="ordinary-large-int"),
        pytest.param(b'{"x": 1.5}', id="ordinary-float"),
        pytest.param(b'{"x": 1e300}', id="finite-big-float"),
    ],
)
def test_load_settings_json_obj_keeps_ordinary_numbers(raw: bytes) -> None:
    # Positive control: the huge-int and non-finite guards must not refuse an ordinary large
    # integer, a plain float, or a finite large-magnitude float that all serialize fine.
    assert isinstance(cw.load_settings_json_obj(raw), dict)


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


# --- #1393: the containers `safe_load` builds that the redactor used to walk straight past --
#
# `redact_secrets` recursed `dict | list` while `safe_load` also builds a TUPLE (`!!omap`,
# `!!pairs`) and a SET (`!!set`). A tuple/set was neither masked nor copied -- returned by
# identity, then rendered as a JSON array by `jsonable_encoder`. All three detection rules
# leaked through it (secret-shaped key, `${interp}`, credential URL), and the "deep copy"
# promise did not hold for a mutable value underneath one. Reproduced end to end through
# `_read_agent` before the fix; `_YAML_CONTAINERS` is now the single enumeration both this
# walk and `_expands_beyond` use.
_TUPLE_SET_HEADERS = [
    pytest.param("auth: !!omap\n  - token: sk-live-AAA\n", id="omap-under-secret-key"),
    pytest.param("auth: !!pairs\n  - token: sk-live-AAA\n", id="pairs-under-secret-key"),
    pytest.param("token: !!set\n  ? sk-live-AAA\n", id="set-under-secret-key"),
    pytest.param("x: !!omap\n  - k: ${sk-live-AAA}\n", id="omap-holding-an-interp"),
    pytest.param("x: !!pairs\n  - k: slack://sk-live-AAA@chan\n", id="pairs-holding-a-url"),
    pytest.param("x: !!set\n  ? ${sk-live-AAA}\n", id="set-holding-an-interp"),
]


def _every_value(node: object) -> list[object]:
    """Flatten a parsed header into every value it holds, the containers themselves included."""
    out: list[object] = [node]
    children = node.values() if isinstance(node, dict) else node
    if isinstance(node, dict | list | tuple | set):
        for child in children:
            out += _every_value(child)
    return out


@pytest.mark.parametrize("header", _TUPLE_SET_HEADERS)
def test_the_tuple_and_set_redaction_reproducer_is_a_positive_control(header: str) -> None:
    """PIN: `safe_load` really builds a tuple/set here, which is why the walk must cover them."""
    value = yaml.safe_load(header)
    built = {type(v) for v in _every_value(value)}
    assert built & {tuple, set}, built
    assert "sk-live-AAA" in json.dumps(value, default=list)  # the secret really is in there


@pytest.mark.parametrize("header", _TUPLE_SET_HEADERS)
def test_redact_masks_a_secret_inside_an_omap_pairs_or_set(header: str) -> None:
    red = cw.redact_secrets(yaml.safe_load(header))
    # `default=` is not needed any more, and that is half the point: a tuple/set is emitted as
    # a list, so the redacted view is plain JSON rather than something the encoder reshapes.
    assert "sk-live-AAA" not in json.dumps(red)
    assert cw.REDACTION_SENTINEL in json.dumps(red)


# --- #1450: a `!!binary` secret is `bytes`, which `_is_secretish` used to reject outright -----
#
# `_is_secretish` returned False for any non-`str` value, so a secret spelled as a UTF-8
# `!!binary` (base64 of the plain secret) fell through `redact_secrets`' scalar return and was
# handed back by IDENTITY, then decoded into the display by `jsonable_encoder`. Same shape as the
# #1393 omap leak: a spelling `safe_load` accepts that the redactor walked straight past. Bytes
# now run all three detection rules -- a secret-shaped KEY masks, and so does secret-shaped
# CONTENT (a credential URL or `${interp}`) once the display path's UTF-8 decode is applied. A
# `bytes` value that is neither still decodes unchanged, matching the plain-string spelling.
def test_redact_masks_a_binary_spelled_secret_under_a_secret_key() -> None:
    # `!!binary "c2stbGl2ZS1BQUE="` is base64 of `sk-live-AAA`; `safe_load` parses it to bytes.
    data = cw.load_frontmatter_yaml('api_token: !!binary "c2stbGl2ZS1BQUE="\n', what="frontmatter")
    assert data["api_token"] == b"sk-live-AAA"  # positive control: the secret really is bytes
    red = cw.redact_secrets(data)
    assert red == {"api_token": cw.REDACTION_SENTINEL}
    assert "sk-live-AAA" not in json.dumps(red)


def test_redact_decodes_a_binary_value_that_is_not_secret_shaped() -> None:
    # `!!binary "aGk="` is base64 of `hi`: an ordinary key AND benign content, so the bytes
    # pass through unchanged -- `jsonable_encoder` decodes them for the response as before.
    data = cw.load_frontmatter_yaml('description: !!binary "aGk="\n', what="frontmatter")
    assert data["description"] == b"hi"
    assert cw.redact_secrets(data) == {"description": b"hi"}


def test_redact_masks_a_binary_whose_decoded_content_is_secret_shaped() -> None:
    # A `!!binary` value under an ORDINARY key still masks when the decoded text is itself
    # secret-shaped -- a credential URL or `${interp}` -- because the display path decodes the
    # bytes, so redaction must not depend on the base64 spelling any more than on `!!omap`.
    # `cG9zdGdyZXM6Ly91OnB3QGgvZGI=` is base64 of `postgres://u:pw@h/db`.
    url = cw.load_frontmatter_yaml(
        'endpoint: !!binary "cG9zdGdyZXM6Ly91OnB3QGgvZGI="\n', what="frontmatter"
    )
    assert url["endpoint"] == b"postgres://u:pw@h/db"  # positive control: the credential is bytes
    assert cw.redact_secrets(url) == {"endpoint": cw.REDACTION_SENTINEL}
    assert cw.redact_secrets(b"${SECRET}", "note") == cw.REDACTION_SENTINEL


def test_redact_binary_top_level_scalar_masked_by_key_hint_not_by_benign_content() -> None:
    assert cw.redact_secrets(b"sk-live-AAA", "api_token") == cw.REDACTION_SENTINEL
    assert cw.redact_secrets(b"sk-live-AAA", "name") == b"sk-live-AAA"


def test_redact_deep_copies_a_mutable_value_reached_through_a_tuple() -> None:
    # The docstring's first line promises a deep copy. A tuple returned by identity broke that
    # promise for anything mutable underneath it: mutating the caller's input then changed the
    # redacted view it had already handed out.
    inner = {"port": 8080}
    red = cw.redact_secrets({"x": [("k", inner)]})
    assert red["x"][0][1] is not inner
    inner["port"] = 9090
    assert red["x"][0][1] == {"port": 8080}  # the display value did not move with it


def test_redact_reuses_an_aliased_node_instead_of_re_expanding_every_path_to_it() -> None:
    # #1393: a YAML alias makes the parsed structure a DAG, and an unmemoized walk re-expands
    # every distinct PATH to a node -- exponential in the header's byte length. Asserting the
    # output IDENTITY is the falsifiable form: without the memo the walk still finishes here
    # (the input is tiny) but returns two independent copies, so dropping the memo fails this
    # line rather than hanging the suite. The cost side is pinned at the parse seam, where a
    # header far too large to walk twice is what proves the memo is doing the work.
    shared = {"k": ["v"]}
    red = cw.redact_secrets({"a": shared, "b": shared})
    assert red["a"] is red["b"]
    assert red["a"] == {"k": ["v"]}


def test_redact_output_shares_an_aliased_node_so_a_nested_mutation_moves_its_sibling() -> None:
    # The FOOTGUN the memo introduces, made visible rather than left as docstring prose. The
    # result is a deep copy of the input but NOT a fully expanded tree: one aliased input node
    # becomes one shared output object. Every caller today replaces a whole key on the ROOT
    # (`config_write_settings._redact_misc`, `config_write_subagents._read_agent`), which is
    # safe because an acyclic root can never also be a nested node. Mutating a NESTED value in
    # place is not safe, and this is what that looks like.
    shared = {"port": 8080}
    red = cw.redact_secrets({"a": shared, "b": shared})

    red["a"]["port"] = 9090  # a caller "adjusting" one branch...

    assert red["b"]["port"] == 9090  # ...moved the other one too
    # Replacing the whole key, which is what the real callers do, does NOT reach the sibling.
    red["a"] = {"port": 7070}
    assert red["b"]["port"] == 9090


def test_redact_memo_keys_on_the_secret_hint_so_an_aliased_node_is_not_under_masked() -> None:
    # The hint is half the memo key, and dropping it would UNDER-MASK: the same aliased object
    # reached under a benign key and under a secret-shaped one must redact differently, and an
    # id()-only cache would serve whichever was walked first to both. Asserted in both orders,
    # because a cache bug is order-dependent by nature.
    shared = {"value": "sk-live-AAA"}
    for label, root in (
        ("benign first", {"name": shared, "auth": shared}),
        ("secret first", {"auth": shared, "name": shared}),
    ):
        red = cw.redact_secrets(root)
        assert red["name"] == {"value": "sk-live-AAA"}, label  # benign hint: not masked
        assert red["auth"] == {"value": cw.REDACTION_SENTINEL}, label  # secret hint: masked


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


# --- #1393: a billion-laughs alias pyramid -----------------------------------------------
#
# The same anchor referenced many times per level is legal, ACYCLIC YAML, so the #1368 cycle
# guard neither does nor should reject it -- but each level multiplies the number of distinct
# paths to a node, so every unmemoized walk is exponential in the header's BYTE length. Two
# consumers were: `_is_self_referential` (fixed with its `done` set when it was written) and
# `redact_secrets`, which held an `asyncio.to_thread` worker on a GET for as long as it ran.
#
# Memoizing `redact_secrets` is necessary and not sufficient: the redacted view is then a DAG,
# and `jsonable_encoder` + `json.dumps` on the route's response -- in the event loop, not a
# worker -- expand it right back. Measured on this host: the 8x8 header below redacts in
# 0.04ms and serializes to 101 MB in 0.5s; a 472-byte 10x9 one redacts in 0.05ms and does not
# finish serializing. The expansion is inherent to the document, so the seam refuses it by
# expanded SIZE, exactly as it refuses a cycle -- and, exactly as for a cycle, a file an
# earlier release already wrote still reads back with `frontmatter` degraded to `{}`.


def _alias_pyramid(*, levels: int, refs: int, leaf: str = "x") -> str:
    """Build a `&a`/`*a` header of `levels` levels, each aliasing the one below `refs` times."""
    lines = [f"a0: &a0 {leaf}"]
    for i in range(1, levels + 1):
        lines.append(f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * refs) + "]")
    return "\n".join(lines) + "\n"


def test_the_alias_pyramid_reproducer_is_a_positive_control() -> None:
    """PIN: a few hundred bytes really do alias (not copy) into millions of values."""
    header = _alias_pyramid(levels=8, refs=8)
    assert len(header.encode()) < 400  # three orders of magnitude under every byte cap
    value = yaml.safe_load(header)  # and `safe_load` itself never expands it
    assert value["a8"][0] is value["a8"][1]  # one shared object per level, not 8 copies
    assert cw._expands_beyond(value, 10_000_000)  # 21_913_116 characters from 346 bytes


def test_load_frontmatter_yaml_accepts_an_alias_pyramid_that_stays_under_the_cap() -> None:
    # Rejection is by expanded SIZE, never by alias count or depth: a pyramid whose expansion
    # fits is ordinary YAML and must still be accepted. Getting there must also not cost
    # O(distinct paths) -- measured before `_is_self_referential` grew its memo, an 8x8 header
    # `safe_load` parses in 0.7ms took 1.64s to check. The bound below is orders of magnitude
    # above the memoized cost, so it is not a timing flake.
    header = _alias_pyramid(levels=6, refs=6)  # 67_198 characters, under MAX_EXPANDED_CHARS
    started = time.perf_counter()
    parsed = cw.load_frontmatter_yaml(header, what="frontmatter")
    assert time.perf_counter() - started < 0.5
    assert parsed["a6"][0] is parsed["a6"][1]  # accepted WITH its aliases intact


def test_load_frontmatter_yaml_rejects_an_alias_pyramid_that_expands_past_the_cap() -> None:
    # Fail closed at the parse seam, the same shape as the cycle rejection: the write is
    # refused, so such a document never reaches disk, and the message names nothing from the
    # document (invariant 4 -- it reaches the browser as a skill's `frontmatter_error`).
    header = _alias_pyramid(levels=8, refs=8)
    started = time.perf_counter()
    with pytest.raises(cw.InvalidCandidateError, match="expands past the .* character cap"):
        cw.load_frontmatter_yaml(header, what="frontmatter")
    assert time.perf_counter() - started < 0.5  # the guard short-circuits AT the cap


def test_load_frontmatter_yaml_rejects_a_pyramid_whose_leaf_is_one_long_scalar() -> None:
    # A cap on the number of VALUES is the wrong metric and this is the counterexample: a
    # value's serialized length is not bounded, so a 30_000-character leaf aliased into a
    # 6x6 pyramid's 46_656 slots is the same 67_198 values that the test above accepts -- and
    # ~1.4 GB of JSON in the event loop. Both files are inside every byte cap, so this is a
    # WRITE the tier would otherwise take, not only a file that arrived with a clone.
    header = _alias_pyramid(levels=6, refs=6, leaf='"' + "A" * 30_000 + '"')
    assert len(header.encode()) < config_write_subagents.MAX_BYTES  # accepted by the byte cap
    with pytest.raises(cw.InvalidCandidateError, match="expands past the .* character cap"):
        cw.load_frontmatter_yaml(header, what="frontmatter")


def test_load_frontmatter_yaml_rejects_a_pyramid_whose_leaf_is_a_yaml_set() -> None:
    # `safe_load` builds a `set` for `!!set`, and `jsonable_encoder` emits one JSON entry per
    # member -- so a set counted as a single scalar is the same bypass as the long string.
    # `_is_self_referential` is right to leave sets out (a set holds only hashables, so it
    # cannot carry a cycle); a SIZE check is the opposite case and must walk them.
    members = "\n".join(f"  k{i:04d}:" for i in range(1_000))
    header = f"s: &s !!set\n{members}\n" + "\n".join(
        f"a{i}: &a{i} [" + ",".join([f"*{'s' if i == 1 else f'a{i - 1}'}"] * 6) + "]"
        for i in range(1, 7)
    )
    parsed = yaml.safe_load(header)
    assert isinstance(parsed["s"], set) and len(parsed["s"]) == 1_000  # positive control
    with pytest.raises(cw.InvalidCandidateError, match="expands past the .* character cap"):
        cw.load_frontmatter_yaml(header, what="frontmatter")


def test_the_size_cap_does_not_fire_on_a_huge_alias_free_hex_integer() -> None:
    # The size cap's whole claim is that it fires on alias AMPLIFICATION and never on an
    # alias-free document, and a hex integer is the densest alias-free expansion there is:
    # `safe_load` turns 65_500 hex digits into ~78_900 decimal ones. Counting an int's BITS
    # -- the first attempt -- over-stated that by 3.3x (262_000) and rejected this header,
    # which carries no alias at all. `safe_load` itself refuses the same value spelled in
    # decimal (CPython caps int(str) at 4300 digits), so hex is the only spelling that
    # reaches the seam. `_expands_beyond` must NOT fire on it.
    header = "n: 0x" + "f" * 65_500 + "\n"
    assert len(header.encode()) < config_write_subagents.MAX_BYTES
    value = yaml.safe_load(header)
    assert value["n"].bit_length() == 262_000
    assert cw._expands_beyond(value, cw.MAX_EXPANDED_CHARS) is False
    # #1415: `load_frontmatter_yaml` NOW rejects this int at the seam, because `str(int)`
    # past the digit limit 500s the response. That rejection is a SEPARATE guard
    # (`_first_json_unsafe`), not the size cap -- the size cap still accepts it above.
    with pytest.raises(cw.InvalidCandidateError, match="too large"):
        cw.load_frontmatter_yaml(header, what="frontmatter")


# --- #1415: scalars that parse but cannot cross the JSON response path -------------------


@pytest.mark.parametrize("scalar", [".nan", ".inf", "-.inf"])
def test_load_frontmatter_yaml_rejects_a_non_finite_float(scalar: str) -> None:
    # `.nan`/`.inf` parse to a float the response `json.dumps` (allow_nan=False, Starlette's
    # JSONResponse) refuses, 500ing a tier documented to fail as 422 (#1415).
    with pytest.raises(cw.InvalidCandidateError, match="non-finite float"):
        cw.load_frontmatter_yaml(f"a: {scalar}\n", what="frontmatter")


def test_load_frontmatter_yaml_rejects_a_non_utf8_binary() -> None:
    # `!!binary` of a byte that is not UTF-8 (0xff) parses to `bytes` that `jsonable_encoder`
    # cannot decode for the response (#1415).
    with pytest.raises(cw.InvalidCandidateError, match="not valid UTF-8"):
        cw.load_frontmatter_yaml('a: !!binary "/w=="\n', what="frontmatter")


def test_load_frontmatter_yaml_accepts_a_utf8_binary() -> None:
    # A `!!binary` whose bytes ARE UTF-8 decodes fine, so it is not rejected: the guard is
    # byte-content specific, not a blanket `!!binary` ban.
    value = cw.load_frontmatter_yaml('a: !!binary "aGk="\n', what="frontmatter")
    assert value["a"] == b"hi"


_HUGE_INT = "0x" + "f" * 5_000  # ~6_000 decimal digits, past CPython's 4300-digit str() limit


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(f"n: {_HUGE_INT}\n", id="value"),
        pytest.param(f"?\n  {_HUGE_INT}\n:\n  v\n", id="explicit-key"),
        pytest.param(f"a: !!omap\n- ? {_HUGE_INT}\n  : v\n", id="omap-key"),
    ],
)
def test_load_frontmatter_yaml_rejects_a_huge_int_in_any_position(header: str) -> None:
    # A hex int past `sys.get_int_max_str_digits()` 500s on `str(int)`: `json.dumps` in a
    # VALUE position, `redact_secrets`' `str(k)` in a KEY position. PyYAML's explicit `?` key
    # syntax bypasses the 1024-char simple-key cap, so the key variants reach it (#1415). The
    # guard walks keys as well as values, so all three are refused.
    with pytest.raises(cw.InvalidCandidateError, match="too large"):
        cw.load_frontmatter_yaml(header, what="frontmatter")


def test_the_json_unsafe_rejection_message_carries_no_document_payload() -> None:
    # invariant 4: the rejection reaches the browser as a `frontmatter_error`, so it names the
    # scalar's CLASS only, never the value -- a huge int pasted as a credential-shaped literal
    # must not echo back.
    with pytest.raises(cw.InvalidCandidateError) as exc_info:
        cw.load_frontmatter_yaml(f"n: {_HUGE_INT}\n", what="frontmatter")
    msg = str(exc_info.value)
    assert "0x" not in msg and "ffff" not in msg


def test_load_frontmatter_yaml_accepts_ordinary_finite_scalars() -> None:
    # Positive control: the guard does not over-reject. A finite float, a small int, a bool,
    # and a null all parse and serialize, so they pass untouched.
    value = cw.load_frontmatter_yaml("f: 1.5\ni: 42\nb: true\nz: null\n", what="frontmatter")
    assert value == {"f": 1.5, "i": 42, "b": True, "z": None}


def test_load_frontmatter_yaml_rejects_a_lone_surrogate_string() -> None:
    # A `\uD800` escape resolves to a lone-surrogate `str` that JSON accepts but the response
    # body's `.encode("utf-8")` (Starlette renders with ensure_ascii=False) refuses -- the
    # fourth shape of the #1415 class, in a value here and a key below.
    with pytest.raises(cw.InvalidCandidateError, match="lone surrogate"):
        cw.load_frontmatter_yaml('a: "\\uD800"\n', what="frontmatter")
    with pytest.raises(cw.InvalidCandidateError, match="lone surrogate"):
        cw.load_frontmatter_yaml('? "\\uD800"\n:\n  v\n', what="frontmatter")


def test_load_frontmatter_yaml_accepts_a_non_ascii_string() -> None:
    # Positive control: an ordinary non-ASCII string encodes fine, so the surrogate guard does
    # not become a blanket non-ASCII ban.
    value = cw.load_frontmatter_yaml("a: café ☕\n", what="frontmatter")
    assert value == {"a": "café ☕"}


def test_a_disabled_int_str_limit_accepts_every_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    # CPython lets the int-to-str limit be turned off (PYTHONINTMAXSTRDIGITS=0,
    # sys.set_int_max_str_digits(0)); `get_int_max_str_digits()` then returns 0, and `str(int)`
    # never raises, so NO int 500s and the guard must accept every int. A `> 0` test would
    # instead reject `timeout: 30`. Modelled by patching the query to 0 -- the guard reads it
    # there and must special-case it. Precondition pinned so the arm cannot go green wrongly.
    monkeypatch.setattr(cw.sys, "get_int_max_str_digits", lambda: 0)
    assert cw.sys.get_int_max_str_digits() == 0
    value = cw.load_frontmatter_yaml(f"small: 30\nbig: {_HUGE_INT}\n", what="frontmatter")
    assert value["small"] == 30
    assert value["big"].bit_length() == 20_000


def test_byte_caps_stay_under_the_expansion_cap() -> None:
    # The two constants are COUPLED and the coupling is invisible from either side, so this is
    # the gate rather than a comment. `MAX_EXPANDED_CHARS` claims it can only fire on alias
    # AMPLIFICATION, never on an alias-free document -- but the densest alias-free expansion
    # measured is a `!!timestamp` at 2.909x (`2001-12-14,` is 11 source bytes and 32 counted
    # characters). At the 64 KiB file cap that is 190_625, inside the cap by 1.31x, NOT by the
    # order of magnitude the byte figures suggest. 3x here is the measured ceiling rounded up.
    # Raising a file cap to 128 KiB reads as safe and is not: it would put an alias-free header
    # at 381k and have the guard reject it. Raise `MAX_EXPANDED_CHARS` in the same PR.
    for label, byte_cap in (
        ("subagent", config_write_subagents.MAX_BYTES),
        ("skill SKILL.md", config_write_skills.MAX_SKILL_MD_BYTES),
    ):
        assert byte_cap * 3 < cw.MAX_EXPANDED_CHARS, label
    # The `byte_cap * 3` line above is necessary and NOT sufficient, so each scalar kind that
    # could beat 2.909x is also filled to the byte cap and measured against the real cap. A
    # float is the one that made this necessary: counting it as a flat `_TIMESTAMP_CHARS`
    # rather than by its own repr puts `1.` at 10.67x -- 698_915 characters for a 64 KiB
    # alias-free header -- while the 3x line above still passes. Densest first.
    for label, item, count in (
        ("timestamps", "2001-12-14", 5_957),  # 2.909x, the ceiling
        ("short floats", "1.", 21_841),  # 1.0x by repr; 10.67x under a flat 32
        ("hex ints", "0xffff", 9_361),  # 1.204x
        ("strings", "aaaaaaaaaa", 5_957),  # 1.0x
    ):
        filled = "d: [" + ",".join([item] * count) + "]\n"
        assert len(filled.encode()) < config_write_subagents.MAX_BYTES, label
        assert not cw._expands_beyond(yaml.safe_load(filled), cw.MAX_EXPANDED_CHARS), label


def test_expands_beyond_counts_a_diamond_once_and_sizes_scalars_by_length() -> None:
    # The memo is what keeps the walk linear, and it must not turn a diamond -- the same
    # object under two keys -- into a double count that rejects an ordinary document. Every
    # container `safe_load` builds is walked (`!!omap` makes tuples, `!!set` makes sets), and
    # a dict's KEYS count too: the serializer emits them at every alias slot as well.
    shared = ["x", "y", "z"]
    assert not cw._expands_beyond({"a": shared, "b": shared}, 11)  # 1 + 2 keys + 2x(1+3) = 11
    assert cw._expands_beyond({"a": shared, "b": shared}, 10)
    assert not cw._expands_beyond("scalar", 0)  # a non-container is not walked at all
    assert cw._expands_beyond({"t": ("x", "y", "z")}, 5)  # 1 + 1 + (1+3) = 6, via a tuple
    assert cw._expands_beyond({"s": {"x", "y", "z"}}, 5)  # ...and the same via a set
    assert cw._expands_beyond({"k": "A" * 50}, 51)  # a string costs its LENGTH, not one
    assert cw._expands_beyond({"k": 10**60}, 51)  # ...and an int its 61 decimal digits
    assert not cw._expands_beyond({"k": 10**60}, 63)  # 1 + 1 + 61 = 63, never OVER-counted
    assert not cw._expands_beyond({"n": None, "b": True}, 5)  # 1 + 2 keys + 1 + 1 = 5
    # A `!!timestamp` is the longest bounded repr and is counted, not defaulted: at one
    # character each, a 500-byte pyramid of timestamp leaves was an accepted 4 MB response.
    assert cw._expands_beyond({"t": datetime.date(2001, 12, 14)}, 33)  # 1 + 1 + 32 = 34
    # A float is counted by its OWN repr, not by the timestamp constant. Its source spelling
    # can be far shorter than a timestamp's, so a flat 32 would over-count an alias-free
    # document -- see the ceiling assertion in test_byte_caps_stay_under_the_expansion_cap.
    assert cw._expands_beyond({"f": 1.7976931348623157e308}, 24)  # 1 + 1 + 23 = 25
    assert not cw._expands_beyond({"f": 1.0}, 5)  # 1 + 1 + len("1.0") = 5, not 34


# --- #1417: a merge-key (`<<`) pyramid detonates INSIDE safe_load ------------------------
#
# Unlike the plain-alias pyramid above -- whose blow-up is a post-parse consumer walking
# O(distinct paths) -- a `<<` merge flattens at CONSTRUCTION: PyYAML's `flatten_mapping`
# splices each merged mapping's entries into a fresh list before `construct_mapping` dedupes
# them, so a level referencing the one below N times over L levels builds an N**L-entry list
# INSIDE `safe_load`. The dict it finally returns is tiny (the merged keys dedupe), so no
# post-parse guard -- `_is_self_referential`, `_expands_beyond`, `_first_json_unsafe` -- can
# see it: they only run on the returned object, and reaching it took 7s for 456 bytes. The
# fix refuses it on the composed NODE graph, which `yaml.compose` builds WITHOUT flattening.


def _merge_pyramid(*, levels: int, refs: int) -> str:
    """Build a `<<` merge header: `levels` levels, each merging the one below `refs` times."""
    lines = ["a0: &a0 {k: v}"]
    for i in range(1, levels + 1):
        merged = "[" + ",".join([f"*a{i - 1}"] * refs) + "]"
        lines.append(f"a{i}: &a{i} {{<<: {merged}, k: v}}")
    return "\n".join(lines) + "\n"


def test_the_merge_pyramid_reproducer_is_a_positive_control() -> None:
    """PIN: `safe_load` flattens a `<<` pyramid at construction while `compose` does not.

    Both halves are why the guard is pre-parse. If a PyYAML release moved merge flattening off
    the construct path, or `compose` began flattening merges, the guard would be in the wrong
    place and this test should fail rather than let the fix pass vacuously.
    """
    header = _merge_pyramid(levels=6, refs=8)  # ~300k flattened pairs from < 400 bytes
    assert len(header.encode()) < 400  # orders of magnitude under every byte cap
    # `compose` builds the node DAG without following a single merge -- milliseconds.
    started = time.perf_counter()
    root = yaml.compose(header, Loader=yaml.SafeLoader)
    assert time.perf_counter() - started < 0.2
    assert cw._merge_expands_beyond(root, cw.MAX_MERGE_EXPANDED_PAIRS)
    # `safe_load` DOES flatten, and the dict it returns is tiny (the merged `k` dedupes to one),
    # so the cost is entirely in the parse where no post-parse guard can reach it.
    value = yaml.safe_load(header)
    assert value["a6"] == {"k": "v"}
    assert not cw._expands_beyond(value, cw.MAX_EXPANDED_CHARS)  # post-parse guard is blind here


def test_load_frontmatter_yaml_rejects_a_merge_pyramid_before_it_detonates() -> None:
    # The whole point: refuse it FAST, before `safe_load` flattens. Without the pre-parse guard
    # this header holds a worker thread for ~7s (#1417); with it, the compose walk short-circuits
    # at the pair cap in milliseconds. The 1s bound is a positive control -- reverting the guard
    # makes `safe_load` run to completion (no raise, ~7s) and fails BOTH assertions.
    header = _merge_pyramid(levels=8, refs=8)  # ~19M flattened pairs -> ~7s in `safe_load`
    assert len(header.encode()) < 550  # ~503 bytes, inside every byte cap
    started = time.perf_counter()
    with pytest.raises(cw.InvalidCandidateError, match="pair cap through its YAML merge keys"):
        cw.load_frontmatter_yaml(header, what="frontmatter", line_offset=1)
    assert time.perf_counter() - started < 1.0  # the guarded parse never reaches the flatten


def test_the_merge_pyramid_rejection_carries_no_document_payload() -> None:
    # invariant 4: the rejection reaches the browser as a skill's `frontmatter_error`. The
    # anchor names could be credential-shaped, so the static message must echo none of them.
    # Low-entropy padding on purpose (a real secret-shaped literal fails the gitleaks gate).
    token = "FAKEFAKEFAKEFAKEFAKEfake42"
    lines = [f"a0: &{token}0 {{k: v}}"]
    for i in range(1, 9):
        merged = "[" + ",".join([f"*{token}{i - 1}"] * 8) + "]"
        lines.append(f"a{i}: &{token}{i} {{<<: {merged}, k: v}}")
    with pytest.raises(cw.InvalidCandidateError) as exc_info:
        cw.load_frontmatter_yaml("\n".join(lines) + "\n", what="frontmatter")
    assert token not in str(exc_info.value)


def test_load_frontmatter_yaml_accepts_an_ordinary_merge_key() -> None:
    # Positive control: a `<<` merge is legal, useful YAML and must still parse. The guard
    # counts flattened pairs, so a single small merge is nowhere near the cap.
    value = cw.load_frontmatter_yaml(
        "defaults: &d {model: opus, tools: [a, b]}\nagent: {<<: *d, name: x}\n",
        what="frontmatter",
    )
    assert value["agent"] == {"model": "opus", "tools": ["a", "b"], "name": "x"}


def test_load_frontmatter_yaml_accepts_many_independent_merges() -> None:
    # The guard bounds the CUMULATIVE flattened pairs, not merge USE. Hundreds of siblings each
    # merging one shared base flatten to only ~4 pairs apiece, so the sum (~2k here) stays far
    # below the cap and this must pass where the pyramid does not. A flat `total alias
    # references` cap (the other option in #1417) counts references, not sizes, so it would
    # wrongly reject these 400 tiny merges -- which is why the bound is by flattened SIZE.
    base = "base: &b {a: 1, b: 2, c: 3}\n"
    body = "\n".join(f"e{i}: {{<<: *b, id: {i}}}" for i in range(400))
    value = cw.load_frontmatter_yaml(base + body + "\n", what="frontmatter")
    assert value["e399"] == {"a": 1, "b": 2, "c": 3, "id": 399}


def test_load_frontmatter_yaml_accepts_a_comment_that_merely_mentions_merge() -> None:
    # The `<<` fast-path is a substring test, so a comment mentioning `<<` triggers the compose
    # but composes to no merge -- or, if the header is only that comment, to nothing at all
    # (`root is None`). Both must be accepted, not error on the extra compose.
    assert cw.load_frontmatter_yaml("# uses << merge\nname: r\n", what="frontmatter") == {
        "name": "r"
    }
    assert cw.load_frontmatter_yaml("# just << here\n", what="frontmatter") is None


def test_merge_expands_beyond_sums_flattened_pairs_across_mappings() -> None:
    # Unit-level: the cap is on the CUMULATIVE flattened pairs -- the total list
    # `flatten_mapping` builds across every mapping -- because `safe_load`'s cost is that sum,
    # not the largest single mapping. A shared target is sized once for itself and again inside
    # each mapping that merges it, and a shared anchor is discovered once (memoized) so the walk
    # is linear in the DAG, not exponential in its paths.
    def compose(src: str) -> yaml.Node:
        return yaml.compose(src, Loader=yaml.SafeLoader)

    # a = {k: v} = 1; b = <<:[*a,*a] + own `k` = 3; root has 2 literal keys = 2. Sum = 6.
    root = compose("a: &a {k: v}\nb: {<<: [*a, *a], k: v}\n")
    assert not cw._merge_expands_beyond(root, 6)
    assert cw._merge_expands_beyond(root, 5)  # short-circuits when the running total crosses
    # Many tiny siblings sum past the cap even though each mapping is small -- the WIDE shape a
    # per-mapping cap misses. d = {a,b} = 2; each of 10 `e{i}: {<<: *d}` = 2; root has 11 literal
    # keys. Sum = 2 + 10*2 + 11 = 33.
    wide = compose("d: &d {a: 1, b: 2}\n" + "\n".join(f"e{i}: {{<<: *d}}" for i in range(10)))
    assert cw._merge_expands_beyond(wide, 32)
    assert not cw._merge_expands_beyond(wide, 33)
    # A merge-free mapping contributes only its literal key count -- never tripped by a `<<`.
    assert not cw._merge_expands_beyond(compose("a: 1\nb: 2\nc: 3\n"), 3)
    assert cw._merge_expands_beyond(compose("a: 1\nb: 2\nc: 3\nd: 4\n"), 3)
    # A `<<` pointing at a single mapping (not a sequence) is followed too: a=3, b=3, root=2 = 8.
    assert cw._merge_expands_beyond(compose("a: &a {p: 1, q: 2, r: 3}\nb: {<<: *a}\n"), 7)


def test_load_frontmatter_yaml_rejects_wide_sibling_merges_that_each_stay_small() -> None:
    # The bound is cumulative, not per mapping. Many sibling mappings each merging one shared,
    # under-cap target keep every mapping small, but `safe_load` still flattens the SUM: a ~few-KB
    # header of them held a worker for seconds on every list, and the read path caps no bytes.
    # Positive control: without the cumulative bound the per-mapping sizes here are all tiny and
    # the header passes -- reverting to a per-mapping cap makes this NOT raise.
    shared = _merge_pyramid(levels=4, refs=7)  # `a4` flattens to 2_801 pairs, tiny per mapping
    siblings = "\n".join(f"s{i}: {{<<: *a4, id: {i}}}" for i in range(50))  # ~50 * 2_802 pairs
    header = shared + siblings + "\n"
    started = time.perf_counter()
    with pytest.raises(cw.InvalidCandidateError, match="pair cap through its YAML merge keys"):
        cw.load_frontmatter_yaml(header, what="frontmatter")
    assert time.perf_counter() - started < 1.0  # refused on the node graph, before the flatten


def test_load_frontmatter_yaml_refuses_a_self_referential_merge_quickly() -> None:
    # A `<<` that merges its own anchor cannot be sized on the node graph (its target is never
    # memoized), so the pair-cap guard counts it as zero. PyYAML would load it without hanging
    # (`flatten_mapping` deletes the merge key before recursing) and silently drop the merge,
    # but the CLI's YAML 1.2 loader refuses it ("Merge key cannot reference an enclosing
    # node"), so since TASK-449.6 the seam refuses it too, on the node graph, before the load.
    started = time.perf_counter()
    with pytest.raises(cw.InvalidCandidateError, match="alias to a node that encloses it"):
        cw.load_frontmatter_yaml("x: &x {<<: *x, k: v}\n", what="frontmatter")
    assert time.perf_counter() - started < 1.0


# --- #1369: a YAMLError's prose must not echo the offending token -------------------------
#
# The rejection message reaches the browser (`list_skills` surfaces it as a skill's
# `frontmatter_error`), so it is bound by invariant 4. Interpolating `str(exc)` did not
# satisfy it: PyYAML writes the offending token into the message PROSE for these shapes, where
# it sits mid-line with no `key:` anchor and `redact_secret_lines` -- which masks a `key: value`
# whose KEY looks secret-shaped -- structurally cannot reach it.
# Low-entropy padding on purpose, matching the sibling test's convention: a genuinely
# secret-shaped literal in any commit fails the gitleaks gate. What matters here is only that
# the token is distinctive and that PyYAML copies it into the prose verbatim.
_LEAK_TOKEN = "FAKEFAKEFAKEFAKEFAKEfake42"
_TOKEN_IN_PROSE = [
    # Nested: the CLI retry re-quotes a top-level line with `*` or `&` (TASK-449.6).
    pytest.param(f"name: x\nm:\n  extra: *{_LEAK_TOKEN}\n", id="undefined-alias"),
    pytest.param(f"m:\n  a: &{_LEAK_TOKEN} 1\n  b: &{_LEAK_TOKEN} 2\n", id="duplicate-anchor"),
    pytest.param(f"a: !{_LEAK_TOKEN} 1\n", id="unknown-tag"),
]


@pytest.mark.parametrize("header", _TOKEN_IN_PROSE)
def test_the_yaml_prose_leak_reproducer_is_a_positive_control(header: str) -> None:
    """PIN: PyYAML really does put the token in the prose, and the redactor really misses it.

    Both halves matter. If a PyYAML release stopped embedding the token, the assertions below
    would pass while asserting nothing; if `redact_secret_lines` grew mid-line masking, the
    fix would be belt-and-braces rather than the thing standing between a pasted credential
    and the dashboard. Either way the next reader should be sent back here.
    """
    with pytest.raises(yaml.YAMLError) as raised:
        yaml.safe_load(header)
    # Against the PROSE attributes, not `str(exc)`. `MarkedYAMLError.__str__` also renders the
    # mark's snippet -- the offending SOURCE LINE -- which carries the token too, so asserting
    # on the full string would still pass if PyYAML dropped the token from the prose tomorrow.
    # That is the vacuous green this control exists to prevent. Duplicate-anchor puts the token
    # in `context`, the other two in `problem`; either can be None.
    prose = f"{raised.value.context or ''} {raised.value.problem or ''}"
    assert _LEAK_TOKEN in prose
    # This half is right to stay on the full string: it is about what the redactor can reach
    # across the whole message, snippet included.
    assert _LEAK_TOKEN in cw.redact_secret_lines(str(raised.value))


@pytest.mark.parametrize("header", _TOKEN_IN_PROSE)
def test_load_frontmatter_yaml_never_echoes_the_offending_token(header: str) -> None:
    # The message is built from POSITIONS only -- the class name plus problem_mark/context_mark
    # integers -- so it is fail-closed by construction rather than by a predicate that has to
    # be right about every message PyYAML will ever emit.
    with pytest.raises(cw.InvalidCandidateError) as raised:
        cw.load_frontmatter_yaml(header, what="frontmatter")
    message = str(raised.value)
    assert _LEAK_TOKEN not in message
    assert "line " in message and "column " in message  # ...and it still says WHERE


def test_load_frontmatter_yaml_reports_a_useful_position_and_category() -> None:
    # What replaces the prose has to earn its place: the operator gets PyYAML's own error
    # category and a 1-based line/column that matches what their editor shows, next to the
    # `content` they are already looking at. Marks are 0-based internally, hence the +1.
    with pytest.raises(cw.InvalidCandidateError) as raised:
        cw.load_frontmatter_yaml("name: x\n\tbad: 1\n", what="frontmatter")
    assert "ScannerError at line 2, column 1" in str(raised.value)


def test_load_frontmatter_yaml_names_the_reader_error_coordinate_space() -> None:
    # `ReaderError` is the real no-mark shape and it is not hypothetical: a control character
    # pasted into frontmatter (terminal output, an escape sequence) raises it. It carries a
    # plain character offset instead of a mark, which is positions-only by definition -- so
    # the message must still say WHERE rather than degrading to a bare category. Driven by a
    # genuine production shape, not a monkeypatched exception, so it also pins that PyYAML's
    # no-mark class stays handled.
    with pytest.raises(cw.InvalidCandidateError) as raised:
        cw.load_frontmatter_yaml("a: \x08SECRETVALUE\n", what="frontmatter")
    message = str(raised.value)
    # "of the frontmatter block" is load-bearing: `.position` indexes the header slice, while
    # the line/column arm is shifted to name the FILE. Two coordinate spaces in one function,
    # so the one that is not file-relative has to say so.
    assert "ReaderError at character " in message
    assert "of the frontmatter block" in message
    assert "SECRETVALUE" not in message


def test_yaml_error_where_degrades_to_the_category_alone_with_no_position() -> None:
    # The last arm of `_yaml_error_where`, exercised DIRECTLY because it is unreachable
    # through the FRONTMATTER seam: every error `safe_load` raises sets `problem_mark`, and
    # the one that does not (`ReaderError`) carries `.position`. So a YAMLError with neither
    # cannot be produced by `load_frontmatter_yaml` -- which is exactly why the arm exists. It
    # is the defence that keeps a future PyYAML shape degrading to the category instead of
    # raising an AttributeError out of the handler whose whole job is preventing 500s.
    # (#1395 gave the arm a live producer on the OTHER seam: the
    # `config.FixedDetailYamlError` subclasses are raised by hand and carry neither. Note
    # `run_doctor` catches those before this helper sees them, so it prints their own
    # literal rather than this fallback.)
    kw = {"block_name": "frontmatter block"}
    assert cw._yaml_error_where(yaml.YAMLError("no marks here"), **kw) == " (YAMLError)"
    # ...and, like every other arm, it carries nothing derived from the document.
    assert "no marks here" not in cw._yaml_error_where(yaml.YAMLError("no marks here"), **kw)


def test_load_frontmatter_yaml_reports_the_second_position_when_it_differs() -> None:
    # An unterminated quote puts `problem_mark` at end-of-stream, which on its own points the
    # operator at the wrong place; `context_mark` holds the opening quote. Both are integer
    # pairs, so both are reported when they differ.
    #
    # The label is shape-neutral on purpose. `context_mark` means something different per
    # shape -- for a duplicate anchor it is the FIRST occurrence, not where anything "started"
    # -- and this seam deliberately does not know which error it is holding.
    with pytest.raises(cw.InvalidCandidateError) as raised:
        cw.load_frontmatter_yaml('name: x\nbad: "unterminated\n', what="frontmatter")
    assert "see also line " in str(raised.value)

    with pytest.raises(cw.InvalidCandidateError) as dup:
        cw.load_frontmatter_yaml("x:\n  a: &m 1\n  b: &m 2\n", what="frontmatter")
    assert "see also line " in str(dup.value)


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


# The three display readers that share read_config_bytes (issue 1528).
_CONFIG_BYTES_READERS = {
    "nested-subtree": lambda p: cw.read_nested_subtree(p, "projects", "/repo/a", "mcpServers"),
    "enabled-plugins": config_write_plugins.read_user_enabled_plugins,
    "declared-marketplaces": config_write_plugins.read_user_marketplaces,
}


@pytest.mark.parametrize("reader", _CONFIG_BYTES_READERS.values(), ids=_CONFIG_BYTES_READERS)
@pytest.mark.parametrize(
    "exc",
    [
        PermissionError(errno.EACCES, "denied"),
        IsADirectoryError(errno.EISDIR, "is a directory"),
        NotADirectoryError(errno.ENOTDIR, "not a directory"),
    ],
    ids=["EACCES", "EISDIR", "ENOTDIR"],
)
def test_config_bytes_readers_map_path_errors(monkeypatch, tmp_path: Path, reader, exc) -> None:
    # A path that is not a readable file is a typed ConfigWriteError (-> 400) naming only
    # the basename. Patched, so ENOTDIR is covered on every OS, not just where it occurs.
    def _raise(self: Path) -> bytes:
        raise exc

    monkeypatch.setattr(Path, "read_bytes", _raise)
    with pytest.raises(cw.ConfigWriteError) as info:
        reader(tmp_path / "settings.json")
    assert str(info.value) == "cannot read settings.json: it is unreadable"
    assert str(tmp_path) not in str(info.value)


@pytest.mark.parametrize("reader", _CONFIG_BYTES_READERS.values(), ids=_CONFIG_BYTES_READERS)
def test_config_bytes_readers_propagate_host_os_errors(
    monkeypatch, tmp_path: Path, reader
) -> None:
    # A host fault (EIO here; EMFILE/ENFILE likewise) says nothing about the path, so it
    # must NOT be dressed up as a client-side ConfigWriteError: it propagates unchanged.
    def _raise(self: Path) -> bytes:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(Path, "read_bytes", _raise)
    with pytest.raises(OSError) as info:
        reader(tmp_path / "settings.json")
    assert not isinstance(info.value, cw.ConfigWriteError)
    assert info.value.errno == errno.EIO


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


def test_ensure_gitignored_noop_when_root_anchored_ancestor_rule_present(tmp_path: Path) -> None:
    # #1484: /.claude covers .claude/settings.local.json and its .bak sibling
    (tmp_path / ".gitignore").write_text("/.claude\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "/.claude\n"


def test_ensure_gitignored_noop_when_trailing_slash_ancestor_rule_present(tmp_path: Path) -> None:
    # #1484: .claude/ covers all files under .claude/
    (tmp_path / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".claude/\n"


def test_ensure_gitignored_noop_when_bare_ancestor_rule_present(tmp_path: Path) -> None:
    # #1484: .claude covers all files under .claude
    (tmp_path / ".gitignore").write_text(".claude\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ".claude\n"


def test_ensure_gitignored_noop_when_nested_ancestor_directory_rule_present(
    tmp_path: Path,
) -> None:
    # #1484: a/b/ covers nested paths like a/b/c/local.json
    (tmp_path / ".gitignore").write_text("a/b/\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, "a/b/c/local.json")
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "a/b/\n"


def test_ensure_gitignored_appends_uncovered_path_when_unrelated_rule_present(
    tmp_path: Path,
) -> None:
    # A rule that merely shares a prefix like .claude-other does not cover .claude
    (tmp_path / ".gitignore").write_text(".claude-other/\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    expected = ".claude-other/\n.claude/settings.local.json\n"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == expected


def test_ensure_gitignored_backup_sibling_covered_by_wildcard(tmp_path: Path) -> None:
    # *.bak covers the sibling, so only the file itself is appended
    (tmp_path / ".gitignore").write_text("*.bak\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    expected = "*.bak\n.claude/settings.local.json\n"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == expected


def test_ensure_gitignored_appends_when_negation_line_overrides_wildcard(tmp_path: Path) -> None:
    # Negation !settings.local.json re-includes file excluded by *.json, so must append
    (tmp_path / ".gitignore").write_text("*.json\n!settings.local.json\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, "settings.local.json")
    expected = "*.json\n!settings.local.json\nsettings.local.json\n"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == expected


def test_ensure_gitignored_appends_when_slash_negation_overrides_wildcard(tmp_path: Path) -> None:
    # Negation with path / glob pattern (e.g. !.claude/*.json) re-includes file
    (tmp_path / ".gitignore").write_text("*.json\n!.claude/*.json\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    expected = "*.json\n!.claude/*.json\n.claude/settings.local.json\n"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == expected


def test_ensure_gitignored_appends_when_doublestar_negation_matches_zero_directories(
    tmp_path: Path,
) -> None:
    # A negation !a/**/b matches when **/ stands for zero directories (a/b)
    (tmp_path / ".gitignore").write_text(
        "*.json\n!.claude/**/settings.local.json\n", encoding="utf-8"
    )
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    expected = "*.json\n!.claude/**/settings.local.json\n.claude/settings.local.json\n"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == expected


def test_ensure_gitignored_case_sensitive_matching(tmp_path: Path) -> None:
    # Gitignore matching is case-sensitive across platforms
    (tmp_path / ".gitignore").write_text("*.BAK\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json", ignore_backup_sibling=True)
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json.bak\n" in content


def test_ensure_gitignored_appends_when_root_anchored_glob_does_not_cover_nested(
    tmp_path: Path,
) -> None:
    # #1484 review: `/*.json` is root-anchored in git (root-level *.json only, not nested), so it
    # does NOT cover `.claude/settings.local.json`. The helper must not strip the anchor and skip
    # the append, or the secret-bearing file stays git-tracked.
    (tmp_path / ".gitignore").write_text("/*.json\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    expected = "/*.json\n.claude/settings.local.json\n"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == expected


def test_ensure_gitignored_over_appends_a_positive_slash_wildcard(tmp_path: Path) -> None:
    # A POSITIVE slash-wildcard is not trusted (fnmatch's `*` crosses `/` and drops git's anchor),
    # so the helper appends rather than risk skipping. `.claude/*.json` may or may not cover the
    # path in git; over-appending is the safe direction (#1484 review).
    (tmp_path / ".gitignore").write_text(".claude/*.json\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    expected = ".claude/*.json\n.claude/settings.local.json\n"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == expected


def test_ensure_gitignored_appends_when_doublestar_negation_matches_root_file(
    tmp_path: Path,
) -> None:
    # `!**/foo` re-includes a ROOT-level `foo` (git's `**/` matches zero or more leading
    # directories). A root target does not match the whole-path form, so this exercises the
    # `**/` sub-pattern branch, which is trusted only for a negation.
    (tmp_path / ".gitignore").write_text("*.json\n!**/settings.local.json\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, "settings.local.json")
    expected = "*.json\n!**/settings.local.json\nsettings.local.json\n"
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == expected


def test_ensure_gitignored_ignores_comment_and_blank_lines(tmp_path: Path) -> None:
    # Comment (`#`) and blank lines are skipped; the real ancestor rule still covers the path.
    (tmp_path / ".gitignore").write_text("# a comment\n\n/.claude\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, ".claude/settings.local.json")
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "# a comment\n\n/.claude\n"


def test_ensure_gitignored_noop_when_negation_does_not_match(tmp_path: Path) -> None:
    # A negation for an UNRELATED path does not clear the wildcard's coverage, so the file stays
    # ignored and nothing is appended.
    (tmp_path / ".gitignore").write_text("*.json\n!other.json\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, "settings.local.json")
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "*.json\n!other.json\n"


def test_ensure_gitignored_noop_when_doublestar_negation_does_not_match(tmp_path: Path) -> None:
    # A `!**/...` negation whose sub-pattern does not match leaves the wildcard's coverage
    # intact (the `**/` no-match fall-through), so the file stays ignored and nothing is added.
    (tmp_path / ".gitignore").write_text("*.json\n!**/unrelated.txt\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, "settings.local.json")
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "*.json\n!**/unrelated.txt\n"


def test_ensure_gitignored_noop_when_slash_negation_does_not_match(tmp_path: Path) -> None:
    # A slash-wildcard negation that is not `**/` and does not match falls through (the
    # `startswith("**/")` false edge), leaving the file ignored and nothing appended.
    (tmp_path / ".gitignore").write_text("*.json\n!.claude/other.json\n", encoding="utf-8")
    cw.ensure_gitignored(tmp_path, "settings.local.json")
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "*.json\n!.claude/other.json\n"


def test_is_gitignored_helper_returns_false_for_empty_target() -> None:
    # A path that normalizes to empty (e.g. "" or "/") is never "already covered".
    assert cw._is_gitignored_by_existing_rules("", "*.json") is False
    assert cw._is_gitignored_by_existing_rules("/", "*.json") is False


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
    run cost 8.6s at 64 KB.

    The check is a growth ratio, not a wall-clock bound: each shape runs at size ``n`` and
    ``4n``, and a linear pass costs about 4x where a quadratic one costs about 16x. A fixed
    bound failed on a slow CI runner under coverage (2.24s for a shape that takes 0.11s
    locally), while the ratio does not depend on the speed of the machine. The ceiling of
    30s per shape still catches a catastrophic regression at the full size.
    """
    import time

    shapes = {
        "interpolation flood": (500_000, lambda n: "${" * n),
        "alphanumeric run": (256_000, lambda n: "a" * n),
        "internal whitespace run": (64_000, lambda n: "token: a" + " " * n + "b"),
        "minified blob": (500_000, lambda n: "a." * n),
        # TASK-497: list markers, flow-mapping pairs, and a block value under a secret key.
        "list marker run": (200_000, lambda n: "- " * n + "token: x"),
        "flow pair flood": (50_000, lambda n: "{" + ", token: a" * n),
        "unclosed flow quotes": (50_000, lambda n: "{" + ', token: "a' * n),
        "flow key run": (256_000, lambda n: "{," + "a" * n),
        "block opener spaces": (128_000, lambda n: "token: |" + " " * n + "x"),
        "block value lines": (200_000, lambda n: "token: |\n" + "  x\n" * n),
        # TASK-536: a secret value that goes on past its line.
        "flow value run-on lines": (200_000, lambda n: "env: {token:\n" + "  x\n" * n),
        "quoted value run-on lines": (200_000, lambda n: 'token: "x\n' + "  x\n" * n),
        "run-on openers": (50_000, lambda n: "env: {token:\n" * n),
        "node property run": (128_000, lambda n: "token: " + "& " * n + '"x'),
        # A secret flow pair after a closed or plain secret value on the same line.
        "closed quote then pairs": (40_000, lambda n: 'token: "x"' + ", password: 'y'" * n),
        "plain value then pairs": (40_000, lambda n: "token: x" + ", password: y" * n),
    }

    def timed(payload: str) -> float:
        start = time.perf_counter()
        cw.redact_secret_lines(payload)
        return time.perf_counter() - start

    def ratio_of(small_payload: str, large_payload: str) -> tuple[float, float]:
        # Small and large runs alternate, so a load change on a shared CI runner hits both.
        small = timed(small_payload)
        large = timed(large_payload)
        assert large < 30.0, f"{label} took {large:.2f}s — quadratic behaviour is back"
        # The 2 ms floor keeps a near-zero denominator from inflating the ratio.
        return large / max(small, 0.002), large

    for label, (full, make) in shapes.items():
        small_payload, large_payload = make(full // 4), make(full)
        # A real quadratic fails every attempt; a load spike does not. One interleaved pair
        # decides a passing shape, so a green run costs about what the old single run did.
        # A full-size run under 50 ms passes whatever its ratio: three shapes run their
        # small size under the 2 ms floor ("internal whitespace run", "block opener spaces",
        # "flow key run"), so on a loaded runner their ratio only measures the large run.
        # Every quadratic this test records cost seconds at these sizes.
        ratios = []
        for _ in range(3):
            ratio, large = ratio_of(small_payload, large_payload)
            ratios.append(ratio)
            if ratio < 9.0 or large < 0.05:
                break
        else:
            pytest.fail(f"{label}: 4x the input cost {min(ratios):.1f}x on 3 tries — not linear")


# --- TASK-449.6: the YAML 1.1 (PyYAML) vs YAML 1.2 (CLI) differences, at the shared seam ----
#
# Both `parse_frontmatter` implementations go through `load_frontmatter_yaml`, so each case
# runs on the subagent and the skills surface. The CLI's loader is `Rd` in 2.1.318: it drops
# a BOM, cuts the header at the first `---` (`/^---\s*\n([\s\S]*?)---\s*\n?/`), and parses it
# with Bun.YAML (YAML 1.2).


def _both_parsers():
    return [
        pytest.param(config_write_subagents.parse_frontmatter, id="subagents"),
        pytest.param(config_write_skills.parse_frontmatter, id="skills"),
    ]


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize("char", ["\x85", "\u2028", "\u2029"])
def test_a_yaml_1_1_only_line_break_is_refused_on_both_surfaces(parse, char: str) -> None:
    # `tools: Read # c<LS>disallowedTools: Bash` is two keys to PyYAML and one comment to
    # the CLI, which then never sees the denylist.
    text = f"---\nname: a\ndescription: d\ntools: Read # c{char}disallowedTools: Bash\n---\nb\n"
    with pytest.raises(cw.InvalidCandidateError, match=f"U\\+{ord(char):04X}, a line break"):
        parse(text)


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize("value", ["a --- b", "'quoted --- too'", "x---"])
def test_a_triple_dash_inside_the_header_is_refused_on_both_surfaces(parse, value: str) -> None:
    # The CLI's lazy fence ends the header at the first `---`, even mid-line or in quotes.
    with pytest.raises(cw.InvalidCandidateError, match="contains '---' before its closing"):
        parse(f"---\nname: a\ndescription: {value}\n---\nb\n")


@pytest.mark.parametrize("parse", _both_parsers())
def test_an_alias_to_an_enclosing_node_is_refused_on_both_surfaces(parse) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="encloses it"):
        parse("---\nname: a\ndescription: d\nx: &x {<<: *x, k: v}\n---\nb\n")


def test_a_shared_alias_that_does_not_enclose_itself_still_loads() -> None:
    value = cw.load_frontmatter_yaml("a: &x {k: v}\nb: {<<: *x, j: 1}\nc: *x\n", what="f")
    assert value == {"a": {"k": "v"}, "b": {"k": "v", "j": 1}, "c": {"k": "v"}}


_NESTED_HOOK = (
    "---\nname: a\ndescription: d\nhooks:\n  PreToolUse:\n    - hooks:\n"
    "        - {{type: command, command: ./x.sh, {entry}}}\n---\nb\n"
)


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "entry",
    [
        "async: yes",
        "asyncRewake: On",
        "once: off",
        "continueOnBlock: no",
        "timeout: 1_000",
        "timeout: 010",
        "timeout: 0b101",
        "timeout: 1:30",
        "timeout: 1_0.5",
        "async: !!bool yes",
        "timeout: !!int '5'",
    ],
)
def test_a_yaml_1_1_only_spelling_in_frontmatter_hooks_is_refused_on_both_surfaces(
    parse, entry: str
) -> None:
    # Coordinator scope addition (2026-11-05): the walker is generic, so it needs no list
    # of which hook fields are booleans or numbers. YAML 1.2 reads each of these as a
    # string (or, for `010`, as 10), and the CLI then rejects or ignores the hook.
    with pytest.raises(cw.InvalidCandidateError, match="nested value at line 7"):
        parse(_NESTED_HOOK.format(entry=entry))


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "entry",
    [
        "async: true",
        "once: False",
        "timeout: 1000",
        "timeout: 0x1F",
        "timeout: 1.5",
        "timeout: -3",
        "async: 'yes'",
        "statusMessage: yes please",
        "async: !!bool true",
    ],
)
def test_a_spelling_both_yaml_versions_read_alike_still_loads(parse, entry: str) -> None:
    frontmatter, _body = parse(_NESTED_HOOK.format(entry=entry))
    assert "hooks" in frontmatter


@pytest.mark.parametrize("parse", _both_parsers())
def test_the_nested_walker_leaves_top_level_fields_to_the_surface(parse) -> None:
    # A skill's `user-invocable: yes` is true to the CLI as well (its boolean reader takes
    # the string), and a subagent surface checks its own top-level fields. A `<<` source
    # merged into the root stands in for top-level fields too, and a key is checked too.
    frontmatter, _ = parse("---\nname: a\ndescription: d\nbackground: yes\n---\nb\n")
    assert frontmatter["background"] is True
    frontmatter, _ = parse("---\nname: a\ndescription: d\nm: &m {k: 1}\n<<: *m\n---\nb\n")
    assert frontmatter["k"] == 1
    with pytest.raises(cw.InvalidCandidateError, match="nested value"):
        parse("---\nname: a\ndescription: d\nmetadata: {on: 1}\n---\nb\n")


def test_the_nested_walker_reports_a_position_never_the_value() -> None:
    secret = "FAKEFAKEFAKEFAKEFAKEfake42"
    with pytest.raises(cw.InvalidCandidateError) as excinfo:
        cw.load_frontmatter_yaml(f"{secret}:\n  a: yes\n", what="frontmatter", line_offset=1)
    assert secret not in str(excinfo.value)
    assert "line 3, column 6" in str(excinfo.value)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("a: yes", {"a": "yes"}),
        ("a: On", {"a": "On"}),
        ("a: TRUE", {"a": True}),
        ("a: tRue", {"a": "tRue"}),
        ("a: 010", {"a": 10}),
        ("a: 0o17", {"a": 15}),
        ("a: 0x1F", {"a": 31}),
        ("a: 0b101", {"a": "0b101"}),
        ("a: 1_000", {"a": "1_000"}),
        ("a: 1:30", {"a": "1:30"}),
        ("a: 1e3", {"a": 1000.0}),
        ("a: .5", {"a": 0.5}),
        ("a: 1.", {"a": 1.0}),
        ("a: -.inf", {"a": float("-inf")}),
        ("a: ~", {"a": None}),
        ("a:", {"a": None}),
        ("a: 2024-01-01", {"a": "2024-01-01"}),
        ('a: "010"', {"a": "010"}),
        ("a: !!str 010", {"a": "010"}),
        ("a: [yes, 1e3]", {"a": ["yes", 1000.0]}),
        ("b: &x {k: 1}\nc: {<<: *x, j: 2}", {"b": {"k": 1}, "c": {"k": 1, "j": 2}}),
        ("b: &x {k: 1}\nc: {<<: [*x, {k: 2}], j: 3}", {"b": {"k": 1}, "c": {"k": 1, "j": 3}}),
        ("", None),
    ],
)
def test_yaml12_view_reads_plain_scalars_by_the_yaml_1_2_core_schema(
    header: str, expected: object
) -> None:
    # The readings were checked against eemeli/yaml (YAML 1.2, core schema), the stand-in
    # for Bun.YAML, which cannot run on this host.
    assert cw.yaml12_view(header) == expected


def test_yaml12_view_marks_an_explicit_tag_it_cannot_vouch_for() -> None:
    value = cw.yaml12_view("a: !!int '5'\nb: !!binary aGk=\n")
    assert isinstance(value["a"], cw.Yaml12Unknown)
    assert isinstance(value["b"], cw.Yaml12Unknown)
    assert repr(value["a"]) == "<explicitly tagged YAML value>"
    assert math.isnan(cw.yaml12_view("a: .NaN")["a"])


def test_yaml12_view_maps_a_recursion_error_to_a_rejection(monkeypatch) -> None:
    def _deep(*_a, **_k):
        raise RecursionError

    monkeypatch.setattr(cw, "_yaml12_node", _deep)
    with pytest.raises(cw.InvalidCandidateError, match="nested too deeply"):
        cw.yaml12_view("a: 1")


@pytest.mark.parametrize("entry", ["async: yes", "timeout: 1_000"])
def test_a_subagent_file_with_a_yaml_1_1_only_hook_spelling_is_refused_whole(entry: str) -> None:
    # The write path of the subagent surface, end to end: `timeout: 1_000` is 1000 to
    # PyYAML (so the hooks validator alone would accept it) and the string "1_000" to the
    # CLI's YAML 1.2. The seam refuses it before any surface check runs.
    content = _NESTED_HOOK.format(entry=entry).replace("name: a", "name: my-agent")
    with pytest.raises(cw.InvalidCandidateError, match="nested value at line 7"):
        config_write_subagents.validate_agent_content(content, expected_name="my-agent")
    plain = content.replace(entry, "timeout: 1000")
    config_write_subagents.validate_agent_content(plain, expected_name="my-agent")  # no raise


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize("entry", ["timeout: 1e3", "timeout: 1.0e3", "timeout: 0o17"])
def test_a_nested_number_only_yaml_1_2_reads_is_refused_on_both_surfaces(
    parse, entry: str
) -> None:
    # The other direction: a string to PyYAML, a number to the CLI's YAML 1.2.
    with pytest.raises(cw.InvalidCandidateError, match="nested value at line 7"):
        parse(_NESTED_HOOK.format(entry=entry))


# --- TASK-449.6: the CLI's retry for a header that is not valid YAML -------------------
#
# The 2.1.318 loader (`Rd` -> `SMn`) parses the header, and when that fails it parses a copy
# with each top-level `key: value` line that holds a YAML indicator put in double quotes
# (`Bm`), and with leading tabs turned into two spaces each. The expected values below are
# what eemeli/yaml (YAML 1.2) gives for the retry text; it also fails on each original.


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        pytest.param(
            "argument-hint: [filename] [format]",
            {"argument-hint": "[filename] [format]"},
            id="docs-argument-hint-example",
        ),
        pytest.param(
            "metadata:\n\towner: me\n\ttier: 2",
            {"metadata": {"owner": "me", "tier": 2}},
            id="tab-indented-block",
        ),
        pytest.param("when: Use this: for deploys", {"when": "Use this: for deploys"}, id="colon"),
        pytest.param('when: say "hi": now', {"when": 'say "hi": now'}, id="inner-quotes"),
        pytest.param(
            "when: [a, b] [c]\nlist: [Read, Grep]",
            {"when": "[a, b] [c]", "list": ["Read", "Grep"]},
            id="flow-list-kept",
        ),
    ],
)
def test_a_header_the_cli_reads_through_its_retry_loads(parse, lines: str, expected) -> None:
    frontmatter, body = parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")
    assert frontmatter == {"name": "a", "description": "d", **expected}
    assert body == "body\n"


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "lines",
    [
        # Indented, so the retry does not quote it, and it stays invalid YAML.
        pytest.param("metadata:\n  a: b: c", id="nested-line-not-requoted"),
        # A CRLF line ends in \r, which JavaScript's `.` does not match: no retry for it.
        pytest.param("argument-hint: [a] [b]\r", id="crlf-line-not-requoted"),
        # Valid YAML with an unknown tag: no syntax error, so no retry.
        pytest.param("agent: !custom x", id="unknown-tag-is-not-retried"),
    ],
)
def test_a_header_the_retry_cannot_save_is_still_refused(parse, lines: str) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")


def test_a_valid_header_is_never_retried() -> None:
    # `a # b: c` would be quoted by the retry, but the header is valid YAML, so the CLI
    # parses it as written and reads the comment as a comment.
    header = "name: x\ndescription: a # b: c\n"
    assert cw.cli_frontmatter_text(header) == header
    assert cw.load_frontmatter_yaml(header, what="f") == {"name": "x", "description": "a"}


def test_the_yaml12_view_reads_the_retry_text_too() -> None:
    assert cw.yaml12_view("description: Use this: for x\n") == {"description": "Use this: for x"}


def test_a_too_deep_header_is_not_retried() -> None:
    # Block nesting: flow nesting this deep stops at the depth cap before any recursion.
    header = "a:\n" + "- " * 5000 + "x\n"
    assert cw.cli_frontmatter_text(header) == header


def test_a_subagent_file_the_cli_reads_through_its_retry_saves() -> None:
    content = "---\nname: my-agent\ndescription: Use this: for reviews\n---\nPrompt.\n"
    config_write_subagents.validate_agent_content(content, expected_name="my-agent")  # no raise


def test_frontmatter_header_keeps_the_cr_the_cli_keeps() -> None:
    text = "---\r\nname: a\r\nhint: [a] [b]\r\n---\r\nbody\r\n"
    match = cw.FRONTMATTER_RE.match(text)
    assert match is not None
    assert cw.frontmatter_header(text, match) == "name: a\r\nhint: [a] [b]\r"
    with pytest.raises(cw.InvalidCandidateError, match="not valid YAML"):
        config_write_skills.parse_frontmatter(text)
    lf = "---\nname: a\nhint: [a] [b]\n---\nbody\n"
    match = cw.FRONTMATTER_RE.match(lf)
    assert match is not None
    assert cw.frontmatter_header(lf, match) == "name: a\nhint: [a] [b]"
    assert config_write_skills.parse_frontmatter(lf)[0]["hint"] == "[a] [b]"


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "lines",
    [
        pytest.param("allowed-tools: &t [Bash]\nwhen_to_use: &t [Read]", id="two-anchors"),
        pytest.param("model: &d opus\nagent: &d x\nhint: [a] [b]", id="anchors-and-a-bad-line"),
        pytest.param("k: [&x a, &x b]\nhint: [a] [b]", id="anchors-in-a-flow-list"),
    ],
)
def test_a_duplicate_anchor_is_refused_not_retried(parse, lines: str) -> None:
    # YAML 1.2 allows two anchors of one name; PyYAML does not. The retry would read the
    # values as other strings, so refuse instead (fail closed). For the case with a bad
    # line the CLI does take its retry, but the refusal stands.
    with pytest.raises(cw.InvalidCandidateError, match="not valid YAML"):
        parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")


def test_a_merge_pyramid_in_a_retried_header_is_refused_quickly() -> None:
    levels = ["&l0 {k0: 0, k1: 1}"] + [
        f"&l{i} {{<<: [*l{i - 1}, *l{i - 1}]}}" for i in range(1, 22)
    ]
    header = "argument-hint: [a] [b]\nx: [" + ", ".join(levels) + "]\n"
    started = time.perf_counter()
    with pytest.raises(cw.InvalidCandidateError):
        cw.load_frontmatter_yaml(header, what="frontmatter")
    assert time.perf_counter() - started < 2.0


def test_the_retry_keeps_an_already_quoted_value() -> None:
    header = "when: 'a: b'\nhint: [a] [b]\n"
    assert cw.load_frontmatter_yaml(header, what="f") == {"when": "a: b", "hint": "[a] [b]"}


def test_the_retry_replaces_tabs_after_a_lone_cr() -> None:
    # JavaScript's multiline `^` also matches after a lone CR. (`m:\r\tk: v` would not do: the
    # CLI's line pattern takes \r\t as the space after `m:` and quotes `k: v`, as this port does.)
    assert cw._cli_retry_text("# c\r\tk: v\n\t\tj: w") == "# c\r  k: v\n    j: w"
    assert cw._cli_retry_text("m:\r\tk: v") == 'm: "k: v"'


def test_the_flow_list_check_refuses_to_answer_for_a_too_deep_value() -> None:
    # TASK-502: neither answer is safe (False would quote a list the CLI reads as a list),
    # so the depth error goes up to `cli_frontmatter_text`, which keeps the header.
    with pytest.raises(cw.FlowNestingTooDeepError):
        cw._is_yaml_list("[" * 5000 + "]" * 5000)
    depth = cw.MAX_NESTING_DEPTH
    assert cw._is_yaml_list("[" * depth + "]" * depth) is True
    assert cw._is_yaml_list("[a, b]") is True
    assert cw._is_yaml_list("[&x a, &x b]") is True  # a duplicate anchor: a list to YAML 1.2
    assert cw._is_yaml_list("[a] [b]") is False


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "lines",
    [
        pytest.param("description2: &d.1 Deploys prod\nhint: [a] [b]", id="anchor-with-a-dot"),
        pytest.param("x: &é v\nhint: [a] [b]", id="anchor-non-ascii"),
        pytest.param("x: &a v\ny: *a.b\nhint: [a] [b]", id="alias-with-a-dot"),
        pytest.param("x: [:a]\nhint: [a] [b]", id="flow-item-starts-with-colon"),
        # PyYAML cannot start a token at a tab after a value; YAML 1.2 reads a comment.
        pytest.param("disallowedTools: Bash\t# , Read", id="tab-then-hash-in-a-value"),
    ],
)
def test_syntax_only_yaml_1_2_accepts_is_refused_not_retried(parse, lines: str) -> None:
    # eemeli/yaml (YAML 1.2) parses each of these as written, so the CLI does not take the
    # retry. PyYAML refuses them; the retry would read other values, so refuse instead.
    with pytest.raises(cw.InvalidCandidateError, match="not valid YAML"):
        parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "lines",
    [
        pytest.param('when: "a"#b\ndisallowedTools: [Bash] # deny', id="hash-after-double-quote"),
        pytest.param("when: 'a'#b", id="hash-after-single-quote"),
        pytest.param("when: [x]#b", id="hash-after-flow-list"),
        pytest.param("when: {x: 1}#b", id="hash-after-flow-map"),
        pytest.param("when: x\rdisallowedTools: Bash", id="lone-carriage-return"),
    ],
)
def test_syntax_only_pyyaml_accepts_is_refused(parse, lines: str) -> None:
    # eemeli/yaml refuses each of these, so the CLI reads its retry text or no fields at
    # all: `[Bash] # deny` would become a string, and Bash would no longer be denied.
    with pytest.raises(cw.InvalidCandidateError, match="see differently"):
        parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize("lines", ['when: "a" #b', "when: [x] # b", "when: x#b", "when: 'it''s'"])
def test_a_comment_after_white_space_still_loads(parse, lines: str) -> None:
    parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")  # no raise


def test_the_lone_cr_position_names_the_line() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="line 2, column 5"):
        cw.load_frontmatter_yaml("a: 1\nb: x\rc: y\n", what="f")


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "lines",
    [
        pytest.param("x: [a,#c\n  b]\ndisallowedTools: [Bash] # deny", id="hash-after-comma"),
        pytest.param("x: [#c\n  a]", id="hash-after-open-bracket"),
        pytest.param("x: {#c\n  a: 1}", id="hash-after-open-brace"),
        pytest.param('x: "a\nb"\ndisallowedTools: Bash', id="double-quoted-at-column-0"),
        pytest.param("x: 'a\nb'", id="single-quoted-at-column-0"),
        pytest.param("x: a\nx: b\ndisallowedTools: Bash", id="duplicate-top-level-key"),
        pytest.param(
            "hooks:\n  Stop:\n    - hooks:\n        - {type: command, command: a, command: b}",
            id="duplicate-nested-key",
        ),
    ],
)
def test_more_syntax_the_two_yaml_versions_read_differently_is_refused(parse, lines) -> None:
    # Each is accepted by PyYAML and refused, or read otherwise, by eemeli/yaml (YAML 1.2).
    # The CLI would then read fewer fields (or none) than the editor checked.
    with pytest.raises(cw.InvalidCandidateError, match="see differently"):
        parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "lines",
    [
        'x: "a\n  b"',  # a quoted value that goes on indented
        "x: [a, # c\n  b]",  # a comment after a space
        "# a comment line\nx: 1",
        "a: &m {k: 1}\nb: {<<: *m, j: 2}",  # merge keys are not duplicates
        "url: http://x/#frag",  # a '#' with no space before it stays in a plain value
    ],
)
def test_the_same_syntax_written_portably_still_loads(parse, lines: str) -> None:
    parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")  # no raise


def test_a_tab_used_as_indentation_or_a_reserved_character_still_takes_the_retry() -> None:
    # Both YAML versions refuse these, so the CLI retries, and so does the seam.
    assert cw.cli_frontmatter_text("m:\n\tk: v\n") == "m:\n  k: v\n"
    assert cw.cli_frontmatter_text("x: @y\n") == 'x: "@y"\n'


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "keys",
    [
        pytest.param("1: a\n01: b", id="one-and-zero-one"),
        pytest.param("1: a\n1.0: b", id="int-and-float"),
        pytest.param("0x1: a\n1: b", id="hex-and-decimal"),
        pytest.param("null: a\n~: b", id="two-nulls"),
        pytest.param("true: a\nTrue: b", id="two-trues"),
        pytest.param('yes: a\n"yes": b', id="yes-and-quoted-yes"),
        pytest.param("x: &m {k: 1}\ny: {<<: *m, <<: *m}", id="two-merge-keys"),
    ],
)
def test_keys_that_one_yaml_reading_merges_are_refused(parse, keys: str) -> None:
    # Different spellings, one key to YAML 1.2 (or to PyYAML): a strict YAML 1.2 parser
    # refuses the header, so the CLI would drop every field, the denylist included.
    with pytest.raises(cw.InvalidCandidateError, match="see differently"):
        parse(f"---\nname: a\ndescription: d\n{keys}\ndisallowedTools: Bash\n---\nbody\n")


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "lines",
    [
        "when: >\n  Long text\n# Tools\nlist: Read",
        "when: |\n  Long text\n# Tools\nlist: Read",
        "when: |+\n  Long text\n\n# Tools\nlist: Read",
        "when: >\r\n  Long text\r\n# Tools\r\nlist: Read",
    ],
)
def test_a_comment_line_after_a_block_scalar_still_loads(parse, lines: str) -> None:
    frontmatter, _ = parse(f"---\nname: a\ndescription: d\n{lines}\n---\nbody\n")
    assert frontmatter["list"] == "Read"


def test_an_explicitly_tagged_key_never_collides_in_the_yaml12_reading() -> None:
    root = yaml.compose('a: {? !!int "5" : x, "5": y}\n', Loader=yaml.SafeLoader)
    # PyYAML reads the first key as 5 and the second as "5": no collision either way.
    assert cw._duplicate_key(root) is None


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "keys",
    [
        pytest.param("9007199254740993: a\n9007199254740992: b", id="past-2-53"),
        pytest.param("1e21: a\n1000000000000000000000: b", id="exponent-and-digits"),
        # Only PyYAML merges these (a Python dict folds 1 and True); the CLI keeps two.
        pytest.param("1: a\ntrue: b", id="pyyaml-only-one-and-true"),
        pytest.param("on: a\nyes: b", id="pyyaml-only-on-and-yes"),
    ],
)
def test_keys_one_reading_merges_past_the_obvious_cases_are_refused(parse, keys: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="see differently"):
        parse(f"---\nname: a\ndescription: d\n{keys}\ndisallowedTools: Bash\n---\nbody\n")


def test_the_yaml12_key_of_a_huge_integer_is_infinity() -> None:
    node = yaml.compose("1" + "0" * 400 + ": a\n", Loader=yaml.SafeLoader).value[0][0]
    assert cw._yaml12_key(node) == "Infinity"


def test_yaml12_view_of_a_scalar_header_is_the_scalar() -> None:
    assert cw.yaml12_view("just text") == "just text"


_MERGE_SPELLINGS = [
    pytest.param("<<", id="plain"),
    pytest.param("!!merge x", id="bang-bang-merge"),
    pytest.param("!<tag:yaml.org,2002:merge> x", id="verbatim-tag"),
    pytest.param("!!m%65rge x", id="percent-escaped"),
]


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize("merge", _MERGE_SPELLINGS)
def test_an_enclosing_merge_in_any_spelling_is_refused(parse, merge: str) -> None:
    # Every spelling composes to the merge tag, so the seam checks the composed graph, not
    # a `<<` in the text (a regression against main once the guard was text-gated).
    text = (
        f"---\nname: a\ndescription: d\npermissionMode: plan # c\na: &a {{{merge} : *a}}\n---\nb\n"
    )
    with pytest.raises(cw.InvalidCandidateError, match="encloses it"):
        parse(text)


@pytest.mark.timeout(20)
@pytest.mark.parametrize("merge", _MERGE_SPELLINGS)
def test_a_merge_pyramid_in_any_spelling_hits_the_pair_cap_quickly(merge: str) -> None:
    levels = ["l0: &l0 {k0: 0, k1: 1}"] + [
        f"l{i}: &l{i} {{{merge} : [*l{i - 1}, *l{i - 1}], k{i}: {i}}}" for i in range(1, 24)
    ]
    started = time.perf_counter()
    with pytest.raises(cw.InvalidCandidateError, match="pair cap"):
        cw.load_frontmatter_yaml("\n".join(levels) + "\n", what="frontmatter")
    assert time.perf_counter() - started < 5.0


def _override_pyramid(depth: int) -> list[str]:
    """Return anchor lines of an alias pyramid whose top, ``P{depth}``, a merge reaches only.

    Used behind an overridden merge key, so the loaded value stays tiny and every check on
    it passes: only the walks of the composed graph meet the pyramid.
    """
    lines = ["  - &P0 [a, a, a, a]"]
    lines += [
        f"  - &P{i} [*P{i - 1}, *P{i - 1}, *P{i - 1}, *P{i - 1}]" for i in range(1, depth + 1)
    ]
    return lines


@pytest.mark.timeout(20)
def test_every_seam_walk_is_memoized_on_an_override_shaped_pyramid() -> None:
    # Review of TASK-449.6: removing the visited set from any composed-tree walk left the
    # suite green but hung on this 530-byte shape. The anchors sit in a list under an
    # unknown key, which the CLI ignores, so nothing else refuses the file first.
    header = (
        "description: d\nanchors:\n"
        + "\n".join(_override_pyramid(20))
        + "\nmetadata: {<<: {x: *P20}, x: 1}\n"
    )
    root = yaml.compose(header, Loader=yaml.SafeLoader)
    started = time.perf_counter()
    cw._first_nested_yaml11_number(root)
    cw._duplicate_key(root)
    cw._has_enclosing_alias(root)
    cw._has_merge_key(root)
    cw.yaml12_view(header)
    assert time.perf_counter() - started < 2.0


def _inline_pyramid(depth: int) -> str:
    """Return a flow list whose items anchor an alias pyramid ``depth`` levels deep."""
    items = ["&P0 [a, a, a, a]"] + [
        f"&P{i} [*P{i - 1}, *P{i - 1}, *P{i - 1}, *P{i - 1}]" for i in range(1, depth + 1)
    ]
    return "[" + ", ".join(items) + "]"


@pytest.mark.timeout(20)
def test_an_override_shaped_pyramid_saves_quickly_on_both_surfaces() -> None:
    # The merge source is overridden, so the loaded value is tiny and passes every check on
    # it. Only the walks of the composed graph meet the pyramid, and each must visit a node
    # once (a walk per alias path would take hours).
    pyramid = _inline_pyramid(20)
    skill = f"---\ndescription: d\nmetadata: {{<<: {{x: {pyramid}}}, x: 1}}\n---\nb\n"
    agent = (
        "---\nname: x\ndescription: d\nmcpServers:\n"
        f"  - srv: {{command: c, <<: {{args: {pyramid}}}, args: []}}\n---\nb\n"
    )
    started = time.perf_counter()
    config_write_skills.validate_skill_md_content(skill)
    config_write_subagents.validate_agent_content(agent, expected_name="x")
    assert time.perf_counter() - started < 5.0


@pytest.mark.parametrize("parse", _both_parsers())
def test_a_huge_leading_zero_number_in_a_nested_value_is_refused_not_a_500(parse) -> None:
    # PyYAML reads `0999...` (5000 digits) as a string; YAML 1.2 reads a decimal number,
    # and int() past Python's digit limit raised ValueError after the load: a 500.
    huge = "0" + "9" * 5000
    assert cw.yaml12_view(f"y: {huge}") == {"y": math.inf}  # JavaScript Infinity
    with pytest.raises(cw.InvalidCandidateError, match="nested value"):
        parse(f"---\nname: a\ndescription: d\nx:\n  y: {huge}\n---\nb\n")


@pytest.mark.parametrize(
    "line",
    [f'maxTurns: "{"9" * 5000}"', f'effort: "{"9" * 5000}"', f'autoCompactWindow: "{"9" * 5000}"'],
)
def test_a_subagent_number_field_with_huge_digits_is_refused_not_a_500(line: str) -> None:
    text = f"---\nname: x\ndescription: d\n{line}\n---\nbody\n"
    with pytest.raises(cw.InvalidCandidateError):
        config_write_subagents.validate_agent_content(text, expected_name="x")


_HUGE_HEX = "0x" + "f" * 300  # bases 8 and 16 have no int() digit limit, but pass a double
_HUGE_OCT = "0o" + "7" * 400


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(f"x:\n  {_HUGE_HEX}: 1\n", id="nested-hex-key"),
        pytest.param(f"x:\n  {_HUGE_OCT}: 1\n", id="nested-oct-key"),
        pytest.param(f"x:\n  y: {_HUGE_HEX}\n", id="nested-hex-value"),
        pytest.param("x:\n  y: " + "9" * 309 + "\n", id="nested-decimal-past-a-double"),
    ],
)
def test_a_nested_number_past_the_double_range_is_refused_not_a_500(parse, extra: str) -> None:
    # YAML 1.1 reads a Python int; the CLI's YAML 1.2 reads Infinity, so they disagree. The
    # key spellings raised OverflowError in the duplicate-key check: a 500.
    with pytest.raises(cw.InvalidCandidateError, match="nested value"):
        parse(f"---\nname: a\ndescription: d\n{extra}---\nb\n")


@pytest.mark.parametrize("parse", _both_parsers())
def test_a_huge_top_level_hex_key_and_the_largest_nested_decimal_still_load(parse) -> None:
    parse(f"---\nname: a\ndescription: d\n{_HUGE_HEX}: 1\n---\nb\n")  # no raise
    parse("---\nname: a\ndescription: d\nx:\n  y: " + "9" * 308 + "\n---\nb\n")


def test_the_yaml12_view_reads_a_huge_hex_or_oct_number_as_infinity() -> None:
    assert cw.yaml12_view(f"a: {_HUGE_HEX}\nb: -{_HUGE_OCT}\nc: 0x10") == {
        "a": math.inf,
        "b": -math.inf,
        "c": 16,
    }


# --- Greptile on PR 1810: a `=` key, and one wrapper around every composed-tree check -----


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param("metadata: {=: x}", {"metadata": {"=": "x"}}, id="flow"),
        pytest.param("metadata:\n  =: x", {"metadata": {"=": "x"}}, id="block"),
        pytest.param("=: x", {"=": "x"}, id="top-level"),
        pytest.param("metadata: [{=: x}]", {"metadata": [{"=": "x"}]}, id="in-a-list"),
        pytest.param("metadata: {!!value k: x}", {"metadata": {"k": "x"}}, id="bang-value"),
        pytest.param(
            "metadata: {!<tag:yaml.org,2002:value> k: x}", {"metadata": {"k": "x"}}, id="verbatim"
        ),
        pytest.param(
            "base: &b {=: 1}\nmetadata: {<<: *b}",
            {"base": {"=": 1}, "metadata": {"=": 1}},
            id="merge-source",
        ),
    ],
)
def test_a_value_tagged_key_reads_as_pyyaml_reads_it(parse, extra: str, expected: dict) -> None:
    # PyYAML's construct_mapping retags a `=` (or `!!value`) KEY as a string; the duplicate-key
    # check constructed the node as it stood and raised ConstructorError: a 500.
    frontmatter, _body = parse(f"---\nname: a\ndescription: d\n{extra}\n---\nb\n")
    assert {k: v for k, v in frontmatter.items() if k not in ("name", "description")} == expected


@pytest.mark.parametrize("parse", _both_parsers())
def test_a_value_tagged_key_still_counts_as_a_repeated_key(parse) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="repeated key"):
        parse("---\nname: a\ndescription: d\nmetadata: {=: x, '=': y}\n---\nb\n")


class _CheckCrashedError(BaseException):
    """A check crashed: no `except Exception` in the seam can turn this into a refusal."""


@contextlib.contextmanager
def _crash_escapes(_what: str):
    try:
        yield
    except cw.InvalidCandidateError:
        raise
    except Exception as exc:
        raise _CheckCrashedError(type(exc).__name__) from exc


@pytest.mark.parametrize(
    "extra",
    [
        "metadata: {a: =}",
        "metadata: {? !!value [a] : x}",
        "base: &b {a: 1}\nmetadata: {<<: *b, c: 2}",
        "metadata: {!!merge <<: {a: 1}}",
        "metadata: !!set {a, b}",
        "metadata: !!omap [{a: 1}, {a: 2}]",
        "metadata: !!pairs [{a: 1}, {a: 2}]",
        "metadata: {a: !!binary aGk=}",
        "metadata: {? !!binary aGk= : x}",
        "metadata: {2024-01-01: x}",
        "metadata: {2024-01-01: x, !!timestamp 2024-01-01: y}",
        "metadata: {? [a] : x}",
        "metadata: {? {a: 1} : x}",
        "? [a]\n: x",
        "metadata: {~: x, null: y}",
        "metadata: {.nan: x}",
        "metadata: {!!bool yes: x}",
        "metadata: {!!int x: y}",
        "metadata: {!!str 1: x, 1: y}",
        "metadata: {!!yaml k: x}",
        "metadata: {: x}",
        "a: &k key\nmetadata: {*k : x}",
        "metadata: !!omap [{? [a] : 1}]",
        "metadata: !!pairs [{? {a: 1} : 1}]",
    ],
)
def test_a_tag_or_node_shape_pyyaml_loads_specially_never_escapes_the_seam(
    extra, monkeypatch
) -> None:
    # The self-pass on the third round: every PyYAML tag and node shape the seam's own node
    # walks might read differently from PyYAML's loader. Each loads or is refused by a check
    # that means to refuse it. The crash wrapper is replaced by one that raises a
    # BaseException no seam handler catches, so a check that crashes fails this test, whatever
    # the wrapper's message says and whatever handler sits around the check.
    monkeypatch.setattr(cw, "refuse_check_failures", _crash_escapes)
    text = f"---\nname: a\ndescription: d\n{extra}\n---\nb\n"
    surfaces = [
        lambda: config_write_subagents.parse_frontmatter(text),
        lambda: config_write_skills.parse_frontmatter(text),
        lambda: config_write_skills.validate_skill_md_content(text),
        lambda: config_write_subagents.validate_agent_content(text, expected_name="a"),
    ]
    for surface in surfaces:
        try:
            surface()
        except cw.InvalidCandidateError:
            pass


@pytest.mark.parametrize(
    "extra", ["metadata: !!omap [{? [a] : 1}]", "metadata: !!pairs [{? {a: 1} : 1}]"]
)
def test_a_list_or_mapping_key_in_an_ordered_mapping_is_refused(extra: str) -> None:
    # PyYAML builds `!!omap`/`!!pairs` without hashing a key; the YAML 1.2 view raised
    # TypeError on it (review on PR 1810).
    with pytest.raises(cw.InvalidCandidateError, match="a list or a mapping as a key"):
        config_write_skills.validate_skill_md_content(
            f"---\nname: a\ndescription: d\n{extra}\n---\nb\n"
        )


_CHECK_CRASHES = [
    pytest.param(yaml.constructor.ConstructorError("x", None, "SECRET-1"), id="ConstructorError"),
    pytest.param(ValueError("SECRET-1"), id="ValueError"),
    pytest.param(OverflowError("SECRET-1"), id="OverflowError"),
    pytest.param(TypeError("SECRET-1"), id="TypeError"),
    pytest.param(KeyError("SECRET-1"), id="KeyError"),
    pytest.param(AttributeError("SECRET-1"), id="AttributeError"),
    pytest.param(IndexError("SECRET-1"), id="IndexError"),
]


@pytest.mark.parametrize("parse", _both_parsers())
@pytest.mark.parametrize("crash", _CHECK_CRASHES)
@pytest.mark.parametrize("check", ["_yaml12_refuses", "_merge_expands_beyond"])
def test_a_crash_in_any_composed_tree_check_is_a_refusal(
    parse, crash: Exception, check: str, monkeypatch
) -> None:
    def _boom(*_args, **_kwargs):
        raise crash

    monkeypatch.setattr(cw, check, _boom)
    with pytest.raises(cw.InvalidCandidateError, match=type(crash).__name__) as caught:
        parse("---\nname: a\ndescription: d\n---\nb\n")
    assert "SECRET" not in str(caught.value)  # the class name only, never the payload


@pytest.mark.parametrize("parse", _both_parsers())
def test_a_recursion_error_in_a_check_is_a_refusal(parse, monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise RecursionError

    monkeypatch.setattr(cw, "_first_nested_yaml11_number", _boom)
    with pytest.raises(cw.InvalidCandidateError, match="nested too deeply to check"):
        parse("---\nname: a\ndescription: d\nx: {y: 1}\n---\nb\n")


def test_the_check_wrapper_passes_a_refusal_through_unchanged() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="^the original$"):
        with cw.refuse_check_failures("frontmatter"):
            raise cw.InvalidCandidateError("the original")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('"a\\"b" , x', 6),
        ("'it''s' , x", 7),
        ('"open', None),
        ("'open''", None),
        ('"ends in \\', None),
    ],
)
def test_quoted_end_reads_yaml_escapes(body: str, expected: int | None) -> None:
    assert cw._quoted_end(body, 0) == expected


def test_quoted_end_is_linear_on_many_escaped_quotes() -> None:
    # The CodeQL polynomial-regex shape: an unclosed quote then many `\"` pairs.
    body = '"' + '\\"' * 200_000
    start = time.perf_counter()
    assert cw._quoted_end(body, 0) is None
    assert time.perf_counter() - start < 2.0


def test_a_comment_inside_a_secret_block_still_masks_a_credential_url() -> None:
    text = "token:\n# deploy https://user:password@example.com\n  value\n"
    shown = cw.redact_secret_lines(text)
    assert "password" not in shown
    assert "value" not in shown
    assert shown.count("\n") == text.count("\n")
    restored, _lines = cw.restore_masked_lines(
        shown, text, what="SKILL.md", view=cw.redact_secret_lines
    )
    assert restored == text


@pytest.mark.parametrize(
    "line",
    [
        'env: {token: &a "first, secret-tail"}\n',
        "env: {token: !!str first, secret-tail}\n",
        "env: {token: &a [first, secret-tail]}\n",
    ],
)
def test_a_flow_value_behind_an_anchor_or_tag_is_masked_to_the_end(line: str) -> None:
    shown = cw.redact_secret_lines(line)
    assert "secret-tail" not in shown
    assert "first" not in shown
    restored, _lines = cw.restore_masked_lines(
        shown, line, what="SKILL.md", view=cw.redact_secret_lines
    )
    assert restored == line


@pytest.mark.parametrize(
    "text",
    [
        "token:\n# ${SECRET_LEAK}\n  v\n",
        "token:\n# {api_key: sk-live-LEAK}\n  v\n",
        "token: |\n  a\n# {token: LEAK}\n  b\n",
    ],
)
def test_a_comment_inside_a_secret_block_gets_every_line_mask(text: str) -> None:
    shown = cw.redact_secret_lines(text)
    assert "LEAK" not in shown
    restored, _lines = cw.restore_masked_lines(
        shown, text, what="SKILL.md", view=cw.redact_secret_lines
    )
    assert restored == text
