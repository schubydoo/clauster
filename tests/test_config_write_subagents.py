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

import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml
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
        # Nested: the CLI retry would re-quote a top-level `[` line (TASK-449.6).
        sub.parse_frontmatter("---\nm:\n  name: [unterminated\n---\nbody\n")


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


def test_is_valid_agent_name_length_cap() -> None:
    # The name is also the filename, so it stops short of Claude Code's documented 256:
    # the atomic writer's `<name>.md.<8 chars>.tmp` temp file must fit in 255 bytes.
    assert sub.MAX_NAME_CHARS == 239
    assert sub.is_valid_agent_name("a" * sub.MAX_NAME_CHARS)
    assert not sub.is_valid_agent_name("a" * (sub.MAX_NAME_CHARS + 1))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the 260-character MAX_PATH limit for the WHOLE path, not NAME_MAX, binds first",
)
def test_longest_name_writes_and_reads(tmp_path: Path) -> None:
    # Positive control for the cap: the longest accepted name really fits on disk,
    # temp file included (a cap of 252 failed here with ENAMETOOLONG on the mkstemp).
    name = "a" * sub.MAX_NAME_CHARS
    sub.write_project_agent(tmp_path, name, _content(name), None)
    assert sub.read_project_agent(tmp_path, name)["exists"] is True


@pytest.mark.parametrize("op", ["read", "write", "delete"])
def test_over_long_name_is_a_typed_error_not_an_oserror(tmp_path: Path, op: str) -> None:
    # Before the cap, a 300-character name reached the filesystem and raised a bare
    # OSError (ENAMETOOLONG), which escaped the route's ConfigWriteError mapper as a 500.
    name = "a" * 300
    with pytest.raises(cw.PathEscapeError):
        if op == "read":
            sub.read_project_agent(tmp_path, name)
        elif op == "write":
            sub.write_project_agent(tmp_path, name, _content(name), None)
        else:
            sub.delete_project_agent(tmp_path, name)


def test_builtin_agent_names_match_the_documented_list() -> None:
    # AC: BUILTIN_AGENT_NAMES matches current Claude Code. The "Built-in subagents" section
    # of https://code.claude.com/docs/en/sub-agents lists Explore, Plan, general-purpose
    # and, under "Other", claude, statusline-setup and claude-code-guide.
    assert sub.BUILTIN_AGENT_NAMES == {
        "general-purpose",
        "explore",
        "plan",
        "claude",
        "statusline-setup",
        "claude-code-guide",
    }


@pytest.mark.parametrize(
    "name",
    [
        "general-purpose",
        "Explore",
        "Plan",
        "PLAN",
        "explore",
        "claude",
        "Claude",
        "statusline-setup",
        "claude-code-guide",
    ],
)
def test_is_builtin_agent_matches_case_insensitively(name: str) -> None:
    assert sub.is_builtin_agent(name)


@pytest.mark.parametrize("name", ["claude", "statusline-setup", "claude-code-guide"])
def test_new_builtin_names_are_read_only(tmp_path: Path, name: str) -> None:
    doc = sub.read_project_agent(tmp_path, name)
    assert (doc["source"], doc["editable"], doc["exists"]) == ("built-in", False, False)
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.write_project_agent(tmp_path, name, _content(name), None)
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.delete_project_agent(tmp_path, name)
    assert not sub.project_agents_dir(tmp_path).exists()


@pytest.mark.parametrize("name", ["claude", "explore"])
def test_on_disk_override_of_a_builtin_is_readable_but_not_editable(
    tmp_path: Path, name: str
) -> None:
    # Claude Code lets a user/project file override a built-in of the same name (docs:
    # "A user or project subagent named `Explore` overrides the built-in"). Adding `claude`
    # to the built-ins must not hide an existing claude.md: LIST and GET both show the
    # file, both as read-only, matching PUT/DELETE, which still refuse the name.
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    content = _content(name)
    (agents_dir / f"{name}.md").write_text(content, encoding="utf-8", newline="\n")

    rows = [r for r in sub.list_project_agents(tmp_path) if r["name"] == name]
    assert {(r["source"], r["editable"]) for r in rows} == {
        ("project", False),
        ("built-in", False),
    }
    doc = sub.read_project_agent(tmp_path, name)
    assert (doc["source"], doc["editable"], doc["exists"]) == ("project", False, True)
    assert doc["content"] == content
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.write_project_agent(tmp_path, name, content, doc["hash"])
    with pytest.raises(sub.ReadOnlyAgentError):
        sub.delete_project_agent(tmp_path, name)
    assert (agents_dir / f"{name}.md").read_text(encoding="utf-8") == content


def test_symlink_named_like_a_builtin_still_reads_as_the_builtin(tmp_path: Path) -> None:
    # The override lookup must not follow a link: a symlinked claude.md keeps the
    # content-less synthetic doc, and its target is never read.
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text(_content("claude"), encoding="utf-8")
    try:
        (agents_dir / "claude.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this host")
    doc = sub.read_project_agent(tmp_path, "claude")
    assert (doc["source"], doc["exists"], doc["content"]) == ("built-in", False, "")


@pytest.mark.parametrize(
    "name",
    ["my-agent", "general-purposeish", "explorer", "claude-helper", "comment-thread-analyst"],
)
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
            "mcpServers": [{"srv": {"command": "x"}}, "github"],
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
            "maxTurns": 5,
            "autoCompactWindow": 200000,
            "skills": ["skill-one"],
            "initialPrompt": "start here",
            "memory": "project",
            "effort": "high",
            "background": True,
            "omitClaudeMd": True,
            "isolation": "worktree",
            "color": "blue",
            "experimental": {"cacheTtl": "1h"},
        }
    )  # no raise


# Every documented value of every closed-list field (TASK-449.5), from the "Frontmatter
# reference" table of https://code.claude.com/docs/en/sub-agents. One case per value, so a
# value dropped from a rule fails by name.
_DOCUMENTED_VALUES: list[tuple[str, object]] = [
    *[("model", m) for m in ("sonnet", "opus", "haiku", "fable", "inherit", "claude-opus-5-5")],
    *[
        ("permissionMode", m)
        for m in ("default", "acceptEdits", "auto", "dontAsk", "plan", "manual")
    ],
    *[("memory", m) for m in ("user", "project", "local")],
    *[("effort", e) for e in ("low", "medium", "high", "xhigh", "max")],
    ("isolation", "worktree"),
    *[
        ("color", c)
        for c in ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")
    ],
    *[("autoCompactWindow", n) for n in (100000, 200000, 1000000)],
    *[("omitClaudeMd", b) for b in (True, False)],
    *[("background", b) for b in (True, False)],
    *[("experimental", {"cacheTtl": t}) for t in ("5m", "1h")],
    ("experimental", {}),
    # Only `cacheTtl` is documented inside the map; other keys pass through, like unknown
    # top-level keys do.
    ("experimental", {"futureOption": 1}),
    ("experimental", {"cache_ttl": "5m", 7: "non-string key"}),
    ("mcpServers", ["github"]),
    ("mcpServers", [{"playwright": {"type": "stdio", "command": "npx", "args": ["-y", "x"]}}]),
    ("mcpServers", [{"remote": {"type": "ws", "url": "wss://example.com"}}, "slack"]),
    ("mcpServers", []),
    ("tools", ["Read", "Grep"]),
    ("tools", "Agent(worker, researcher), Read, Bash"),
]

# Values the installed CLI (2.1.318) reads although the docs do not list them. The
# maintainer's call on TASK-449.5: anything the CLI parses is accepted. One case per form,
# next to what the CLI reads it as.
_CLI_PARSED_VALUES: list[tuple[str, object]] = [
    ("effort", 3),  # an integer level
    ("effort", 0),
    ("effort", "High"),  # level names are case-folded
    ("effort", "MAX"),
    ("effort", "med"),  # the CLI's alias for medium
    ("effort", "3 tokens"),  # parseInt: 3
    ("effort", 2.5),  # stringified, then parseInt: 2
    ("isolation", "remote"),
    ("maxTurns", "5"),
    ("maxTurns", " 7 "),
    ("maxTurns", "5 turns"),  # parseInt: 5
    ("maxTurns", "2e1"),  # exponent notation: 20
    ("maxTurns", "1,000"),  # a grouped integer: 1000
    ("maxTurns", 5.0),  # a whole number
    ("autoCompactWindow", "200000"),
    ("autoCompactWindow", "200,000"),
    ("autoCompactWindow", "2e5"),
    ("autoCompactWindow", 200000.0),
    ("background", "true"),
    ("background", "false"),
    ("omitClaudeMd", "true"),
    ("omitClaudeMd", "false"),
    ("skills", "api-conventions, error-handling"),  # split on commas, like `tools`
    ("skills", ""),  # no skills
    ("skills", None),
    ("disallowedTools", ""),  # deny nothing
    ("disallowedTools", []),
    ("disallowedTools", None),
    ("description", "   "),  # the CLI loads a whitespace-only description
]


