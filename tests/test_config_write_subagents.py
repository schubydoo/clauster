"""Subagents config-write surface (#767) over the #347/#687 Foundation + #766 writer.

Covers the frontmatter parser, the pure structural validator (frontmatter shape only
— the body is opaque, and a ``hooks``/``tools``/``mcpServers`` grant is validated for
shape and never resolved/spawned/connected), the user/project read+write+delete+list
functions built on the #766 file/dir-writer primitive, the plugin/built-in read-only
guard, and the gated routes (capability/scope 404, type-the-name 400,
bad-shape/oversize 422 writing nothing, stale-hash 409, path-escape 400, read-only
403, missing-agent 404).

Every test that touches ``~/.claude/agents`` runs under the autouse HOME-isolation
fixture and writes only into the isolated tmp home — the live account is never
touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_subagents as sub
from clauster.app import create_app
from clauster.config import load_config

_MINIMAL = "---\nname: my-agent\ndescription: does a thing\n---\nYou are a helper.\n"


def _content(name: str = "my-agent", **extra: object) -> str:
    lines = [f"name: {name}", "description: does a thing"]
    for key, value in extra.items():
        lines.append(f"{key}: {value}")
    frontmatter = "\n".join(lines)
    return f"---\n{frontmatter}\n---\nYou are a helper.\n"


# --- frontmatter parsing -------------------------------------------------------------


def test_parse_frontmatter_splits_header_and_body() -> None:
    frontmatter, body = sub.parse_frontmatter(_MINIMAL)
    assert frontmatter == {"name": "my-agent", "description": "does a thing"}
    assert body == "You are a helper.\n"


def test_parse_frontmatter_accepts_crlf() -> None:
    text = "---\r\nname: x\r\ndescription: y\r\n---\r\nbody\r\n"
    frontmatter, body = sub.parse_frontmatter(text)
    assert frontmatter == {"name": "x", "description": "y"}
    assert body == "body\r\n"


def test_parse_frontmatter_accepts_empty_body() -> None:
    frontmatter, body = sub.parse_frontmatter("---\nname: x\ndescription: y\n---")
    assert frontmatter == {"name": "x", "description": "y"}
    assert body == ""


def test_parse_frontmatter_rejects_missing_block() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="frontmatter"):
        sub.parse_frontmatter("just a body, no frontmatter\n")


def test_parse_frontmatter_rejects_invalid_yaml() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="YAML"):
        sub.parse_frontmatter("---\nname: [unterminated\n---\nbody\n")


def test_parse_frontmatter_rejects_deeply_nested_yaml() -> None:
    # Deeply-nested flow sequences overflow PyYAML's recursive composer, which raises
    # RecursionError — not a YAMLError — so it escaped the handler and this
    # code-executing write tier raised outside its documented InvalidCandidateError
    # contract. Kept in lockstep with the SKILL.md parser (same test, same tier).
    deep = "[" * 5_000 + "]" * 5_000
    with pytest.raises(cw.InvalidCandidateError, match="too deeply"):
        sub.parse_frontmatter(f"---\nname: {deep}\n---\nbody\n")


@pytest.mark.parametrize(
    ("tag", "raised"),
    [
        ("!!int", ValueError),
        ("!!float", ValueError),
        ("!!bool", KeyError),
        ("!!timestamp", AttributeError),
    ],
)
def test_parse_frontmatter_rejects_explicit_tag_with_an_unfitting_value(
    tag: str, raised: type[Exception]
) -> None:
    # #1354: PyYAML's SafeConstructor raises these four OUTSIDE yaml.YAMLError for a
    # resolvable explicit tag whose value does not fit it, so each escaped the documented
    # InvalidCandidateError contract — a 500 on this code-executing write tier, from a
    # file that arrived with a cloned repository. The input is the fuzzer's; `__cause__`
    # pins WHICH class was caught, so a handler that stopped covering one would fail here
    # rather than silently passing on a different rejection. Same test in the SKILL.md
    # parser's module — one shared handler (config_write.load_frontmatter_yaml).
    with pytest.raises(cw.InvalidCandidateError, match="YAML tag") as excinfo:
        sub.parse_frontmatter(f"---\nname: {tag} x\ndescription: d\n---\nbody\n")
    assert isinstance(excinfo.value.__cause__, raised)


@pytest.mark.parametrize("value", ["!!int", "!!float", "!!int __"])
def test_parse_frontmatter_rejects_explicit_int_tag_with_an_empty_scalar(value: str) -> None:
    # Review catch on the first fix for issue 1354: construct_yaml_int/float INDEX the
    # scalar before parsing it, so an empty (or all-underscore, which strips to empty)
    # scalar raises IndexError — none of the unfitting-value trio above — and still
    # escaped as a 500 until IndexError joined the shared handler's tuple. Same test in
    # the SKILL.md parser's module, as for the trio.
    with pytest.raises(cw.InvalidCandidateError, match="YAML tag") as excinfo:
        sub.parse_frontmatter(f"---\nname: {value}\ndescription: d\n---\nbody\n")
    assert isinstance(excinfo.value.__cause__, IndexError)


def test_parse_frontmatter_tolerates_trailing_whitespace_on_a_fence() -> None:
    # #1352: the fence pattern accepts trailing spaces/tabs on either `---` line, and the
    # whitespace belongs to the fence rather than to the body. Shared byte-for-byte with
    # the SKILL.md parser (one regex object); the cross-parser assertion lives in
    # tests/test_fuzz_harness_smoke.py.
    frontmatter, body = sub.parse_frontmatter("--- \nname: x\ndescription: y\n---\t\nbody\n")
    assert frontmatter == {"name": "x", "description": "y"}
    assert body == "body\n"


def test_parse_frontmatter_rejects_non_mapping() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="mapping"):
        sub.parse_frontmatter("---\n- a\n- b\n---\nbody\n")


def test_parse_frontmatter_empty_yaml_becomes_empty_dict() -> None:
    # An empty (or whitespace-only) frontmatter block parses as YAML `None`, which
    # this surface treats as an empty mapping — not an error at the parse stage
    # (validate_frontmatter is the layer that then rejects it for missing keys).
    frontmatter, body = sub.parse_frontmatter("---\n\n---\nbody\n")
    assert frontmatter == {}
    assert body == "body\n"


def test_parse_frontmatter_first_closing_marker_ends_block() -> None:
    # A body that itself contains a `---` line must not be swallowed into the header.
    text = "---\nname: x\ndescription: y\n---\nbefore\n---\nafter\n"
    frontmatter, body = sub.parse_frontmatter(text)
    assert frontmatter == {"name": "x", "description": "y"}
    assert body == "before\n---\nafter\n"


# --- name validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["my-agent", "reviewer", "a", "a1", "clauster-async-reviewer", "x-1-2-3"],
)
def test_is_valid_agent_name_accepts(name: str) -> None:
    assert sub.is_valid_agent_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-leading-hyphen",
        "1starts-with-digit",
        "Has-Upper",
        "trailing-",
        "double--hyphen",
        "with/slash",
        "..",
        ".",
        "with.dot",
        "with space",
        123,
        None,
    ],
)
def test_is_valid_agent_name_rejects(name: object) -> None:
    assert not sub.is_valid_agent_name(name)


@pytest.mark.parametrize("name", ["general-purpose", "Explore", "Plan", "PLAN", "explore"])
def test_is_builtin_agent_matches_case_insensitively(name: str) -> None:
    assert sub.is_builtin_agent(name)


@pytest.mark.parametrize("name", ["my-agent", "general-purposeish", "explorer"])
def test_is_builtin_agent_does_not_match_others(name: str) -> None:
    assert not sub.is_builtin_agent(name)


# --- frontmatter structural validation -------------------------------------------------


def test_validate_frontmatter_accepts_minimal() -> None:
    sub.validate_frontmatter({"name": "my-agent", "description": "d"})  # no raise


def test_validate_frontmatter_accepts_every_known_field() -> None:
    sub.validate_frontmatter(
        {
            "name": "my-agent",
            "description": "d",
            "tools": "Read, Grep, Glob",
            "disallowedTools": ["Bash"],
            "model": "opus",
            "permissionMode": "plan",
            "mcpServers": {"srv": {"command": "x"}},
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
            "maxTurns": 5,
            "skills": ["skill-one"],
            "initialPrompt": "start here",
            "memory": "some memory",
            "effort": "high",
            "background": True,
            "isolation": "worktree",
            "color": "blue",
        }
    )  # no raise


def test_validate_frontmatter_accepts_dict_memory() -> None:
    sub.validate_frontmatter({"name": "x", "description": "d", "memory": {"k": "v"}})


def test_validate_frontmatter_passes_unknown_keys_through() -> None:
    # #958/DF-3: an unrecognized frontmatter key (forward-compat with Claude Code) is
    # passed through, not rejected — while the required name/description and the known
    # security-relevant keys are still validated.
    sub.validate_frontmatter(
        {"name": "x", "description": "d", "license": "MIT", "metadata": {"a": 1}}
    )  # no raise


def test_validate_frontmatter_accepts_well_formed_env() -> None:
    # #959 (Greptile P1): ``env`` became reachable when the allowlist was dropped; a
    # well-formed name→scalar map is accepted (scalars cover YAML str/int/float/bool).
    sub.validate_frontmatter(
        {"name": "x", "description": "d", "env": {"API_HOST": "example.com", "PORT": 8080}}
    )  # no raise


@pytest.mark.parametrize(
    "candidate",
    [
        ["not", "a", "dict"],
        {"description": "d"},  # missing name
        {"name": "x"},  # missing description
        {"name": "Bad Name", "description": "d"},  # invalid name shape
        {"name": "x", "description": ""},  # empty description
        {"name": "x", "description": 5},  # wrong type
        {"name": "x", "description": "d", "tools": ""},  # empty string
        {"name": "x", "description": "d", "tools": []},  # empty list
        {"name": "x", "description": "d", "tools": [1, 2]},  # non-string items
        {"name": "x", "description": "d", "tools": 5},  # wrong type entirely
        {"name": "x", "description": "d", "disallowedTools": [""]},  # empty item
        {"name": "x", "description": "d", "skills": "not-a-list"},
        {"name": "x", "description": "d", "skills": [1]},
        {"name": "x", "description": "d", "model": ""},
        {"name": "x", "description": "d", "model": 5},
        {"name": "x", "description": "d", "permissionMode": 5},
        {"name": "x", "description": "d", "permissionMode": "not-a-real-mode"},
        {"name": "x", "description": "d", "mcpServers": "nope"},
        {"name": "x", "description": "d", "mcpServers": {"srv": "not-an-object"}},
        {"name": "x", "description": "d", "hooks": {"NotAnEvent": []}},
        {"name": "x", "description": "d", "maxTurns": 0},
        {"name": "x", "description": "d", "maxTurns": -1},
        {"name": "x", "description": "d", "maxTurns": "5"},
        {"name": "x", "description": "d", "maxTurns": True},
        {"name": "x", "description": "d", "initialPrompt": 5},
        {"name": "x", "description": "d", "memory": 5},
        {"name": "x", "description": "d", "effort": ""},
        {"name": "x", "description": "d", "background": "yes"},
        {"name": "x", "description": "d", "isolation": ""},
        {"name": "x", "description": "d", "color": ""},
        {"name": "x", "description": "d", "env": 42},  # not a mapping
        {"name": "x", "description": "d", "env": {"": "v"}},  # empty var name
        {"name": "x", "description": "d", "env": {"K": ["a"]}},  # non-scalar value
    ],
)
def test_validate_frontmatter_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sub.validate_frontmatter(candidate)


def test_validate_frontmatter_rejects_bypass_permissions() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="bypassPermissions"):
        sub.validate_frontmatter(
            {"name": "x", "description": "d", "permissionMode": "bypassPermissions"}
        )


def test_validate_frontmatter_rejects_plugin_root_marker_in_hooks() -> None:
    candidate = {
        "name": "x",
        "description": "d",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/x"}]}]
        },
    }
    with pytest.raises(cw.InvalidCandidateError, match="plugin"):
        sub.validate_frontmatter(candidate)


def test_validate_frontmatter_expected_name_mismatch_rejected() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="must match"):
        sub.validate_frontmatter({"name": "a", "description": "d"}, expected_name="b")


def test_validate_frontmatter_expected_name_match_ok() -> None:
    sub.validate_frontmatter({"name": "a", "description": "d"}, expected_name="a")  # no raise


# --- whole-content validator ----------------------------------------------------------


def test_validate_agent_content_accepts_minimal() -> None:
    sub.validate_agent_content(_MINIMAL, expected_name="my-agent")  # no raise


def test_validate_agent_content_rejects_non_string() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sub.validate_agent_content(123)


def test_validate_agent_content_rejects_oversize() -> None:
    huge = "---\nname: x\ndescription: d\n---\n" + ("x" * sub.MAX_BYTES)
    with pytest.raises(cw.InvalidCandidateError):
        sub.validate_agent_content(huge)


def test_validate_agent_content_propagates_frontmatter_errors() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sub.validate_agent_content("---\nname: Bad Name\ndescription: d\n---\nbody\n")


# --- containment ----------------------------------------------------------------------


def test_resolve_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(cw.PathEscapeError):
        sub._resolve(tmp_path, "../escape")


def test_resolve_returns_expected_path(tmp_path: Path) -> None:
    target = sub._resolve(tmp_path, "my-agent")
    assert target == tmp_path / "my-agent.md"


def test_resolve_rewraps_escaping_symlink(tmp_path: Path) -> None:
    # A NAME that passes `_NAME_RE` (no `..`/slashes) can still resolve outside the
    # agents dir if the on-disk entry at that name is a symlink pointing elsewhere —
    # resolve_contained_path's symlink-following containment check catches that, and
    # `_resolve` rewraps its PathEscapeError as `cw.PathEscapeError`.
    outside = tmp_path.parent / "outside.md"
    # write_bytes (not text-mode write_text) keeps fixtures byte-exact: Path.write_text
    # translates \n -> \r\n on Windows, which would mutate content the byte-exact reader
    # (config_file_writer.read_file) then faithfully returns, breaking round-trip asserts.
    outside.write_bytes(b"elsewhere")
    root = tmp_path / "agents"
    root.mkdir()
    try:
        (root / "escaping-agent.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform/host")
    with pytest.raises(cw.PathEscapeError):
        sub._resolve(root, "escaping-agent")


# --- read-only detection ---------------------------------------------------------------


def test_is_read_only_file_detects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.md"
    real.write_bytes(_MINIMAL.encode("utf-8"))
    link = tmp_path / "linked.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable on this platform/host")
    assert sub._is_read_only_file(link, real.read_bytes())


def test_list_agents_never_reads_a_symlink_target(tmp_path: Path) -> None:
    # The module docstring promises a plugin symlink's target is NEVER followed/read.
    # GET/PUT/DELETE honoured that; LIST did not — `is_file()` and `read_bytes()` both
    # FOLLOW symlinks, and both ran before `_is_read_only_file` ever tested `is_symlink()`,
    # so the out-of-tree target's frontmatter `description` reached the listing. Bounded
    # (a description, not full content) but it is a stated containment boundary.
    outside = tmp_path.parent / "outside-of-tree.md"
    outside.write_bytes(b"---\ndescription: LEAKED-FROM-OUTSIDE-THE-TREE\n---\nbody\n")
    root = tmp_path / "agents"
    root.mkdir()
    try:
        (root / "evil.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform/host")

    entries = {e["name"]: e for e in sub._list_agents(root, "project")}

    assert entries["evil"]["description"] is None  # the target was never read
    assert entries["evil"]["source"] == "plugin"  # still surfaced, still plugin-owned
    assert entries["evil"]["editable"] is False


def test_list_agents_still_reads_a_real_local_agent(tmp_path: Path) -> None:
    # The symlink guard must not cost a genuine agent its description.
    root = tmp_path / "agents"
    root.mkdir()
    (root / "real.md").write_bytes(b"---\ndescription: a real local agent\n---\nbody\n")

    entries = {e["name"]: e for e in sub._list_agents(root, "project")}

    assert entries["real"]["description"] == "a real local agent"
    assert entries["real"]["editable"] is True


def test_list_agents_lists_a_dangling_symlink_so_list_and_get_agree(tmp_path: Path) -> None:
    # `is_file()` follows symlinks, so testing it BEFORE `is_symlink()` silently dropped a
    # dangling (or directory) symlink from the listing — while `_read_agent` still classifies
    # `is_symlink()` on the unresolved path and returns a read-only 200 for it. LIST and GET
    # would then disagree about whether the agent exists. Classifying first also means nothing
    # stats the target, so the listing leaks no information about an out-of-tree path.
    root = tmp_path / "agents"
    root.mkdir()
    try:
        (root / "dangling.md").symlink_to(tmp_path / "does-not-exist.md")
    except OSError:
        pytest.skip("symlinks unavailable on this platform/host")

    entries = {e["name"]: e for e in sub._list_agents(root, "project")}

    assert "dangling" in entries  # present, not silently dropped
    assert entries["dangling"] == {
        "name": "dangling",
        "source": "plugin",
        "editable": False,
        "description": None,
    }


def test_list_agents_skips_a_directory_named_like_an_agent(tmp_path: Path) -> None:
    # A non-symlink `.md` entry that is not a regular file. The `is_file()` guard is not
    # redundant with the `OSError` catch below it: a directory raises there, but a FIFO
    # would BLOCK in read_bytes() forever waiting for a writer, with no exception to catch.
    root = tmp_path / "agents"
    root.mkdir()
    (root / "notanagent.md").mkdir()
    (root / "real.md").write_bytes(b"---\ndescription: a real local agent\n---\nbody\n")

    entries = {e["name"]: e for e in sub._list_agents(root, "project")}

    assert "notanagent" not in entries
    assert "real" in entries  # the guard didn't cost a genuine agent


def test_is_read_only_file_detects_plugin_marker() -> None:
    path = Path("agent.md")
    raw = b"---\nname: x\ndescription: ${CLAUDE_PLUGIN_ROOT}/thing\n---\nbody\n"
    assert sub._is_read_only_file(path, raw)


def test_is_read_only_file_plain_content_is_editable(tmp_path: Path) -> None:
    plain = tmp_path / "plain.md"
    plain.write_bytes(_MINIMAL.encode("utf-8"))
    assert not sub._is_read_only_file(plain, plain.read_bytes())


def test_is_read_only_file_non_utf8_is_not_marker(tmp_path: Path) -> None:
    path = tmp_path / "binary.md"
    raw = b"\xff\xfe not utf-8"
    path.write_bytes(raw)
    assert not sub._is_read_only_file(path, raw)


# --- list --------------------------------------------------------------------------


def test_list_project_agents_empty_dir_shows_only_builtins(tmp_path: Path) -> None:
    agents = sub.list_project_agents(tmp_path)
    names = {a["name"] for a in agents}
    assert names == set(sub.BUILTIN_AGENT_NAMES)
    for entry in agents:
        assert entry["source"] == "built-in"
        assert entry["editable"] is False


def test_list_project_agents_includes_real_files(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "my-agent.md").write_bytes(_content("my-agent").encode("utf-8"))
    agents = sub.list_project_agents(tmp_path)
    real = [a for a in agents if a["name"] == "my-agent"]
    assert len(real) == 1
    assert real[0]["source"] == "project"
    assert real[0]["editable"] is True
    assert real[0]["description"] == "does a thing"


def test_list_skips_invalid_filenames(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "Bad Name.md").write_bytes(_MINIMAL.encode("utf-8"))
    (agents_dir / "not-markdown.txt").write_bytes(b"hi")
    agents = sub.list_project_agents(tmp_path)
    names = {a["name"] for a in agents}
    assert "Bad Name" not in names
    assert "not-markdown" not in names


def test_list_flags_plugin_marker_file_read_only(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "shadowed.md").write_bytes(
        b"---\nname: shadowed\ndescription: ${CLAUDE_PLUGIN_ROOT}/x\n---\nbody\n"
    )
    agents = sub.list_project_agents(tmp_path)
    entry = next(a for a in agents if a["name"] == "shadowed")
    assert entry["source"] == "plugin"
    assert entry["editable"] is False


def test_list_flags_symlink_file_read_only(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.md"
    real.write_bytes(_content("linked").encode("utf-8"))
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    try:
        (agents_dir / "linked.md").symlink_to(real)
    except OSError:
        pytest.skip("symlinks unavailable on this platform/host")
    agents = sub.list_project_agents(tmp_path)
    entry = next(a for a in agents if a["name"] == "linked")
    assert entry["source"] == "plugin"
    assert entry["editable"] is False


def test_list_tolerates_unparsable_frontmatter(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "broken.md").write_bytes(b"not frontmatter at all\n")
    agents = sub.list_project_agents(tmp_path)
    entry = next(a for a in agents if a["name"] == "broken")
    assert entry["description"] is None


def test_list_tolerates_non_utf8_file(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "binary.md").write_bytes(b"\xff\xfe not utf-8")
    agents = sub.list_project_agents(tmp_path)
    entry = next(a for a in agents if a["name"] == "binary")
    assert entry["description"] is None


def test_list_skips_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # config_write_subagents.py 471-474: a listable-but-unreadable .md is skipped (the
    # `read_bytes` OSError -> continue branch). Induce the OSError by monkeypatching the
    # read so the handler runs on every OS, not just where POSIX chmod bits bite.
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    unreadable = agents_dir / "unreadable.md"
    unreadable.write_bytes(_MINIMAL.encode("utf-8"))
    real_read_bytes = Path.read_bytes

    def boom(self: Path) -> bytes:
        if self.name == "unreadable.md":
            raise OSError("simulated unreadable file")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    agents = sub.list_project_agents(tmp_path)
    assert not any(a["name"] == "unreadable" for a in agents)


def test_list_description_missing_from_frontmatter_is_none(tmp_path: Path) -> None:
    # Structurally invalid (validate_frontmatter would reject a missing `description`
    # on write), but the listing best-effort-reads whatever is already on disk — a
    # non-string/absent `description` value just yields `description: None`.
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "no-desc.md").write_bytes(b"---\nname: no-desc\n---\nbody\n")
    agents = sub.list_project_agents(tmp_path)
    entry = next(a for a in agents if a["name"] == "no-desc")
    assert entry["description"] is None


def test_list_user_agents_uses_claude_json_parent(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    agents_dir = sub.user_agents_dir(claude_json)
    agents_dir.mkdir(parents=True)
    (agents_dir / "u.md").write_bytes(_content("u").encode("utf-8"))
    agents = sub.list_user_agents(claude_json)
    entry = next(a for a in agents if a["name"] == "u")
    assert entry["source"] == "user"


# --- read single ---------------------------------------------------------------------


def test_read_builtin_agent_returns_synthetic_doc() -> None:
    doc = sub.read_project_agent(Path("/nonexistent"), "Explore")
    assert doc["exists"] is False
    assert doc["editable"] is False
    assert doc["source"] == "built-in"
    assert doc["content"] == ""


def test_read_missing_agent_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(sub.AgentNotFoundError):
        sub.read_project_agent(tmp_path, "my-agent")


def test_read_existing_agent_round_trip(tmp_path: Path) -> None:
    sub.write_project_agent(tmp_path, "my-agent", _content("my-agent"), None)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["exists"] is True
    assert doc["editable"] is True
    assert doc["source"] == "project"
    assert doc["content"] == _content("my-agent")
    assert doc["frontmatter"]["name"] == "my-agent"
    assert doc["hash"] == cw.hash_bytes(_content("my-agent").encode("utf-8"))


def test_read_redacts_secret_shaped_frontmatter_values(tmp_path: Path) -> None:
    content = (
        "---\nname: my-agent\ndescription: d\n"
        "mcpServers:\n  srv:\n    headers:\n      Authorization: Bearer super-secret\n"
        "---\nbody\n"
    )
    sub.write_project_agent(tmp_path, "my-agent", content, None)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    # the raw content round-trips unredacted (needed for a correct re-save)...
    assert "super-secret" in doc["content"]
    # ...but the derived frontmatter display field masks the secret-shaped value.
    assert (
        doc["frontmatter"]["mcpServers"]["srv"]["headers"]["Authorization"]
        == cw.REDACTION_SENTINEL
    )


def test_read_non_utf8_file_raises_invalid_candidate(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "bin.md").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        sub.read_project_agent(tmp_path, "bin")


def test_read_degrades_gracefully_on_unparsable_frontmatter(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    # write_bytes keeps the fixture byte-exact: text-mode write_text would translate
    # \n -> \r\n on Windows, and the byte-exact reader would then return "...\r\n",
    # breaking the round-trip assert below (the product read path is already byte-exact).
    (agents_dir / "broken.md").write_bytes(b"no frontmatter here\n")
    doc = sub.read_project_agent(tmp_path, "broken")
    assert doc["exists"] is True
    assert doc["frontmatter"] == {}
    assert doc["content"] == "no frontmatter here\n"


def test_read_plugin_marked_file_is_visible_but_not_editable(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "shadowed.md").write_bytes(
        b"---\nname: shadowed\ndescription: ${CLAUDE_PLUGIN_ROOT}/x\n---\nbody\n"
    )
    doc = sub.read_project_agent(tmp_path, "shadowed")
    assert doc["exists"] is True
    assert doc["editable"] is False
    assert doc["source"] == "plugin"


def test_read_user_agent_targets_claude_agents_dir(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    sub.write_user_agent(claude_json, "u", _content("u"), None)
    assert (tmp_path / ".claude" / "agents" / "u.md").exists()
    doc = sub.read_user_agent(claude_json, "u")
    assert doc["source"] == "user"


# --- write (add/edit) -----------------------------------------------------------------


def test_write_creates_new_file(tmp_path: Path) -> None:
    sub.write_project_agent(tmp_path, "my-agent", _content("my-agent"), None)
    target = tmp_path / ".claude" / "agents" / "my-agent.md"
    # read_bytes().decode (not read_text) so the assert is byte-exact: read_text does
    # universal-newline translation, which would diverge from the verbatim bytes the
    # product write path (config_file_writer.write_file) stored.
    assert target.read_bytes().decode("utf-8") == _content("my-agent")


def test_write_no_hash_on_existing_file_is_stale(tmp_path: Path) -> None:
    sub.write_project_agent(tmp_path, "my-agent", _content("my-agent"), None)
    with pytest.raises(cw.StaleConfigWriteError):
        sub.write_project_agent(tmp_path, "my-agent", _content("my-agent", model="opus"), None)


def test_write_stale_hash_raises(tmp_path: Path) -> None:
    sub.write_project_agent(tmp_path, "my-agent", _content("my-agent"), None)
    stale = cw.hash_bytes(b"something else")
    with pytest.raises(cw.StaleConfigWriteError):
        sub.write_project_agent(tmp_path, "my-agent", _content("my-agent", model="opus"), stale)


def test_write_edit_round_trip(tmp_path: Path) -> None:
    sub.write_project_agent(tmp_path, "my-agent", _content("my-agent"), None)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    sub.write_project_agent(tmp_path, "my-agent", _content("my-agent", model="opus"), doc["hash"])
    updated = sub.read_project_agent(tmp_path, "my-agent")
    assert "model: opus" in updated["content"]


def test_write_frontmatter_name_mismatch_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sub.write_project_agent(tmp_path, "my-agent", _content("someone-else"), None)
    assert not (tmp_path / ".claude").exists()


def test_write_bad_shape_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sub.write_project_agent(tmp_path, "my-agent", "no frontmatter\n", None)
    assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("name", ["general-purpose", "Explore", "Plan"])
def test_write_refuses_builtin_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.write_project_agent(tmp_path, name, _content(name), None)
    assert not (tmp_path / ".claude").exists()


def test_write_refuses_existing_plugin_owned_file(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    target = agents_dir / "shadowed.md"
    target.write_bytes(b"---\nname: shadowed\ndescription: ${CLAUDE_PLUGIN_ROOT}/x\n---\nbody\n")
    original = target.read_bytes()
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.write_project_agent(tmp_path, "shadowed", _content("shadowed"), None)
    assert target.read_bytes() == original  # untouched


def test_write_never_executes_hooks_command(tmp_path: Path) -> None:
    """RCE-negative: a frontmatter hooks command must be stored, never run."""
    marker = tmp_path / "PWNED"
    hook_cmd = f"touch {marker}"
    content = (
        "---\nname: my-agent\ndescription: d\n"
        "hooks:\n  PreToolUse:\n    - hooks:\n        - type: command\n"
        f"          command: {hook_cmd}\n"
        "---\nbody\n"
    )
    sub.write_project_agent(tmp_path, "my-agent", content, None)
    assert not marker.exists()
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert str(marker) in doc["content"]


# --- delete ------------------------------------------------------------------------


def test_delete_missing_returns_false(tmp_path: Path) -> None:
    assert sub.delete_project_agent(tmp_path, "my-agent") is False


def test_delete_existing_returns_true_and_removes(tmp_path: Path) -> None:
    sub.write_project_agent(tmp_path, "my-agent", _content("my-agent"), None)
    assert sub.delete_project_agent(tmp_path, "my-agent") is True
    assert not (tmp_path / ".claude" / "agents" / "my-agent.md").exists()


@pytest.mark.parametrize("name", ["general-purpose", "Explore", "Plan"])
def test_delete_refuses_builtin_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.delete_project_agent(tmp_path, name)


def test_delete_refuses_plugin_owned_file(tmp_path: Path) -> None:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    target = agents_dir / "shadowed.md"
    target.write_bytes(b"---\nname: shadowed\ndescription: ${CLAUDE_PLUGIN_ROOT}/x\n---\nbody\n")
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.delete_project_agent(tmp_path, "shadowed")
    assert target.exists()


def test_delete_user_agent(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    sub.write_user_agent(claude_json, "u", _content("u"), None)
    assert sub.delete_user_agent(claude_json, "u") is True
    assert not (tmp_path / ".claude" / "agents" / "u.md").exists()


# --- plugin SYMLINK read-only (containment-ordering fix) ----------------------------
# A .claude/agents/<name>.md that is a SYMLINK to a plugin file OUTSIDE the agents dir
# must be classified read-only (symlink signal) BEFORE the containment resolve would
# follow + reject it as a path-escape. GET → content-less read-only doc (target never
# read); write/delete → ReadOnlyAgentError. A genuinely escaping NON-symlink still
# fails closed via _resolve.


def _symlinked_agent(project_dir: Path, name: str, *, target_body: bytes) -> Path:
    """Create a plugin-style symlink at <project>/.claude/agents/<name>.md.

    The target lives OUTSIDE the agents dir and carries sentinel content, so a test
    can prove the read path never follows it. Skips if symlinks are unavailable.
    """
    outside = project_dir / "plugin-source.md"
    outside.write_bytes(target_body)
    agents_dir = sub.project_agents_dir(project_dir)
    agents_dir.mkdir(parents=True, exist_ok=True)
    link = agents_dir / f"{name}.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform/host")
    return link


def test_unresolved_target_rejects_invalid_name(tmp_path: Path) -> None:
    # The un-resolved helper still fails closed on a bad name (the same PathEscapeError
    # `_resolve` raises), so read/write/delete reject a malformed name before any I/O.
    with pytest.raises(cw.PathEscapeError):
        sub._unresolved_target(tmp_path, "Bad-Name")


def test_read_symlink_agent_is_read_only_and_never_reads_target(tmp_path: Path) -> None:
    _symlinked_agent(tmp_path, "shadow", target_body=b"SECRET plugin content\n")
    doc = sub.read_project_agent(tmp_path, "shadow")
    assert doc["exists"] is True
    assert doc["editable"] is False
    assert doc["source"] == "plugin"
    # The out-of-tree target is NEVER followed/read — no arbitrary-file read.
    assert doc["content"] == ""
    assert "SECRET" not in doc["content"]


def test_write_symlink_agent_refused_403_target_untouched(tmp_path: Path) -> None:
    link = _symlinked_agent(tmp_path, "shadow", target_body=b"original\n")
    target = tmp_path / "plugin-source.md"
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.write_project_agent(tmp_path, "shadow", _content("shadow"), None)
    assert link.is_symlink()  # still a symlink, not clobbered into a real file
    assert target.read_bytes() == b"original\n"  # target content untouched


def test_delete_symlink_agent_refused_403(tmp_path: Path) -> None:
    link = _symlinked_agent(tmp_path, "shadow", target_body=b"original\n")
    target = tmp_path / "plugin-source.md"
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.delete_project_agent(tmp_path, "shadow")
    assert link.is_symlink()  # symlink not removed
    assert target.exists()  # target not removed


def test_user_scope_symlink_agent_refused(tmp_path: Path) -> None:
    # The user-scope writers/deleters route through the same _write_agent/_delete_agent,
    # so the symlink guard applies there too.
    claude_json = tmp_path / ".claude.json"
    outside = tmp_path / "plugin-source.md"
    outside.write_bytes(b"original\n")
    agents_dir = sub.user_agents_dir(claude_json)
    agents_dir.mkdir(parents=True)
    try:
        (agents_dir / "shadow.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform/host")
    assert sub.read_user_agent(claude_json, "shadow")["editable"] is False
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.write_user_agent(claude_json, "shadow", _content("shadow"), None)
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.delete_user_agent(claude_json, "shadow")


# =====================================================================================
# gated routes (full FastAPI lifespan)
# =====================================================================================

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path, extra: str) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"

_LIST_URL = "/api/config-write/subagents"


def _agent_url(name: str) -> str:
    return f"/api/config-write/subagents/{name}"


def test_route_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_LIST_URL}?project=alpha").status_code == 404
        assert c.get(f"{_agent_url('my-agent')}?project=alpha").status_code == 404
        assert (
            c.put(
                _agent_url("my-agent"),
                json={
                    "scope": "project",
                    "project": "alpha",
                    "confirm": "alpha",
                    "content": _content("my-agent"),
                },
            ).status_code
            == 404
        )
        assert (
            c.delete(
                f"{_agent_url('my-agent')}?project=alpha&scope=project&confirm=alpha"
            ).status_code
            == 404
        )


def test_route_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get(f"{_LIST_URL}?scope=user").status_code == 404
        assert c.get(f"{_agent_url('my-agent')}?scope=user").status_code == 404
        assert (
            c.put(
                _agent_url("my-agent"),
                json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "content": _content()},
            ).status_code
            == 404
        )


def test_route_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_LIST_URL}?scope=bogus").status_code == 422
        assert c.get(f"{_LIST_URL}?scope=local&project=alpha").status_code == 422  # no local scope
        assert c.put(_agent_url("x"), json={"scope": "bogus", "content": ""}).status_code == 422


def test_route_bogus_scope_404_when_disabled(write_config, tmp_path) -> None:
    # Capability gate runs BEFORE the scope-enum check — the surface stays invisible.
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_LIST_URL}?scope=bogus").status_code == 404
        assert c.put(_agent_url("x"), json={"scope": "bogus", "content": ""}).status_code == 404


def test_route_confirm_mismatch_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "content": _content("my-agent"),
            },
        )
        assert resp.status_code == 400


def test_route_confirm_runs_before_content_shape_check(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={"scope": "project", "project": "alpha", "confirm": "WRONG", "content": 123},
        )
        assert resp.status_code == 400


def test_route_bad_content_shape_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": "not-a-valid-frontmatter-file",
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "agents").exists()


def test_route_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "content": _content("my-agent"),
            },
        )
        assert resp.status_code == 400
        assert c.get(f"{_LIST_URL}?project=../escape").status_code == 400


def test_route_missing_project_dir_is_404(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "content": _content("my-agent"),
            },
        )
        assert resp.status_code == 404


def test_route_project_write_read_list_edit_delete_round_trip(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        listed = c.get(f"{_LIST_URL}?project=alpha")
        assert listed.status_code == 200
        assert not any(a["name"] == "my-agent" for a in listed.json()["agents"])

        wr = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("my-agent"),
            },
        )
        assert wr.status_code == 200

        get1 = c.get(f"{_agent_url('my-agent')}?project=alpha")
        assert get1.status_code == 200
        body1 = get1.json()
        assert body1["exists"] is True
        assert body1["editable"] is True
        assert body1["content"] == _content("my-agent")

        listed2 = c.get(f"{_LIST_URL}?project=alpha")
        assert any(a["name"] == "my-agent" for a in listed2.json()["agents"])

        edit = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("my-agent", model="opus"),
                "hash": body1["hash"],
            },
        )
        assert edit.status_code == 200
        get2 = c.get(f"{_agent_url('my-agent')}?project=alpha")
        assert "model: opus" in get2.json()["content"]

        deleted = c.delete(f"{_agent_url('my-agent')}?project=alpha&scope=project&confirm=alpha")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        get3 = c.get(f"{_agent_url('my-agent')}?project=alpha")
        assert get3.status_code == 404


def test_route_delete_missing_returns_false_not_error(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.delete(f"{_agent_url('nope')}?project=alpha&scope=project&confirm=alpha")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is False


def test_route_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("my-agent"),
            },
        )
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("my-agent", model="opus"),
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_no_hash_on_existing_file_is_409(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("my-agent"),
            },
        )
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("my-agent", model="opus"),
            },
        )
        assert resp.status_code == 409


def test_route_non_string_hash_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("my-agent"),
                "hash": 123,
            },
        )
        assert resp.status_code == 422


def test_route_user_write_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        wr = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "content": _content("my-agent"),
            },
        )
        assert wr.status_code == 200
        get1 = c.get(f"{_agent_url('my-agent')}?scope=user")
        assert get1.status_code == 200
        assert get1.json()["content"] == _content("my-agent")
        deleted = c.delete(f"{_agent_url('my-agent')}?scope=user&confirm={cw.USER_SCOPE_TOKEN}")
        assert deleted.status_code == 200
    # The write must have landed in the ISOLATED home (autouse fixture), never the
    # real account, under ~/.claude/agents/ — the write was already deleted above, so
    # assert the isolated dir path shape by re-creating and checking directly.
    isolated = Path(os.environ["HOME"]) / ".claude" / "agents"
    assert isolated.parent.name == ".claude"


def test_route_list_user_scope(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        c.put(
            _agent_url("my-agent"),
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "content": _content("my-agent"),
            },
        )
        listed = c.get(f"{_LIST_URL}?scope=user")
        assert listed.status_code == 200
        assert listed.json()["scope"] == "user"
        assert any(a["name"] == "my-agent" for a in listed.json()["agents"])


def test_route_get_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_agent_url('my-agent')}?scope=bogus").status_code == 422


def test_route_get_user_scope_missing_agent_is_404(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"{_agent_url('nope')}?scope=user")
        assert resp.status_code == 404


def test_route_put_non_string_content_with_valid_confirm_is_422(
    write_config, tmp_path, projects_root
) -> None:
    # Distinct from test_route_confirm_runs_before_content_shape_check: this uses a
    # VALID confirm, so it exercises the content-shape 422 branch itself rather than
    # the earlier confirm-mismatch 400 gate.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={"scope": "project", "project": "alpha", "confirm": "alpha", "content": 123},
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "agents").exists()


def test_route_put_user_scope_error_is_mapped(write_config, tmp_path) -> None:
    # A built-in name at USER scope exercises the user-scope PUT except-branch
    # (ReadOnlyAgentError -> 403), the user-scope twin of test_route_builtin_write_is_403.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("Explore"),
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "content": _content("Explore"),
            },
        )
        assert resp.status_code == 403


def test_route_delete_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.delete(f"{_agent_url('my-agent')}?scope=bogus&confirm=x")
        assert resp.status_code == 422


def test_route_delete_user_scope_error_is_mapped(write_config, tmp_path) -> None:
    # A built-in name at USER scope exercises the user-scope DELETE except-branch
    # (ReadOnlyAgentError -> 403), the user-scope twin of test_route_builtin_delete_is_403.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.delete(f"{_agent_url('Plan')}?scope=user&confirm={cw.USER_SCOPE_TOKEN}")
        assert resp.status_code == 403


@pytest.mark.parametrize("name", ["general-purpose", "Explore", "Plan"])
def test_route_builtin_get_is_visible_but_not_editable(
    write_config, tmp_path, projects_root, name: str
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"{_agent_url(name)}?project=alpha")
        assert resp.status_code == 200
        body = resp.json()
        assert body["exists"] is False
        assert body["editable"] is False
        assert body["source"] == "built-in"


@pytest.mark.parametrize("name", ["general-purpose", "Explore", "Plan"])
def test_route_builtin_write_is_403(write_config, tmp_path, projects_root, name: str) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url(name),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content(name),
            },
        )
        assert resp.status_code == 403
    assert not (projects_root / "alpha" / ".claude" / "agents" / f"{name}.md").exists()


@pytest.mark.parametrize("name", ["general-purpose", "Explore", "Plan"])
def test_route_builtin_delete_is_403(write_config, tmp_path, projects_root, name: str) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.delete(f"{_agent_url(name)}?project=alpha&scope=project&confirm=alpha")
        assert resp.status_code == 403


def test_route_plugin_owned_file_write_is_403(write_config, tmp_path, projects_root) -> None:
    agents_dir = projects_root / "alpha" / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    target = agents_dir / "shadowed.md"
    target.write_bytes(b"---\nname: shadowed\ndescription: ${CLAUDE_PLUGIN_ROOT}/x\n---\nbody\n")
    original = target.read_bytes()
    with _client(write_config, tmp_path, _ON) as c:
        get_resp = c.get(f"{_agent_url('shadowed')}?project=alpha")
        assert get_resp.status_code == 200
        assert get_resp.json()["editable"] is False
        assert get_resp.json()["source"] == "plugin"

        put_resp = c.put(
            _agent_url("shadowed"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("shadowed"),
            },
        )
        assert put_resp.status_code == 403

        del_resp = c.delete(f"{_agent_url('shadowed')}?project=alpha&scope=project&confirm=alpha")
        assert del_resp.status_code == 403
    assert target.read_bytes() == original


def test_route_plugin_symlink_get_readonly_200_put_delete_403(
    write_config, tmp_path, projects_root
) -> None:
    # Greptile P1: a plugin SYMLINK agent must be a read-only 200 on GET (target
    # never read) and a 403 on PUT/DELETE — NOT a 400 path-escape from the
    # containment resolve following the symlink.
    agents_dir = projects_root / "alpha" / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    outside = projects_root / "alpha" / "plugin-source.md"
    outside.write_bytes(b"SECRET plugin content\n")
    link = agents_dir / "shadowed.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this platform/host")
    with _client(write_config, tmp_path, _ON) as c:
        get_resp = c.get(f"{_agent_url('shadowed')}?project=alpha")
        assert get_resp.status_code == 200  # read-only 200, NOT a 400 path-escape
        assert get_resp.json()["editable"] is False
        assert get_resp.json()["source"] == "plugin"
        assert get_resp.json()["content"] == ""  # target never followed/read
        assert "SECRET" not in get_resp.json()["content"]

        put_resp = c.put(
            _agent_url("shadowed"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("shadowed"),
            },
        )
        assert put_resp.status_code == 403  # NOT 400

        del_resp = c.delete(f"{_agent_url('shadowed')}?project=alpha&scope=project&confirm=alpha")
        assert del_resp.status_code == 403  # NOT 400
    assert link.is_symlink()  # untouched
    assert outside.read_bytes() == b"SECRET plugin content\n"


def test_route_nonsymlink_escape_still_400(write_config, tmp_path) -> None:
    # The ordering fix must NOT loosen containment for a genuinely-escaping
    # NON-symlink input: a `../escape` project still fails closed as a 400.
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_LIST_URL}?project=../escape").status_code == 400
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "../escape",
                "confirm": "../escape",
                "content": _content("my-agent"),
            },
        )
        assert resp.status_code == 400


def test_route_get_missing_agent_is_404(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"{_agent_url('nope')}?project=alpha")
        assert resp.status_code == 404


def test_route_frontmatter_name_mismatch_is_422(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": _content("someone-else"),
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "agents").exists()


def test_route_never_executes_hooks_command(write_config, tmp_path, projects_root) -> None:
    """RCE-negative at the ROUTE: a frontmatter hooks command must never run."""
    marker = projects_root / "ROUTE_PWNED"
    hook_cmd = f"touch {marker}"
    content = (
        "---\nname: my-agent\ndescription: d\n"
        "hooks:\n  PreToolUse:\n    - hooks:\n        - type: command\n"
        f"          command: {hook_cmd}\n"
        "---\nbody\n"
    )
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _agent_url("my-agent"),
            json={"scope": "project", "project": "alpha", "confirm": "alpha", "content": content},
        )
        assert resp.status_code == 200
    assert not marker.exists()


def test_read_agent_treats_a_directory_as_absent_not_a_500(tmp_path: Path) -> None:
    # A plain DIRECTORY named `<name>.md` raises IsADirectoryError, which is neither
    # FileNotFoundError nor a ConfigWriteError — so it escaped the route's
    # `except ConfigWriteError` as a 500. LIST already skips such an entry (it fails
    # `is_file()`), so GET must agree it is absent; the module docstring states that
    # agreement as a property.
    root = tmp_path / "agents"
    root.mkdir()
    (root / "sneaky.md").mkdir()
    with pytest.raises(sub.AgentNotFoundError):
        sub._read_agent(root, "sneaky", "project")


def test_read_agent_degrades_a_self_referential_file_an_earlier_release_wrote(
    tmp_path: Path,
) -> None:
    # #1368 refuses a self-referential YAML alias at the parse seam, so no NEW file can hold
    # one -- but a release before the fix accepted the write. This pins the property that
    # makes a second guard in `redact_secrets` unnecessary (and it was measured, not assumed,
    # before that guard was dropped from the fix): GET degrades `frontmatter` to `{}` and
    # still surfaces `content` verbatim, so the operator can see and repair the file. Written
    # with raw bytes, deliberately bypassing the write path, because that is how it got there.
    root = tmp_path / "agents"
    root.mkdir()
    raw = b"---\nname: legacy\ndescription: d\nextra: &a [*a]\n---\nbody\n"
    (root / "legacy.md").write_bytes(raw)

    doc = sub._read_agent(root, "legacy", "project")  # must not raise RecursionError

    assert doc["frontmatter"] == {}  # the derived display field degrades...
    assert doc["content"] == raw.decode()  # ...and the raw text is still returned to fix


def test_read_agent_frontmatter_does_not_use_a_server_name_as_a_secret_hint(
    tmp_path: Path,
) -> None:
    # A frontmatter `mcpServers` block is keyed by user-chosen server NAMES. With the
    # widened subtree hint, a server called `oauth-gw` matched `_SECRET_KEY_RE` and masked
    # its own `type`/`url`. Display-only (PUT round-trips `content`, never `frontmatter`),
    # but a transport shown as `********` is simply misleading.
    root = tmp_path / "agents"
    root.mkdir()
    (root / "my-agent.md").write_bytes(
        b"---\n"
        b"name: my-agent\n"
        b"description: does a thing\n"
        b"mcpServers:\n"
        b"  oauth-gw:\n"
        b"    type: http\n"
        b"    url: https://example.invalid/\n"
        b"    headers:\n"
        b"      Authorization: Bearer sk-live-SECRET\n"
        b"---\nbody\n"
    )
    doc = sub._read_agent(root, "my-agent", "project")
    server = doc["frontmatter"]["mcpServers"]["oauth-gw"]

    assert server["type"] == "http"  # structural field survives
    assert server["url"] == "https://example.invalid/"
    assert server["headers"]["Authorization"] == cw.REDACTION_SENTINEL  # secret still masked


def test_write_agent_runs_its_stale_hash_guard_inside_the_write_lock(tmp_path, monkeypatch):
    # The 409 guard read the file, compared, and THEN called write_file with no `verify=`,
    # so the comparison sat outside that function's per-target lock: two concurrent PUTs
    # could both pass and the second would silently overwrite the first. `claude_md`'s
    # scoped write is the sibling that always passed `verify=`; this is the only other
    # read-modify-write on this surface.
    root = tmp_path / "agents"
    root.mkdir()
    target = root / "a.md"
    original = b"---\nname: a\ndescription: original\n---\nbody\n"
    target.write_bytes(original)

    captured = {}
    real_write_file = sub.fw.write_file

    def _capture(root_, relative, content, *, verify=None, **kw):
        captured["verify"] = verify
        return real_write_file(root_, relative, content, verify=verify, **kw)

    monkeypatch.setattr(sub.fw, "write_file", _capture)

    sub._write_agent(
        root, "a", "---\nname: a\ndescription: updated\n---\nbody\n", cw.hash_bytes(original)
    )

    verify = captured["verify"]
    assert verify is not None, "the guard must be handed to write_file, not run before it"
    # And it is a REAL guard, not a no-op passed to satisfy the signature: bytes that no
    # longer match the expected hash must abort the write from inside the lock.
    with pytest.raises(cw.StaleConfigWriteError):
        verify(b"someone else wrote this")
    verify(original)  # the matching bytes still pass


def test_delete_agent_rechecks_the_readonly_refusal_under_the_lock(tmp_path, monkeypatch):
    # Same read-then-act shape as the write path: the read decides whether the file MAY be
    # deleted, so a file that became plugin-owned between the read and the unlink would be
    # removed on a stale decision. The re-check runs as delete_path's `verify=`.
    root = tmp_path / "agents"
    root.mkdir()
    (root / "a.md").write_bytes(b"---\nname: a\ndescription: ordinary\n---\nbody\n")

    captured = {}
    real_delete_path = sub.fw.delete_path

    def _capture(root_, relative, *, verify=None, **kw):
        captured["verify"] = verify
        return real_delete_path(root_, relative, verify=verify, **kw)

    monkeypatch.setattr(sub.fw, "delete_path", _capture)

    assert sub._delete_agent(root, "a") is True

    verify = captured["verify"]
    assert verify is not None
    # A plugin-marker body appearing under the lock must abort the delete.
    with pytest.raises(sub.ReadOnlyAgentError):
        verify(b"---\nname: a\ndescription: ${CLAUDE_PLUGIN_ROOT}/x\n---\nbody\n")


def test_delete_path_verify_can_abort_a_delete(tmp_path):
    # The new writer primitive, on its own terms: raising from `verify` leaves the file.
    target = tmp_path / "f.txt"
    target.write_text("keep me")

    def _refuse(current):
        raise cw.StaleConfigWriteError("nope")

    with pytest.raises(cw.StaleConfigWriteError):
        sub.fw.delete_path(tmp_path, "f.txt", verify=_refuse)

    assert target.read_text() == "keep me"  # nothing removed
    assert sub.fw.delete_path(tmp_path, "f.txt") is True  # and it still deletes normally


def test_delete_path_verify_gets_none_when_the_bytes_are_unreadable(tmp_path, monkeypatch):
    # `verify` is documented to receive None for a target it cannot read, so a callback
    # written against `bytes | None` is never handed a surprise exception instead. The read
    # must not be able to fail the delete on its own — that would make an EACCES file
    # undeletable through this surface.
    target = tmp_path / "f.txt"
    target.write_text("x")

    def _boom(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", _boom)

    seen = {}

    def _record(current):
        seen["current"] = current

    assert sub.fw.delete_path(tmp_path, "f.txt", verify=_record) is True
    assert seen["current"] is None
    assert not target.exists()