def test_tool_lists_split_like_the_cli() -> None:
    # `tools: "*"` means every tool; a `*` inside a specifier is not a bare `*` entry.
    sub.validate_frontmatter({"name": "x", "description": "d", "tools": "*"})
    sub.validate_frontmatter({"name": "x", "description": "d", "disallowedTools": "Bash(rm *)"})
    # A tab right after a separator is skipped (only a space ends an entry).
    assert sub._cli_tool_entries(["Read,\tGrep"]) == ["Read", "Grep"]
    assert sub._cli_tool_entries(["Agent(worker, researcher), Read  Bash", ""]) == [
        "Agent(worker, researcher)",
        "Read",
        "Bash",
    ]


@pytest.mark.parametrize("spelling", ["yes", "no", "on", "off", "Yes", "OFF", "y", "n"])
@pytest.mark.parametrize("key", ["background", "omitClaudeMd"])
def test_yaml11_only_booleans_are_refused(key: str, spelling: str) -> None:
    # PyYAML reads a plain `yes` as True; the CLI's YAML 1.2 loader reads the string
    # "yes", which `background` logs as invalid and `omitClaudeMd` ignores.
    text = f"---\nname: x\ndescription: d\n{key}: {spelling}\n---\nbody\n"
    loaded = sub.parse_frontmatter(text)[0][key]
    if isinstance(loaded, bool):  # `y`/`n` are strings even to PyYAML
        with pytest.raises(cw.InvalidCandidateError, match="Write true or false"):
            sub.validate_agent_content(text, expected_name="x")
    else:
        with pytest.raises(cw.InvalidCandidateError, match="must be true or false"):
            sub.validate_agent_content(text, expected_name="x")


@pytest.mark.parametrize("spelling", ["true", "True", "FALSE", "'true'", '"false"'])
def test_yaml12_booleans_and_quoted_strings_still_save(spelling: str) -> None:
    text = f"---\nname: x\ndescription: d\nbackground: {spelling}\nomitClaudeMd: {spelling}\n"
    sub.validate_agent_content(text + "---\nbody\n", expected_name="x")  # no raise


@pytest.mark.parametrize(
    "header",
    [
        # Through a merge key: compose does not flatten merges.
        "<<: {background: yes}",
        "base: &d {background: on}\n<<: *d",
        "a: &a {omitClaudeMd: yes}\n<<: [*a]",
        # Quoted, with an explicit tag: still `yes` to YAML 1.2.
        'background: !!bool "yes"',
        "omitClaudeMd: !!bool 'on'",
        # YAML 1.1-only number spellings: another number, or a string, to YAML 1.2.
        "autoCompactWindow: 02000000",  # 524288 here, 2000000 (out of range) there
        "maxTurns: 010",
        "maxTurns: 0b101",
        "maxTurns: 0_5",
        "maxTurns: 1_000",  # 1000 here, the string "1_000" (parseInt: 1) there
        # An alias to an enclosing node: PyYAML loads it, the CLI's YAML 1.2 refuses it and
        # falls back to re-quoting values, so `plan # c` would arrive as one string.
        "a: &a {<<: *a}\npermissionMode: plan # read only",
        "a: &a {<<: *a}\n<<: *a",
        "a: &a {<<: [*a]}\npermissionMode: plan # c",  # the same, through a merge list
        "maxTurns: 1:30",
        "effort: 0b11",
        "maxTurns: 5.0_0",
    ],
)
def test_yaml11_only_spellings_are_refused(header: str) -> None:
    text = f"---\nname: x\ndescription: d\n{header}\n---\nbody\n"
    with pytest.raises(cw.InvalidCandidateError, match="YAML 1.2|true or false"):
        sub.validate_agent_content(text, expected_name="x")


@pytest.mark.parametrize(
    "header",
    [
        "maxTurns: 5\nautoCompactWindow: 200000\neffort: 3",
        "maxTurns: 0x10",  # hex: the same number to both versions
        "maxTurns: 5.0\neffort: -2",
        "<<: {maxTurns: 5, background: true}",
        "a: &a {maxTurns: 5}\n<<: [*a, *a]",  # the same source twice is checked once
        "maxTurns: '010'",  # quoted: a string to both, which the CLI's parseInt reads as 10
    ],
)
def test_yaml12_number_spellings_still_save(header: str) -> None:
    text = f"---\nname: x\ndescription: d\n{header}\n---\nbody\n"
    sub.validate_agent_content(text, expected_name="x")  # no raise


@pytest.mark.parametrize("char", ["\x85", " ", " "])
def test_yaml11_only_line_breaks_are_refused_and_never_converted(
    tmp_path: Path, char: str
) -> None:
    # `tools: Read # c<LS>disallowedTools: Bash` is two keys to PyYAML and one to the
    # CLI's YAML 1.2, which then never sees the denylist.
    text = f"---\nname: x\ndescription: d\ntools: Read # c{char}disallowedTools: Bash\n---\nb\n"
    with pytest.raises(cw.InvalidCandidateError, match="line break"):
        sub.validate_agent_content(text, expected_name="x")
    mapped = f"---\nname: x\ndescription: d{char}\nmcpServers:\n  srv:\n    command: x\n---\nb\n"
    assert sub._mcp_servers_as_list(mapped) is None


@pytest.mark.parametrize(
    "candidate",
    [
        {"name": "x", "description": "d", "disallowedTools": "﻿*"},
        {"name": "x", "description": "d", "disallowedTools": "*﻿"},
        {"name": "x", "description": "d", "disallowedTools": ["Bash", "﻿*"]},
        {"name": "x", "description": "d", "tools": "﻿"},
        {"name": "x", "description": "d", "tools": ["﻿"]},
        {"name": "x", "description": "d", "disallowedTools": ["﻿"]},  # the CLI drops it
    ],
)
def test_tool_lists_trim_like_javascript(candidate: dict) -> None:
    # JavaScript's trim() removes a BOM where Python's strip() keeps it, so a BOM-wrapped
    # `*` is a bare `*` to the CLI, and a BOM alone is no tool at all.
    with pytest.raises(cw.InvalidCandidateError):
        sub.validate_frontmatter(candidate)


@pytest.mark.parametrize(("key", "value"), _CLI_PARSED_VALUES)
def test_validate_frontmatter_accepts_every_cli_parsed_value(key: str, value: object) -> None:
    sub.validate_frontmatter({"name": "x", "description": "d", key: value})  # no raise


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5", 5),
        ("5 turns", 5),
        ("2e1", 20),
        ("1,000", 1000),
        ("1" * 40, int("1" * 40)),  # over 32 chars: straight to parseInt
        # Over 32 chars, so the grouped-integer pattern is skipped and parseInt stops at
        # the first comma: 1, not 10**27.
        ("1,000,000,000,000,000,000,000,000,000", 1),
        ("1" * 400, None),  # past the double range: Infinity in JavaScript
        (10**400, None),
        ("٥", None),  # an Arabic-Indic five: JavaScript reads ASCII digits only
        ("５", None),  # a fullwidth five
        ("٥e3", None),  # Python's float() reads 5000; the exponent pattern is ASCII-only
        ("１,０００", None),  # fullwidth digits in the grouped form
        ("\x85 5", None),  # NEL is whitespace to Python, not to JavaScript
        ("﻿5", 5),  # the BOM is whitespace to JavaScript, not to Python
        ("200_000", 200000),
        ("1\u00a0000", 1000),
        (5.0, 5),
        (5.5, None),
        (True, None),
        ("x", None),
        ("-3", None),
        ("1e400", None),
        (float("nan"), None),
        ([5], None),
    ],
)
def test_cli_positive_int_mirrors_the_cli(value: object, expected: int | None) -> None:
    assert sub._cli_positive_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("High", "high"),
        ("MED", "medium"),
        (" high", None),
        ("3 tokens", 3),
        (-2, -2),
        (2.5, 2),
        (2.0, 2),
        (float("inf"), None),
        (10**400, None),
        ("1" * 400, None),
        ("٥", None),
        (True, None),
        ("", None),
        ({"a": 1}, None),
    ],
)
def test_cli_effort_mirrors_the_cli(value: object, expected: object) -> None:
    assert sub._cli_effort(value) == expected


@pytest.mark.parametrize(("key", "value"), _DOCUMENTED_VALUES)
def test_validate_frontmatter_accepts_every_documented_value(key: str, value: object) -> None:
    sub.validate_frontmatter({"name": "x", "description": "d", key: value})  # no raise


def test_documented_values_save_through_the_write_path(tmp_path: Path) -> None:
    # AC: every documented field SAVES with its documented type. The cases above go through
    # the validator; this writes one file carrying all the new fields as YAML text, so the
    # YAML spelling an operator types (a bare int, a nested map, a list of mixed entries)
    # is the thing that is checked.
    content = (
        "---\nname: my-agent\ndescription: d\n"
        "autoCompactWindow: 200000\nomitClaudeMd: true\nmemory: local\neffort: xhigh\n"
        "isolation: worktree\ncolor: cyan\npermissionMode: manual\nmodel: fable\n"
        "experimental:\n  cacheTtl: 5m\n"
        "mcpServers:\n  - github\n  - playwright:\n      type: stdio\n      command: npx\n"
        "---\nbody\n"
    )
    sub.write_project_agent(tmp_path, "my-agent", content, None)
    on_disk = (sub.project_agents_dir(tmp_path) / "my-agent.md").read_bytes()
    assert on_disk == content.encode("utf-8")


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
        {"name": "x", "description": "d", "skills": 5},
        {"name": "x", "description": "d", "skills": {"a": 1}},
        {"name": "x", "description": "d", "disallowedTools": 5},
        {"name": "x", "description": "d", "tools": None},  # the CLI reads zero tools
        {"name": "x", "description": "d", "tools": ","},  # splits to no entry at all
        {"name": "x", "description": "d", "tools": " , "},
        # `*` in the denylist switches the denylist OFF in the CLI.
        {"name": "x", "description": "d", "disallowedTools": "*"},
        {"name": "x", "description": "d", "disallowedTools": "Write, *"},
        {"name": "x", "description": "d", "disallowedTools": ["Bash", "*"]},
        {"name": "x", "description": "d", "skills": [1]},
        {"name": "x", "description": "d", "model": ""},
        {"name": "x", "description": "d", "model": 5},
        {"name": "x", "description": "d", "permissionMode": 5},
        {"name": "x", "description": "d", "permissionMode": "not-a-real-mode"},
        {"name": "x", "description": "d", "mcpServers": "nope"},
        {"name": "x", "description": "d", "mcpServers": {"srv": "not-an-object"}},
        # The mapping spelling: Claude Code ignores a non-list `mcpServers` entirely.
        {"name": "x", "description": "d", "mcpServers": {"srv": {"command": "x"}}},
        {"name": "x", "description": "d", "mcpServers": [""]},  # blank server name
        {"name": "x", "description": "d", "mcpServers": [5]},  # neither name nor map
        {"name": "x", "description": "d", "mcpServers": [{}]},  # empty inline entry
        {"name": "x", "description": "d", "mcpServers": [{"srv": "not-an-object"}]},
        {"name": "x", "description": "d", "mcpServers": [{"": {"command": "x"}}]},
        {"name": "x", "description": "d", "mcpServers": [{1: {"command": "x"}}]},
        {"name": "x", "description": "d", "permissionMode": "Manual"},  # case matters
        {"name": "x", "description": "d", "hooks": {"NotAnEvent": []}},
        {"name": "x", "description": "d", "maxTurns": 0},
        {"name": "x", "description": "d", "maxTurns": -1},
        {"name": "x", "description": "d", "maxTurns": True},
        {"name": "x", "description": "d", "maxTurns": "turns"},  # parseInt gives NaN
        {"name": "x", "description": "d", "maxTurns": "0"},
        {"name": "x", "description": "d", "maxTurns": 5.5},  # a number must be whole
        {"name": "x", "description": "d", "maxTurns": [5]},  # only via JS object coercion
        {"name": "x", "description": "d", "maxTurns": "1e400"},  # Infinity is not whole
        {"name": "x", "description": "d", "initialPrompt": 5},
        {"name": "x", "description": "d", "memory": 5},
        # `memory` is a closed list now; a free string or a mapping was accepted before
        # and Claude Code ignores both.
        {"name": "x", "description": "d", "memory": "some memory"},
        {"name": "x", "description": "d", "memory": {"k": "v"}},
        {"name": "x", "description": "d", "effort": ""},
        {"name": "x", "description": "d", "effort": "extreme"},
        {"name": "x", "description": "d", "effort": " high"},  # the CLI does not trim it
        {"name": "x", "description": "d", "effort": True},
        {"name": "x", "description": "d", "effort": None},
        {"name": "x", "description": "d", "effort": ["high"]},
        {"name": "x", "description": "d", "background": "yes"},
        {"name": "x", "description": "d", "background": "True"},  # the CLI compares exactly
        {"name": "x", "description": "d", "background": 1},
        {"name": "x", "description": "d", "omitClaudeMd": "yes"},
        {"name": "x", "description": "d", "isolation": ""},
        {"name": "x", "description": "d", "isolation": "Worktree"},
        {"name": "x", "description": "d", "isolation": "container"},
        {"name": "x", "description": "d", "color": ""},
        {"name": "x", "description": "d", "color": "magenta"},
        {"name": "x", "description": "d", "color": 5},
        {"name": "x", "description": "d", "autoCompactWindow": 99999},  # below the range
        {"name": "x", "description": "d", "autoCompactWindow": 1000001},  # above the range
        {"name": "x", "description": "d", "autoCompactWindow": "200k"},  # CLI reads 200
        {"name": "x", "description": "d", "autoCompactWindow": 200000.5},
        {"name": "x", "description": "d", "autoCompactWindow": True},
        {"name": "x", "description": "d", "experimental": "1h"},  # not a map
        {"name": "x", "description": "d", "experimental": {"cacheTtl": "2h"}},
        {"name": "x", "description": "d", "experimental": {"cacheTtl": 5}},
        # Claude Code matches the key with `-`/`_` dropped and case folded, then ignores a
        # bad value, so these spellings get the same closed-list check.
        {"name": "x", "description": "d", "experimental": {"cachettl": "2h"}},
        {"name": "x", "description": "d", "experimental": {"CACHE_TTL": "bogus"}},
        {"name": "x", "description": "d", "experimental": {"cache-ttl": "1d"}},
        {"name": "x" * (sub.MAX_NAME_CHARS + 1), "description": "d"},  # filename too long
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
    # The mapping spelling of `mcpServers` is now refused on WRITE (Claude Code ignores it),
    # but a file already on disk in that spelling must still read with its secrets masked,
    # so this one is placed on disk directly.
    content = (
        "---\nname: my-agent\ndescription: d\n"
        "mcpServers:\n  srv:\n    headers:\n      Authorization: Bearer super-secret\n"
        "---\nbody\n"
    )
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True)
    (agents_dir / "my-agent.md").write_text(content, encoding="utf-8", newline="\n")
    doc = sub.read_project_agent(tmp_path, "my-agent")
    # the raw content round-trips unredacted (needed for a correct re-save)...
    assert "super-secret" in doc["content"]
    # ...but the derived frontmatter display field masks the secret-shaped value.
    assert (
        doc["frontmatter"]["mcpServers"]["srv"]["headers"]["Authorization"]
        == cw.REDACTION_SENTINEL
    )


def test_read_redacts_secrets_in_the_documented_list_form(tmp_path: Path) -> None:
    # The list spelling is the one this surface now writes. Its display field must mask an
    # inline server's secret exactly as the mapping spelling does, while `content` still
    # round-trips raw for the re-save.
    content = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  - github\n"
        "  - srv:\n      type: http\n      url: https://example.com/mcp\n"
        "      headers:\n        Authorization: Bearer super-secret\n"
        "---\nbody\n"
    )
    sub.write_project_agent(tmp_path, "my-agent", content, None)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert "super-secret" in doc["content"]
    servers = doc["frontmatter"]["mcpServers"]
    assert servers[0] == "github"
    assert servers[1]["srv"]["headers"]["Authorization"] == cw.REDACTION_SENTINEL
    assert "super-secret" not in json.dumps(doc["frontmatter"])


_MAP_FORM = (
    "---\nname: my-agent\ndescription: d\n"
    "mcpServers:\n"
    "  # inline, scoped to this agent\n"
    '  playwright:\n    type: stdio\n    command: npx\n    args: ["-y", "@playwright/mcp"]\n'
    '  remote: {type: http, url: "https://example.com/mcp"}\n'
    "model: opus\n"
    "---\nYou are a helper.\n"
)
# The textual rewrite: entries gain `- `, deeper lines and comments gain two spaces, and
# every value keeps its exact spelling (the flow list, the quoted url, the comment).
_LIST_FORM = (
    "---\nname: my-agent\ndescription: d\n"
    "mcpServers:\n"
    "    # inline, scoped to this agent\n"
    "  - playwright:\n      type: stdio\n      command: npx\n"
    '      args: ["-y", "@playwright/mcp"]\n'
    '  - remote: {type: http, url: "https://example.com/mcp"}\n'
    "model: opus\n"
    "---\nYou are a helper.\n"
)


def _place(tmp_path: Path, name: str, content: str) -> Path:
    agents_dir = sub.project_agents_dir(tmp_path)
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name}.md"
    path.write_bytes(content.encode("utf-8"))
    return path


def test_read_converts_the_mcp_servers_mapping_to_the_list_form(tmp_path: Path) -> None:
    # Maintainer call on TASK-449.5, "convert on save": a file in the old mapping form loads
    # already rewritten in the list form, with a note, and only the mcpServers block changes.
    path = _place(tmp_path, "my-agent", _MAP_FORM)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["content"] == _LIST_FORM
    assert doc["converted"] == sub.MCP_SERVERS_CONVERSION_NOTE
    assert "list form" in doc["converted"]
    # The stale-write guard still keys on the bytes ON DISK, and the read wrote nothing.
    assert doc["hash"] == cw.hash_bytes(_MAP_FORM.encode("utf-8"))
    assert path.read_bytes() == _MAP_FORM.encode("utf-8")
    # The display field describes the file as it is, so it keeps the mapping.
    assert set(doc["frontmatter"]["mcpServers"]) == {"playwright", "remote"}


def test_saving_the_converted_content_writes_the_list_form(tmp_path: Path) -> None:
    # The next save is what fixes the file: the converted text validates and replaces the
    # mapping on disk, against the hash the read returned.
    path = _place(tmp_path, "my-agent", _MAP_FORM)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    sub.write_project_agent(tmp_path, "my-agent", doc["content"], doc["hash"])
    assert path.read_bytes() == _LIST_FORM.encode("utf-8")
    frontmatter, body = sub.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert frontmatter["mcpServers"] == [
        {"playwright": {"type": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp"]}},
        {"remote": {"type": "http", "url": "https://example.com/mcp"}},
    ]
    assert body == "You are a helper.\n"
    again = sub.read_project_agent(tmp_path, "my-agent")
    assert again["converted"] is None
    assert again["content"] == _LIST_FORM


def test_saving_converted_content_over_a_changed_file_is_stale(tmp_path: Path) -> None:
    # The converted text carries the hash of the file it came from, so an edit on disk
    # after the load still makes the save a 409 rather than a silent overwrite.
    path = _place(tmp_path, "my-agent", _MAP_FORM)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    path.write_bytes(_MAP_FORM.replace("model: opus", "model: haiku").encode("utf-8"))
    with pytest.raises(cw.StaleConfigWriteError):
        sub.write_project_agent(tmp_path, "my-agent", doc["content"], doc["hash"])
    assert b"model: haiku" in path.read_bytes()


def test_conversion_keeps_every_value_spelled_as_written(tmp_path: Path) -> None:
    # PyYAML reads YAML 1.1 and the CLI reads YAML 1.2, which disagree on `yes`, `0755`,
    # `1:30` and on whether `'1e5'` needs its quotes. Re-indenting instead of re-dumping
    # keeps each scalar's text, so the CLI reads the converted file as it read the old one.
    # Since TASK-449.6 the shared seam refuses the UNQUOTED nested `yes`, `0755` and `1:30`
    # outright (see the next test), so this one pins that the quoting survives, and a
    # plain spelling both versions read alike (`0x1F`) keeps its text.
    text = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  srv:\n    command: x\n"
        "    env:\n      DEBUG: 'yes'\n      MODE: \"0755\"\n      PORT: '1:30'\n"
        "      T: '1e5'\n      H: 0x1F\n---\nbody\n"
    )
    _place(tmp_path, "my-agent", text)
    content = sub.read_project_agent(tmp_path, "my-agent")["content"]
    for spelling in ("DEBUG: 'yes'", 'MODE: "0755"', "PORT: '1:30'", "T: '1e5'", "H: 0x1F"):
        assert f"        {spelling}\n" in content
    assert "  - srv:\n" in content


def test_a_nested_yaml_1_1_only_spelling_blocks_the_conversion(tmp_path: Path) -> None:
    # The unquoted `DEBUG: yes` is a boolean to PyYAML and the string "yes" to the CLI, so
    # the shared seam refuses the header (TASK-449.6). The file is shown as it is, with the
    # error, and is not converted: the conversion never builds on a header the seam refused.
    text = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  srv:\n    command: x\n"
        "    env:\n      DEBUG: yes\n---\nbody\n"
    )
    _place(tmp_path, "my-agent", text)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["content"] == text
    assert not doc.get("converted")


def test_conversion_keeps_crlf_line_endings(tmp_path: Path) -> None:
    _place(tmp_path, "my-agent", _MAP_FORM.replace("\n", "\r\n"))
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["content"] == _LIST_FORM.replace("\n", "\r\n")


def test_conversion_of_a_block_that_ends_the_header(tmp_path: Path) -> None:
    # The header slice has no newline before the closing fence: the last entry line keeps
    # that shape, so the body does not gain a blank line.
    text = "---\nname: my-agent\ndescription: d\nmcpServers:\n  srv:\n    command: x\n---\nbody\n"
    _place(tmp_path, "my-agent", text)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["content"] == (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  - srv:\n      command: x\n---\nbody\n"
    )


def test_conversion_keeps_an_anchor_used_after_the_block(tmp_path: Path) -> None:
    # The anchor's text stays where it was, so an alias after the block still resolves.
    text = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  srv: &cfg {command: x}\n"
        "\n  other: {command: y}\nextra: *cfg\n---\nbody\n"
    )
    _place(tmp_path, "my-agent", text)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["converted"] is not None
    # The blank line between the entries stays as it is.
    assert (
        "  - srv: &cfg {command: x}\n\n  - other: {command: y}\nextra: *cfg\n" in (doc["content"])
    )


@pytest.mark.parametrize(
    "text",
    [
        # A flow mapping on the key line: re-spelling it would mean re-dumping values.
        "---\nname: my-agent\ndescription: d\nmcpServers: {a: {command: x}}\n---\nbody\n",
        "---\nname: my-agent\ndescription: d\nmcpServers: {}\n---\nbody\n",
        # Two top-level mcpServers lines: which one YAML keeps is not the line scan's call.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  a: {command: x}\n"
        "mcpServers:\n  b: {command: y}\n---\nbody\n",
        # Two entries with one name: the mapping kept only the second, the list would keep
        # both, so the re-parse check refuses to change what the file means.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  gh: {command: x}\n"
        "  gh: {command: y}\n---\nbody\n",
        # Keys that Python counts as one (`1` and `true`) collapse the same way.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  1: {command: x}\n"
        "  true: {command: y}\n---\nbody\n",
        # A comment shallower than the entries: no safe column to move it to.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n    a:\n      command: x\n"
        "  # note\n    b:\n      command: y\n---\nbody\n",
        # A tab after the indentation spaces, inside a block scalar.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  a:\n    command: |\n"
        "      \tindented\n---\nbody\n",
    ],
)
def test_conversion_falls_back_to_the_raw_file_when_unsafe(tmp_path: Path, text: str) -> None:
    _place(tmp_path, "my-agent", text)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["content"] == text
    assert doc["converted"] is None


def test_list_form_and_unparsable_files_are_not_converted(tmp_path: Path) -> None:
    _place(tmp_path, "listed", _LIST_FORM.replace("my-agent", "listed"))
    assert sub.read_project_agent(tmp_path, "listed")["converted"] is None
    _place(tmp_path, "broken", "no frontmatter here\n")
    doc = sub.read_project_agent(tmp_path, "broken")
    assert (doc["content"], doc["converted"]) == ("no frontmatter here\n", None)
    assert sub._mcp_servers_as_list("no fence\n") is None


def test_read_only_files_are_not_converted(tmp_path: Path) -> None:
    # A plugin-marked file and a built-in override cannot be saved here, so the editor must
    # show them exactly as they are.
    plugin = _MAP_FORM.replace("model: opus", "model: opus\nnote: ${CLAUDE_PLUGIN_ROOT}")
    _place(tmp_path, "my-agent", plugin)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert (doc["editable"], doc["content"], doc["converted"]) == (False, plugin, None)
    override = _MAP_FORM.replace("my-agent", "claude")
    _place(tmp_path, "claude", override)
    doc = sub.read_project_agent(tmp_path, "claude")
    assert (doc["editable"], doc["content"], doc["converted"]) == (False, override, None)


def test_the_unconverted_mapping_is_still_refused_with_the_list_form_explained(
    tmp_path: Path,
) -> None:
    # A direct API write of the mapping is refused, and the message says how to fix it.
    with pytest.raises(cw.InvalidCandidateError, match="reads only the list form"):
        sub.write_project_agent(tmp_path, "my-agent", _MAP_FORM, None)


def test_conversion_is_dropped_when_the_rewrite_reads_back_as_other_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The last guard, pinned on its own: a rewrite that parses but does not read back as the
    # same servers is never handed to the editor.
    monkeypatch.setattr(sub, "_indent_as_list_items", lambda _lines: ["  - other: {command: z}\n"])
    assert sub._mcp_servers_as_list(_MAP_FORM) is None
    # ...and one that does not parse at all is dropped the same way.
    monkeypatch.setattr(sub, "_indent_as_list_items", lambda _lines: ["  - [unclosed\n"])
    assert sub._mcp_servers_as_list(_MAP_FORM) is None


# --- Inline mcpServers definitions go through the MCP tab's validator (TASK-479) ----------
#
# The 2.1.318 agent-file loader reads each inline item with the `.mcp.json` entry schema,
# skips an item with more than one key, and skips a reserved name, but applies no charset
# to the name. Each case below was probed against that CLI in an isolated config dir.


def _servers(*items: object) -> dict[str, object]:
    return {"name": "x", "description": "d", "mcpServers": list(items)}


@pytest.mark.parametrize(
    "item",
    [
        # Names the agent-file loader connects, though the MCP tab refuses them for
        # `.mcp.json`: no charset applies to an inline name.
        {"dotted.name": {"command": "npx"}},
        {"space name": {"command": "npx"}},
        {"-lead": {"command": "npx"}},
        {"constructor": {"command": "npx"}},
        {"prototype": {"command": "npx"}},
        # Every field the MCP tab accepts, per transport.
        {
            "full-http": {
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${TOKEN}"},
                "headersHelper": "/usr/local/bin/helper",
                "oauth": {"clientId": "c", "callbackPort": 8080, "xaa": True},
                "request_timeout_ms": 5000,
                "tools": [{"name": "fetch", "permission_policy": "always_ask"}],
                "toolPermissions": {"fetch": "ask"},
                "displayName": "Full",
                "discoveryCache": False,
                "timeout": 30000,
                "alwaysLoad": True,
                "bareElicitationCapability": True,
                "role": "comms",
            }
        },
        {"sock": {"type": "ws", "url": "wss://example.com", "headersHelper": "h"}},
        {"local": {"type": "stdio", "command": "npx", "args": ["-y", "x"], "env": {"A": "b"}}},
    ],
)
def test_inline_servers_accept_what_the_agent_loader_reads(item: object) -> None:
    sub.validate_frontmatter(_servers(item, "github"))


@pytest.mark.parametrize(
    ("items", "message"),
    [
        # The MCP tab's field refusals, now reached for inline servers too.
        ([{"srv": {"transport": "sse", "url": "https://e.com"}}], "'transport' is not"),
        ([{"srv": {"type": "http", "url": "https://e.com", "env": {"A": "b"}}}], "has 'env'"),
        ([{"srv": {"command": "x", "cwd": "/tmp"}}], r"unknown keys: \['cwd'\]"),
        ([{"srv": {"url": "https://e.com"}}], "has a 'url' but no 'type'"),
        ([{"srv": {"type": "sdk", "name": "x"}}], "registered at runtime"),
        ([{"srv": {"type": "bogus", "command": "x"}}], "unknown server transport"),
        ([{"srv": {"type": "http"}}], "requires a non-empty string 'url'"),
        ([{"srv": {"args": ["a"]}}], "requires a non-empty string 'command'"),
        ([{"srv": {"command": "x", "timeout": 0}}], "'timeout' must be a positive integer"),
        ([{"srv": {"type": "ws", "url": "wss://e", "oauth": {}}}], "unknown keys"),
        ([{"srv": "not-an-object"}], "must be an object"),
        # What the agent-file loader skips.
        ([{"a": {"command": "x"}, "b": {"command": "y"}}], "defines 2 servers"),
        ([{"gh": {"command": "x"}}, {"gh": {"command": "y"}}], "defines inline server 'gh' twice"),
        # YAML can give a definition a number or null key; the 422 still names the keys.
        ([{"srv": {"command": "x", 1: "y", None: "z"}}], r"unknown keys: \[1, None\]"),
        ([{"srv": {"type": "http", "url": "u", "oauth": {1: "a", "b": "c"}}}], "unknown keys"),
        (
            [{"srv": {"type": "http", "url": "u", "tools": [{"name": "n", 1: "a", "b": "c"}]}}],
            "unknown",
        ),
    ],
)
def test_inline_servers_refuse_what_the_cli_rejects_or_ignores(
    items: list[object], message: str
) -> None:
    with pytest.raises(cw.InvalidCandidateError, match=message) as exc:
        sub.validate_frontmatter(_servers(*items))
    # The message says which subagent field and which server it is about.
    assert str(exc.value).startswith("frontmatter 'mcpServers'")


@pytest.mark.parametrize("name", sorted(sub._FRONTMATTER_SKIPPED_SERVER_NAMES))
def test_inline_servers_refuse_each_name_the_agent_loader_skips(name: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="is reserved by Claude Code"):
        sub.validate_frontmatter(_servers({name: {"command": "x"}}))


def test_a_string_reference_may_repeat_an_inline_name() -> None:
    # Only two INLINE definitions of one name collide: the loader skips string references
    # when it builds the inline servers.
    sub.validate_frontmatter(_servers("gh", {"gh": {"command": "x"}}))


@pytest.mark.parametrize(
    "definition",
    [
        "      alwaysLoad: yes\n",
        "      bareElicitationCapability: on\n",
        "      timeout: 1_000\n",
        "      timeout: 010\n",
        '      type: http\n      url: "https://e.com"\n      oauth:\n        xaa: no\n',
        "      type: http\n      url: https://e.com\n      oauth:\n        callbackPort: 0b101\n",
        '      type: http\n      url: "https://e.com"\n      discoveryCache: off\n',
        '      type: http\n      url: "https://e.com"\n      request_timeout_ms: 1:30\n',
        # Through a merge key, which `compose` leaves unflattened.
        "      <<: {alwaysLoad: yes}\n",
    ],
)
def test_inline_servers_refuse_yaml11_only_spellings(definition: str) -> None:
    # PyYAML reads these as booleans or numbers; the CLI's YAML 1.2 reads another value or
    # a string, which its entry schema refuses, so it would skip the whole server.
    if "command" not in definition and "url" not in definition:
        definition = "      command: x\n" + definition
    text = f"---\nname: x\ndescription: d\nmcpServers:\n  - srv:\n{definition}---\nbody\n"
    # TASK-449.6: the shared seam's nested walker refuses this first, for every surface,
    # so the message is the seam's (it names the line and column, never the value).
    with pytest.raises(cw.InvalidCandidateError, match="nested value at line"):
        sub.validate_agent_content(text, expected_name="x")


@pytest.mark.parametrize(
    "item",
    [
        "  - srv:\n      command: x\n      env: {A: 1e3}\n",
        "  - srv:\n      command: x\n      args: [ok, 0o17]\n",
        "  - srv:\n      command: '09'\n      args:\n        - 09\n",
        "  - srv:\n      type: http\n      url: https://e.com\n      headers:\n        X: 1.0e3\n",
        "  - srv:\n      type: http\n      url: https://e.com\n      oauth: {clientId: 1e5}\n",
        "  - 0o17\n",  # a server-name reference
        # Probed one by one as an `args` item on 2.1.318: each made the server invalid.
        *[
            f"  - srv:\n      command: x\n      args: [{v}]\n"
            for v in ("+1e3", "1E3", "-0o17", "+0o7", "-.5", "+.5e-3")
        ],
    ],
)
def test_inline_servers_refuse_strings_yaml12_reads_as_numbers(item: str) -> None:
    # PyYAML reads each plain value as a string; the CLI's YAML 1.2 reads a number, which its
    # entry schema refuses, so it skips the server (probed: `env: {A: 1e3}`, `args: [0o17]`).
    text = f"---\nname: x\ndescription: d\nmcpServers:\n{item}---\nbody\n"
    # TASK-449.6: the shared seam's nested walker refuses this first, for every surface,
    # so the message is the seam's (it names the line and column, never the value).
    with pytest.raises(cw.InvalidCandidateError, match="nested value at line"):
        sub.validate_agent_content(text, expected_name="x")


def test_inline_servers_accept_quoted_number_strings() -> None:
    text = (
        "---\nname: x\ndescription: d\nmcpServers:\n  - srv:\n      command: x\n"
        "      args: ['0o17', \"1e3\", --port, 0O17, 0o8, 1.2.3]\n"
        "      env: {A: '1e3', B: 1e3x}\n---\nbody\n"
    )
    sub.validate_agent_content(text, expected_name="x")


def test_inline_servers_accept_yaml12_spellings() -> None:
    text = (
        "---\nname: x\ndescription: d\nmcpServers:\n  - github\n  - srv:\n"
        "      type: http\n      url: https://e.com\n      alwaysLoad: true\n"
        "      timeout: 30000\n      oauth:\n        xaa: false\n        callbackPort: 8080\n"
        "---\nbody\n"
    )
    sub.validate_agent_content(text, expected_name="x")


@pytest.mark.parametrize(
    "header",
    [
        "mcpServers:\n  srv: {alwaysLoad: yes}\n",  # the mapping form: refused elsewhere
        "mcpServers:\n  - srv: yes\n",  # not a definition object: refused elsewhere
        "mcpServers:\n  - srv: {oauth: yes}\n",  # an oauth that is not an object
        "mcpServers:\n  - srv: {headers: {X: yes}}\n",  # not a boolean field
    ],
)
def test_inline_spelling_check_leaves_other_shapes_to_the_validator(header: str) -> None:
    sub._refuse_yaml11_only_spellings(header)


def _alias_pyramid(depth: int, *, bottom: str = "a") -> str:
    """Return a subagent file whose discarded duplicate `args` keys hold an alias pyramid.

    Each level aliases the one below four times. `safe_load` keeps only the last `args`,
    `[]`, so the loaded data is tiny and passes every check on it; only a walk of the
    composed tree sees the pyramid.
    """
    levels = [f"      args: &p0 [{bottom}, {bottom}, {bottom}, {bottom}]"]
    for level in range(1, depth + 1):
        below = f"*p{level - 1}"
        levels.append(f"      args: &p{level} [{below}, {below}, {below}, {below}]")
    return (
        "---\nname: x\ndescription: d\nmcpServers:\n  - srv:\n      command: c\n"
        + "\n".join(levels)
        + "\n      args: []\n---\nbody\n"
    )


def _naive_walk_visits(node: yaml.Node) -> int:
    """Count the nodes a walk that follows every alias path visits (memoized, so it is fast)."""
    counts: dict[int, int] = {}

    def visits(current: yaml.Node) -> int:
        if id(current) not in counts:
            if isinstance(current, yaml.SequenceNode):
                children = current.value
            elif isinstance(current, yaml.MappingNode):
                children = [value for _key, value in current.value]
            else:
                children = []
            counts[id(current)] = 1 + sum(visits(child) for child in children)
        return counts[id(current)]

    return visits(node)


@pytest.mark.timeout(20)
def test_shared_aliases_under_a_discarded_key_do_not_hang_a_save(tmp_path: Path) -> None:
    # Greptile on TASK-479: the number-string walk followed each alias path again, so this
    # file of under 1 KB, which a walk without memory visits more than 10**12 times, never
    # finished saving and held a server worker. Each composed node is now checked once.
    text = _alias_pyramid(20)
    assert len(text.encode("utf-8")) < 1024
    header = sub._FRONTMATTER_RE.match(text)
    assert header is not None
    root = yaml.compose(header.group(1), Loader=yaml.SafeLoader)
    assert _naive_walk_visits(root) > 10**12  # the positive control: it IS a pyramid
    # TASK-449.6: the shared seam now refuses a repeated key (the two YAML versions may keep
    # different values), so this file is refused, and it must be refused just as fast: every
    # composed-tree walk in the seam visits each node once.
    start = time.monotonic()
    with pytest.raises(cw.InvalidCandidateError, match="see differently"):
        sub.validate_agent_content(text, expected_name="x")
    assert time.monotonic() - start < 0.5
    with pytest.raises(cw.InvalidCandidateError):
        sub.write_project_agent(tmp_path, "x", text, None)
    # The walks still follow aliases: here the bad value sits at the bottom of a chain of
    # distinct keys that only `args: *p20` reaches, 21 alias levels down.
    levels = ["p0: &p0 [1e3, a]"] + [f"p{i}: &p{i} [*p{i - 1}]" for i in range(1, 21)]
    deep = (
        "---\nname: x\ndescription: d\n"
        + "\n".join(levels)
        + "\nmcpServers:\n  - srv:\n      command: c\n      args: *p20\n---\nbody\n"
    )
    start = time.monotonic()
    with pytest.raises(cw.InvalidCandidateError, match="nested value at line"):
        sub.validate_agent_content(deep, expected_name="x")
    assert time.monotonic() - start < 0.5


@pytest.mark.parametrize(
    "bottom", ["{command: c}", "{command: c, alwaysLoad: yes}", "{command: c, args: [1e3]}"]
)
def test_a_long_merge_chain_behind_an_inline_server_is_walked_without_recursion(
    bottom: str,
) -> None:
    # A `<<` chain declared in document order loads fine and passes the merge-size guard
    # (each link is one pair), so the spelling walk met its full length. Recursively, 1500
    # links overflowed the stack; the walk is iterative now and still reaches the bottom.
    links = [f"x0: &a0 {bottom}"] + [f"x{i}: &a{i} {{<<: *a{i - 1}}}" for i in range(1, 1500)]
    text = (
        "---\nname: x\ndescription: d\n"
        + "\n".join(links)
        + "\nmcpServers:\n  - s: *a1499\n---\nbody\n"
    )
    assert len(text.encode("utf-8")) < sub.MAX_BYTES
    if bottom == "{command: c}":
        sub.validate_agent_content(text, expected_name="x")
    else:
        # The shared seam's nested walker refuses it first (TASK-449.6), also iteratively.
        with pytest.raises(cw.InvalidCandidateError, match="nested value at line"):
            sub.validate_agent_content(text, expected_name="x")


def test_each_kind_of_inline_check_keeps_its_own_walked_nodes() -> None:
    # One node reached first as an `env` value (the number-string walk) and then as server
    # b's definition must still get the definition checks: one shared walked set would skip
    # it and accept `alwaysLoad: yes`, which the CLI reads as a string.
    header = "mcpServers:\n  - a: {command: x, env: &n {alwaysLoad: yes}}\n  - b: *n\n"
    # TASK-449.6: the shared seam's walker checks every nested value once, wherever it is
    # reached, and needs no per-check sets: a nested `yes` is a mismatch in any position.
    with pytest.raises(cw.InvalidCandidateError, match="nested value at line"):
        cw.load_frontmatter_yaml(header, what="frontmatter")


_INLINE_LEGACY = (
    "---\nname: my-agent\ndescription: d\n"
    "mcpServers:\n"
    "  - github\n"
    "  - legacy:\n      transport: sse  # old spelling\n      url: https://e.com/sse\n"
    "  - both:\n      type: http\n      transport: http\n      url: https://e.com/mcp\n"
    "      env:\n        # never read\n        TOKEN: x\n\n        OTHER: y\n"
    "      alwaysLoad: true\n"
    "  - local:\n      command: npx\n      env:\n        DEBUG: '1'\n"
    "model: opus\n"
    "---\nYou are a helper.\n"
)
# Only the `transport` and remote `env` lines change; the blank line inside the dropped
# `env` stays, and every other value and comment keeps its spelling.
_INLINE_CONVERTED = (
    "---\nname: my-agent\ndescription: d\n"
    "mcpServers:\n"
    "  - github\n"
    "  - legacy:\n      type: sse  # old spelling\n      url: https://e.com/sse\n"
    "  - both:\n      type: http\n      url: https://e.com/mcp\n"
    "\n"
    "      alwaysLoad: true\n"
    "  - local:\n      command: npx\n      env:\n        DEBUG: '1'\n"
    "model: opus\n"
    "---\nYou are a helper.\n"
)


def test_read_converts_the_inline_fields_claude_code_ignores(tmp_path: Path) -> None:
    path = _place(tmp_path, "my-agent", _INLINE_LEGACY)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["content"] == _INLINE_CONVERTED
    note = doc["converted"]
    assert note == (
        "Inline MCP server 'legacy': 'transport' is converted to \"type\": 'sse' (Claude Code "
        "ignores it, and reads 'type' only). Inline MCP server 'both': 'transport' is removed "
        "because 'type' is set (Claude Code ignores it). Inline MCP server 'both': 'env' on a "
        "remote server is removed (Claude Code ignores it). It held 2 value(s), which Claude "
        f"Code never read. {sub.INLINE_SERVERS_CONVERSION_TAIL}"
    )
    # The note never carries a stored value.
    assert "TOKEN" not in note and "OTHER" not in note
    # The read writes nothing, and the stale-write guard keys on the bytes on disk.
    assert path.read_bytes() == _INLINE_LEGACY.encode("utf-8")
    assert doc["hash"] == cw.hash_bytes(_INLINE_LEGACY.encode("utf-8"))
    # The unconverted file is refused on save; the converted text saves and reads clean.
    with pytest.raises(cw.InvalidCandidateError, match="'transport' is not"):
        sub.write_project_agent(tmp_path, "my-agent", _INLINE_LEGACY, doc["hash"])
    sub.write_project_agent(tmp_path, "my-agent", doc["content"], doc["hash"])
    again = sub.read_project_agent(tmp_path, "my-agent")
    assert (again["content"], again["converted"]) == (_INLINE_CONVERTED, None)


def test_mapping_and_inline_conversion_share_one_note(tmp_path: Path) -> None:
    text = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n"
        "  remote:\n    transport: http\n    url: https://e.com\n---\nbody\n"
    )
    _place(tmp_path, "my-agent", text)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["content"] == (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n"
        "  - remote:\n      type: http\n      url: https://e.com\n---\nbody\n"
    )
    assert doc["converted"].startswith(sub.MCP_SERVERS_CONVERSION_NOTE + " Inline MCP server")
    assert doc["converted"].endswith(sub.INLINE_SERVERS_CONVERSION_TAIL)


def test_inline_conversion_keeps_crlf_line_endings(tmp_path: Path) -> None:
    _place(tmp_path, "my-agent", _INLINE_LEGACY.replace("\n", "\r\n"))
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert doc["content"] == _INLINE_CONVERTED.replace("\n", "\r\n")


def test_inline_conversion_skips_read_only_files(tmp_path: Path) -> None:
    plugin = _INLINE_LEGACY.replace("model: opus", "model: opus\nnote: ${CLAUDE_PLUGIN_ROOT}")
    _place(tmp_path, "my-agent", plugin)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert (doc["editable"], doc["content"], doc["converted"]) == (False, plugin, None)


@pytest.mark.parametrize(
    "text",
    [
        # A definition in flow style: there is no key line to edit.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n"
        "  - srv: {transport: sse, url: https://e.com}\n---\nbody\n",
        # The whole list in flow style on the key line.
        "---\nname: my-agent\ndescription: d\n"
        "mcpServers: [{srv: {transport: sse, url: https://e.com}}]\n---\nbody\n",
        # The ignored field comes in through a merge key.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n"
        "  - srv:\n      <<: {transport: sse}\n      url: https://e.com\n---\nbody\n",
        # A line break only to YAML 1.1 somewhere in the header.
        '---\nname: my-agent\ndescription: "d\u2028e"\nmcpServers:\n'
        "  - srv:\n      transport: sse\n      url: https://e.com\n---\nbody\n",
        # A `transport` that is not a transport word: never converted, refused on save.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n"
        "  - srv:\n      transport: carrier-pigeon\n      url: https://e.com\n---\nbody\n",
        # Two top-level mcpServers lines: the line scan cannot tell which one YAML keeps.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  - a: {command: x}\n"
        "mcpServers:\n  - srv:\n      transport: sse\n      url: https://e.com\n---\nbody\n",
        # Nothing to convert: the stdio `env` stays, a multi-key item is the save's problem.
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  - gh\n"
        "  - a: {command: x, env: {A: b}}\n    b: {command: y}\n---\nbody\n",
    ],
)
def test_inline_conversion_falls_back_to_the_raw_file(tmp_path: Path, text: str) -> None:
    _place(tmp_path, "my-agent", text)
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert (doc["content"], doc["converted"]) == (text, None)


@pytest.mark.parametrize(
    ("definition", "fix"),
    [
        ("{type: http, url: https://e.com, env: {A: b}}", "remove 'env' by hand."),
        ("{type: http, transport: http, url: https://e.com}", "remove 'transport' by hand."),
        # Removing a `transport` that `type` does not back up would only lead to the next
        # refusal ("a 'url' but no 'type'"), so the fix is the rename the reload makes.
        ("{transport: sse, url: https://e.com}", "rename 'transport' to 'type' by hand."),
        # Both fields at once: one manual fix must clear both refusals.
        (
            "{transport: http, url: https://e.com, env: {A: b}}",
            "rename 'transport' to 'type' and remove 'env' by hand.",
        ),
    ],
)
def test_unconverted_inline_field_says_how_to_fix_it_by_hand(
    tmp_path: Path, definition: str, fix: str
) -> None:
    # A flow-style definition loads unconverted, so its save must not only say "reload".
    text = f"---\nname: my-agent\ndescription: d\nmcpServers:\n  - srv: {definition}\n---\nb\n"
    with pytest.raises(cw.InvalidCandidateError) as raised:
        sub.write_project_agent(tmp_path, "my-agent", text, None)
    assert str(raised.value).endswith(fix)
    # A field the converter never touches gets no such hint.
    with pytest.raises(cw.InvalidCandidateError) as exc:
        sub.validate_frontmatter(_servers({"srv": {"command": "x", "cwd": "/"}}))
    assert "by hand" not in str(exc.value)


def test_inline_conversion_needs_a_parsable_list_form() -> None:
    assert sub._convert_inline_servers("no fence\n") is None
    assert sub._convert_inline_servers("---\nname: [unclosed\n---\nb\n") is None
    assert sub._convert_inline_servers(_MAP_FORM) is None  # the mapping step runs first


def test_inline_conversion_is_dropped_when_the_rewrite_reads_back_as_other_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sub, "_edit_inline_definitions", lambda _b, _e: ["  - other\n"])
    assert sub._convert_inline_servers(_INLINE_LEGACY) is None
    monkeypatch.setattr(sub, "_edit_inline_definitions", lambda _b, _e: ["  - [unclosed\n"])
    assert sub._convert_inline_servers(_INLINE_LEGACY) is None


def test_inline_edit_leaves_lines_it_cannot_place() -> None:
    # A key line before any item (index -1), or past the last planned item, is kept: the
    # caller's re-parse check is what refuses such a block.
    block = ["  odd: 1\n", "    transport: sse\n", "  - a:\n", "      transport: sse\n"]
    assert sub._edit_inline_definitions(block, []) == block
    # A comment under a dropped key goes with it; one at the key column stays.
    block = ["  - a:\n", "      env:\n", "        # gone\n", "      # kept\n", "      url: u\n"]
    out = sub._edit_inline_definitions(block, [{"env": "drop"}])
    assert out == ["  - a:\n", "      # kept\n", "      url: u\n"]


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


def test_route_saves_the_documented_mcp_list_and_refuses_the_mapping(
    write_config, tmp_path, projects_root
) -> None:
    # End to end through the PUT route: the documented list spelling saves, the mapping
    # spelling Claude Code ignores is a 422 that writes nothing.
    body = {"scope": "project", "project": "alpha", "confirm": "alpha"}
    mapping = _content("map-agent", mcpServers="{srv: {command: npx}}")
    listed = _content("list-agent", mcpServers="[github, {srv: {command: npx}}]")
    with _client(write_config, tmp_path, _ON) as c:
        bad = c.put(_agent_url("map-agent"), json={**body, "content": mapping})
        assert bad.status_code == 422
        assert "mcpServers" in bad.json()["detail"]
        ok = c.put(_agent_url("list-agent"), json={**body, "content": listed})
        assert ok.status_code == 200, ok.text
    agents = projects_root / "alpha" / ".claude" / "agents"
    assert not (agents / "map-agent.md").exists()
    assert (agents / "list-agent.md").read_bytes() == listed.encode("utf-8")


def test_route_get_returns_the_converted_list_form_and_put_saves_it(
    write_config, tmp_path, projects_root
) -> None:
    # Convert on save, end to end: GET hands the editor the list form plus the note, and a
    # PUT of exactly that content against the returned hash writes the list form.
    path = _place(projects_root / "alpha", "my-agent", _MAP_FORM)
    with _client(write_config, tmp_path, _ON) as c:
        got = c.get(_agent_url("my-agent"), params={"scope": "project", "project": "alpha"})
        assert got.status_code == 200, got.text
        doc = got.json()
        assert doc["converted"] == sub.MCP_SERVERS_CONVERSION_NOTE
        assert doc["content"] == _LIST_FORM
        put = c.put(
            _agent_url("my-agent"),
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": doc["content"],
                "hash": doc["hash"],
            },
        )
        assert put.status_code == 200, put.text
    assert path.read_bytes() == _LIST_FORM.encode("utf-8")


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


def test_read_agent_degrades_an_alias_pyramid_file_instead_of_hanging_the_worker(
    tmp_path: Path,
) -> None:
    # #1393: a 346-byte billion-laughs header is legal ACYCLIC YAML, so it was accepted by the
    # write and then re-expanded on every GET -- `redact_secrets` walked O(distinct paths) and
    # held an `asyncio.to_thread` worker for as long as it ran (0.72s here for this header, and
    # a 472-byte one never finished). The parse seam now refuses it by expanded SIZE, so no NEW
    # file can hold one; a file an earlier release wrote takes the SAME degradation path as a
    # self-referential one above -- `frontmatter` becomes `{}`, `content` still comes back so
    # the operator can repair it. Raw bytes on purpose: that is how it got there.
    root = tmp_path / "agents"
    root.mkdir()
    levels = ["a0: &a0 x"]
    for i in range(1, 9):
        levels.append(f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 8) + "]")
    raw = ("---\nname: legacy\ndescription: d\n" + "\n".join(levels) + "\n---\nbody\n").encode()
    (root / "legacy.md").write_bytes(raw)

    started = time.perf_counter()
    doc = sub._read_agent(root, "legacy", "project")
    assert time.perf_counter() - started < 0.5  # orders of magnitude under the 0.72s blow-up

    assert doc["frontmatter"] == {}
    assert doc["content"] == raw.decode()


def test_read_agent_masks_a_secret_inside_an_omap_reached_through_a_tuple(
    tmp_path: Path,
) -> None:
    # #1393: `safe_load` builds a list of TUPLES for `!!omap`, and `redact_secrets` recursed
    # `dict | list` only -- so the tuple was returned by identity and `jsonable_encoder`
    # rendered the live secret into the response as a JSON array. This is the end-to-end pin
    # on the real read path, not just the unit walk: the file is written with raw bytes,
    # because a header carrying an `!!omap` is accepted by the write validator too.
    root = tmp_path / "agents"
    root.mkdir()
    raw = (
        b"---\nname: my-agent\ndescription: d\nauth: !!omap\n"
        b"  - token: sk-live-DEADBEEF\n---\nbody\n"
    )
    (root / "my-agent.md").write_bytes(raw)

    doc = sub._read_agent(root, "my-agent", "project")

    assert "sk-live-DEADBEEF" not in json.dumps(doc["frontmatter"])
    assert cw.REDACTION_SENTINEL in json.dumps(doc["frontmatter"])
    assert doc["content"] == raw.decode()  # `content` is verbatim by contract, as always


def test_read_agent_masks_a_binary_spelled_secret_reached_as_bytes(
    tmp_path: Path,
) -> None:
    # #1450: a UTF-8 `!!binary` value parses to `bytes`, and `_is_secretish` rejected any
    # non-`str` value -- so a secret spelled as base64 fell through `redact_secrets`' scalar
    # return by IDENTITY and `jsonable_encoder` decoded it back into `frontmatter`. Same shape
    # as the #1393 omap identity leak. End-to-end pin on the real read path: the write validator
    # accepts a UTF-8 `!!binary` header, so a cloned repo can carry one.
    # `c2stbGl2ZS1ERUFEQkVFRg==` is base64 of `sk-live-DEADBEEF`.
    root = tmp_path / "agents"
    root.mkdir()
    raw = (
        b"---\nname: my-agent\ndescription: d\n"
        b"api_token: !!binary c2stbGl2ZS1ERUFEQkVFRg==\n---\nbody\n"
    )
    (root / "my-agent.md").write_bytes(raw)

    doc = sub._read_agent(root, "my-agent", "project")

    assert "sk-live-DEADBEEF" not in json.dumps(doc["frontmatter"])
    assert doc["frontmatter"]["api_token"] == cw.REDACTION_SENTINEL
    assert doc["content"] == raw.decode()  # `content` is verbatim by contract, as always


def test_read_agent_omap_inner_key_is_a_secret_hint_the_way_a_dict_key_is() -> None:
    # Redaction must not depend on how the document SPELLS a mapping. The hint comes from a
    # key, and an `!!omap`/`!!pairs` entry is a two-element tuple whose first element IS the
    # key -- so `mcpServers: !!omap [{token: sk-live-…}]` masks exactly like the dict
    # spelling, even though the only outer key (`mcpServers`) is benign. Before #1393 the
    # whole tuple passed by IDENTITY, so this spelling leaked past the key-name rule and past
    # the `${interp}` and credential-URL rules too.
    parsed = yaml.safe_load("mcpServers: !!omap\n  - token: sk-live-DEADBEEF\n")
    red = cw.redact_secrets(parsed)
    assert red == {"mcpServers": [["token", cw.REDACTION_SENTINEL]]}
    # The KEY survives, as a dict key does -- masking it too would be over-masking that makes
    # the display useless, and the dict branch has never masked a key by itself.
    dict_spelling = cw.redact_secrets(yaml.safe_load("mcpServers:\n  token: sk-live-DEADBEEF\n"))
    assert dict_spelling == {"mcpServers": {"token": cw.REDACTION_SENTINEL}}
    # Under a secret-shaped ANCESTOR both elements mask, because the ambient hint still
    # reaches the key element. Over-masking, deliberately: that is the safe direction.
    secret_outer = cw.redact_secrets(yaml.safe_load("auth: !!omap\n  - name: sk-live-DEADBEEF\n"))
    assert secret_outer == {"auth": [[cw.REDACTION_SENTINEL, cw.REDACTION_SENTINEL]]}


def test_read_agent_omap_mcp_servers_get_the_same_server_name_carve_out(
    tmp_path: Path,
) -> None:
    # The `mcpServers` carve-out exists because the block is keyed by user-chosen SERVER
    # NAMES, not config keys, so a name must not act as a secret hint. It was guarded by
    # `isinstance(servers, dict)` and so missed the `!!omap`/`!!pairs` spelling -- where the
    # redactor's own pair rule then DID treat the server name as a key, masking a server's
    # `command`/`args` that the dict spelling shows in the clear. The two spellings of one
    # block must agree; this is the direction the tuple walk made possible, so it is pinned
    # against the dict spelling rather than in isolation.
    root = tmp_path / "agents"
    root.mkdir()
    raw = (
        b"---\nname: my-agent\ndescription: d\nmcpServers: !!omap\n"
        b"  - auth-gw:\n"
        b"      command: npx\n"
        b"      args: [-y, srv]\n"
        b"      env: {API_TOKEN: sk-live-DEADBEEF}\n"
        b"---\nbody\n"
    )
    (root / "my-agent.md").write_bytes(raw)

    doc = sub._read_agent(root, "my-agent", "project")
    name_, entry = doc["frontmatter"]["mcpServers"][0]

    assert name_ == "auth-gw"  # the server name survives, as a dict key does
    assert entry["command"] == "npx"  # NOT masked by its own server name
    assert entry["args"] == ["-y", "srv"]
    assert entry["env"]["API_TOKEN"] == cw.REDACTION_SENTINEL  # a real secret still masks

    # The dict spelling of the identical block, for the same assertions.
    dict_raw = raw.replace(b"mcpServers: !!omap\n  - auth-gw:\n", b"mcpServers:\n  auth-gw:\n")
    (root / "dict-agent.md").write_bytes(dict_raw.replace(b"name: my-agent", b"name: dict-agent"))
    dict_doc = sub._read_agent(root, "dict-agent", "project")
    dict_entry = dict_doc["frontmatter"]["mcpServers"]["auth-gw"]

    assert dict_entry["command"] == "npx"
    assert dict_entry["env"]["API_TOKEN"] == cw.REDACTION_SENTINEL
    assert dict_entry == entry  # the two spellings display the same thing


def test_read_agent_omap_server_name_that_is_a_container_is_still_redacted(
    tmp_path: Path,
) -> None:
    # The `mcpServers` carve-out re-redacts an entry's NAME as well as its value, where the
    # dict branch keeps its key verbatim. `construct_yaml_omap` puts no hashability
    # constraint on a key, so an omap "name" can be a whole container carrying a secret,
    # while a dict key is always a scalar. Skipping the name -- the obvious mirror of the
    # dict branch -- put that secret back into the display field the carve-out exists to
    # clean, and contradicted `redact_secrets`, which masks element 0 of a pair.
    root = tmp_path / "agents"
    root.mkdir()
    raw = (
        b"---\nname: my-agent\ndescription: d\nmcpServers: !!omap\n"
        b"  - ? {token: sk-live-DEADBEEF}\n"
        b"    : {command: npx}\n"
        b"---\nbody\n"
    )
    (root / "my-agent.md").write_bytes(raw)

    doc = sub._read_agent(root, "my-agent", "project")

    assert "sk-live-DEADBEEF" not in json.dumps(doc["frontmatter"])
    # ...and an ordinary string name is untouched by that, because with no hint it is not
    # secret-shaped. Pinned here so the fix cannot be "mask the name unconditionally".
    assert cw.redact_secrets("auth-gw") == "auth-gw"


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
