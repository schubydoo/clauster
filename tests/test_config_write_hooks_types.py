"""Hooks surface: every documented event, hook type and field (TASK-449.4).

Ground truth is https://code.claude.com/docs/en/hooks ("Hook lifecycle", "Matcher
patterns", "Hook handler fields", "Prompt hook configuration", "Agent hook
configuration"), cross-checked against the installed CLI 2.1.318. The lists below are
copied from those docs by hand, on purpose: they are the independent reference the
validator's own tables are checked against, so a dropped or invented entry in
``config_write_hooks`` fails here.

Also covers the read-side masking of the secret-bearing fields (``http`` headers, URL
credentials, secret-shaped ``mcp_tool`` input) and the write-side keep-stored merge,
which restores a masked value only for a hook with the same destination. Every test
writes under ``tmp_path`` or the autouse isolated HOME.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_hooks as hooks
from clauster.app import create_app
from clauster.config import load_config

S = cw.REDACTION_SENTINEL

#: The "Hook lifecycle" table of the hooks reference, in its order (33 events).
DOCUMENTED_EVENTS = [
    "SessionStart",
    "Setup",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "MessageDisplay",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "TeammateIdle",
    "InstructionsLoaded",
    "ConfigChange",
    "CwdChanged",
    "DirectoryAdded",
    "FileChanged",
    "WorktreeCreate",
    "WorktreeRemove",
    "PreCompact",
    "PostCompact",
    "PreModelSwitch",
    "PostModelSwitch",
    "Elicitation",
    "ElicitationResult",
    "SessionEnd",
]

#: A minimal valid handler of each documented type (its required fields only).
MINIMAL = {
    "command": {"type": "command", "command": "echo hi"},
    "http": {"type": "http", "url": "http://localhost:8080/hooks/pre-tool-use"},
    "mcp_tool": {"type": "mcp_tool", "server": "my_server", "tool": "security_scan"},
    "prompt": {"type": "prompt", "prompt": "Evaluate if Claude should stop: $ARGUMENTS"},
    "agent": {"type": "agent", "prompt": "Verify that all unit tests pass. $ARGUMENTS"},
}

#: Every documented optional field per type, with a valid value from the docs. The
#: common fields (``if``, ``timeout``, ``statusMessage``, ``once``) apply to all five.
COMMON = {"if": "Bash(git *)", "timeout": 30, "statusMessage": "Checking", "once": True}
DOCUMENTED_FIELDS = {
    "command": {
        **COMMON,
        "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/check-command.js", "--fix"],
        "async": True,
        "asyncRewake": False,
        "shell": "powershell",
        "onFailure": "block",
    },
    "http": {
        **COMMON,
        "headers": {"Authorization": "Bearer $MY_TOKEN"},
        "allowedEnvVars": ["MY_TOKEN"],
        "onFailure": "continue",
    },
    "mcp_tool": {**COMMON, "input": {"file_path": "${tool_input.file_path}"}},
    "prompt": {**COMMON, "model": "claude-sonnet-5", "continueOnBlock": True},
    "agent": {**COMMON, "model": "claude-sonnet-5"},
}

#: Fields the installed CLI's hook schemas (2.1.318) parse but the docs do not list, with
#: the CLI's types. Accepted because what the CLI reads wins (maintainer, 2026-11-05).
CLI_PARSED_FIELDS = {
    "command": {
        "rewakeMessage": "Background check failed:",
        "rewakeSummary": "Check",
        "cloud": "skip",
    },
    "http": {"cloud": "device"},
}

_FIELD_CASES = [
    (t, f, v)
    for table in (DOCUMENTED_FIELDS, CLI_PARSED_FIELDS)
    for t, fields in table.items()
    for f, v in fields.items()
]


#: A Slack-shaped webhook URL, assembled at runtime: the IDs and token are fake, but a
#: literal in this shape trips secret scanning on push.
_SLACK_BASE = "https://hooks." + "slack.com/services/"
_SLACK_PARTS = ("T0123" + "ABCD", "B0456" + "EFGH", "aBcD1234" + "eFgH5678iJkL9012")


def _cred_url(user: str, password: str, rest: str) -> str:
    """Return ``https://user:password@rest``, assembled so no credential URL is a literal."""
    return "https://" + user + ":" + password + "@" + rest


def _block(entry: dict, event: str = "PreToolUse") -> dict:
    return {event: [{"matcher": "Bash", "hooks": [entry]}]}


# --- events --------------------------------------------------------------------------


def test_recognized_events_match_the_documented_list() -> None:
    assert len(DOCUMENTED_EVENTS) == len(set(DOCUMENTED_EVENTS)) == 33
    assert set(DOCUMENTED_EVENTS) == hooks.RECOGNIZED_EVENTS


@pytest.mark.parametrize("event", DOCUMENTED_EVENTS)
@pytest.mark.parametrize("hook_type", list(MINIMAL))
def test_every_documented_event_accepts_every_type(event: str, hook_type: str) -> None:
    hooks.validate_hooks(_block(MINIMAL[hook_type], event))


# --- matcher forms ("Matcher patterns") ---------------------------------------------


@pytest.mark.parametrize(
    "matcher",
    ["*", "", "Bash", "Edit|Write", "Edit, Write", "^Notebook", "mcp__memory__.*", ".envrc|.env"],
)
def test_every_documented_matcher_form_is_accepted(matcher: str) -> None:
    hooks.validate_hooks({"PreToolUse": [{"matcher": matcher, "hooks": [MINIMAL["command"]]}]})


def test_matcher_on_an_event_without_matcher_support_is_accepted() -> None:
    # "If you add a matcher field to an event without matcher support, it is silently
    # ignored" — valid config, so it is stored, not refused.
    hooks.validate_hooks({"CwdChanged": [{"matcher": "x", "hooks": [MINIMAL["command"]]}]})


# --- types and fields ----------------------------------------------------------------


def test_hook_types_are_the_five_documented_types() -> None:
    assert hooks.HOOK_TYPES == ("command", "http", "mcp_tool", "prompt", "agent")


@pytest.mark.parametrize("hook_type", list(MINIMAL))
def test_minimal_handler_of_each_type_is_accepted(hook_type: str) -> None:
    hooks.validate_hooks(_block(MINIMAL[hook_type]))


@pytest.mark.parametrize(("hook_type", "field", "value"), _FIELD_CASES)
def test_each_documented_field_is_accepted(hook_type: str, field: str, value: object) -> None:
    hooks.validate_hooks(_block({**MINIMAL[hook_type], field: value}))


@pytest.mark.parametrize("hook_type", list(MINIMAL))
def test_all_documented_fields_together_are_accepted(hook_type: str) -> None:
    hooks.validate_hooks(_block({**MINIMAL[hook_type], **DOCUMENTED_FIELDS[hook_type]}))


def test_every_validator_field_is_documented_or_cli_parsed() -> None:
    """The validator accepts no field outside the docs and the CLI schemas (and checks each)."""
    documented = {"type"} | {f for fields in DOCUMENTED_FIELDS.values() for f in fields}
    documented |= {f for fields in CLI_PARSED_FIELDS.values() for f in fields}
    documented |= {f for e in MINIMAL.values() for f in e}
    accepted = set(hooks._COMMON_FIELDS)
    for required, optional in hooks._TYPE_FIELDS.values():
        accepted |= required | optional
    assert accepted == documented
    assert set(hooks._FIELD_CHECKS) == accepted - {"type"}


@pytest.mark.parametrize(
    ("hook_type", "field", "value"),
    [
        (t, f, v)
        for t in MINIMAL
        for f, v in [
            ("if", ""),
            ("if", 1),
            ("timeout", "30"),
            ("timeout", True),
            ("timeout", 0),  # the CLI reads timeout as number().positive()
            ("timeout", -5),
            ("statusMessage", 5),
            ("once", "yes"),
        ]
    ]
    + [
        ("command", "command", ""),
        ("command", "args", "a b"),
        ("command", "args", ["ok", 1]),
        ("command", "async", 1),
        ("command", "asyncRewake", "true"),
        ("command", "shell", "zsh"),
        ("command", "onFailure", "stop"),
        ("command", "rewakeMessage", ""),  # the CLI reads string().min(1)
        ("command", "rewakeSummary", ""),
        ("command", "rewakeSummary", 5),
        ("command", "cloud", True),
        ("http", "cloud", ["device"]),
        ("http", "url", ""),
        ("http", "url", "ftp://host/x"),
        ("http", "url", "file:///etc/passwd"),
        ("http", "url", "localhost:8080/x"),
        ("http", "url", "http://[::1"),
        ("http", "url", "https://"),
        ("http", "headers", ["Authorization"]),
        ("http", "headers", {"Authorization": 5}),
        ("http", "headers", {"": "x"}),
        ("http", "allowedEnvVars", "MY_TOKEN"),
        ("http", "onFailure", True),
        ("mcp_tool", "server", ""),
        ("mcp_tool", "tool", 5),
        ("mcp_tool", "input", ["a"]),
        ("prompt", "prompt", ""),
        ("prompt", "model", ""),
        ("prompt", "continueOnBlock", "no"),
        ("agent", "prompt", None),
        ("agent", "model", 5),
    ],
)
def test_bad_field_value_is_rejected(hook_type: str, field: str, value: object) -> None:
    with pytest.raises(cw.InvalidCandidateError, match=re.escape(repr(field))):
        hooks.validate_hooks(_block({**MINIMAL[hook_type], field: value}))


@pytest.mark.parametrize(
    ("hook_type", "field", "value"),
    [
        ("http", "command", "x"),
        ("http", "async", True),  # async is command-only
        ("mcp_tool", "onFailure", "block"),  # onFailure is command/http only
        ("prompt", "onFailure", "block"),
        ("agent", "continueOnBlock", True),  # agent hooks have no continueOnBlock
        ("prompt", "url", "https://x"),
        ("command", "prompt", "x"),
        ("command", "model", "x"),
        ("http", "rewakeMessage", "x"),  # CLI-parsed for command hooks only
        ("mcp_tool", "cloud", "skip"),  # CLI-parsed for command and http only
        ("command", "description", "x"),
    ],
)
def test_field_outside_its_type_is_rejected(hook_type: str, field: str, value: object) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="unknown keys"):
        hooks.validate_hooks(_block({**MINIMAL[hook_type], field: value}))


@pytest.mark.parametrize(
    ("hook_type", "missing"),
    [("command", "command"), ("http", "url"), ("mcp_tool", "server"), ("mcp_tool", "tool")]
    + [("prompt", "prompt"), ("agent", "prompt")],
)
def test_missing_required_field_is_rejected(hook_type: str, missing: str) -> None:
    entry = {k: v for k, v in MINIMAL[hook_type].items() if k != missing}
    with pytest.raises(cw.InvalidCandidateError, match=f"needs {missing!r}"):
        hooks.validate_hooks(_block(entry))


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "command", "command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/x.js"]},
        {"type": "http", "url": "https://h/x", "headers": {"X": "${CLAUDE_PLUGIN_ROOT}"}},
        {"type": "http", "url": "https://h/CLAUDE_PLUGIN_ROOT"},
        {
            "type": "mcp_tool",
            "server": "s",
            "tool": "t",
            "input": {"a": [{"b": "CLAUDE_PLUGIN_ROOT"}]},
        },
        {"type": "prompt", "prompt": "read ${CLAUDE_PLUGIN_ROOT}/rules.md"},
        {"type": "agent", "prompt": "x", "statusMessage": "CLAUDE_PLUGIN_ROOT"},
    ],
)
def test_plugin_root_marker_in_any_field_is_rejected(entry: dict) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="plugin"):
        hooks.validate_hooks(_block(entry))


def _nested(levels: int) -> dict:
    deep: dict = {"k": "leaf"}
    for _ in range(levels - 1):
        deep = {"k": deep}
    return deep


def test_input_depth_cap_boundary() -> None:
    entry = {**MINIMAL["mcp_tool"], "input": _nested(hooks._MAX_INPUT_DEPTH)}
    hooks.validate_hooks(_block(entry))  # exactly at the cap: accepted
    entry["input"] = _nested(hooks._MAX_INPUT_DEPTH + 1)
    with pytest.raises(cw.InvalidCandidateError, match="nested at most"):
        hooks.validate_hooks(_block(entry))


def test_deep_input_write_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """A deeply nested input is a 422 at write time, never a later unreadable file."""
    entry = {**MINIMAL["mcp_tool"], "input": _nested(3000)}
    with pytest.raises(cw.InvalidCandidateError, match="nested at most"):
        hooks.write_project_hooks(tmp_path, _block(entry), None)
    assert not cw.project_settings_path(tmp_path).exists()


def test_hand_edited_block_past_the_depth_headroom_is_422(tmp_path: Path) -> None:
    """Refused by an iterative check before any recursive walk (mask or JSON encoder)."""
    _seed(tmp_path, {"Stop": [{"hooks": [{**MINIMAL["mcp_tool"], "input": _nested(500)}]}]})
    with pytest.raises(cw.InvalidCandidateError, match="nested too deeply"):
        hooks.read_project_hooks(tmp_path)


def test_hand_edited_deep_input_reads_masked_whole(tmp_path: Path) -> None:
    # Deeper than the write cap, still inside the read headroom: masked whole, not walked.
    deep = _nested(hooks._MAX_INPUT_DEPTH + 5)
    _seed(tmp_path, {"Stop": [{"hooks": [{**MINIMAL["mcp_tool"], "input": deep}]}]})
    block, _ = hooks.read_project_hooks(tmp_path)
    assert block["Stop"][0]["hooks"][0]["input"] == S


def test_hand_edited_deep_junk_read_is_422_not_500(tmp_path: Path) -> None:
    """A deep malformed node the recursive mask cannot walk is refused, never shown raw."""
    path = cw.project_settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    # Built as text: json.dumps itself recurses. CPython 3.13 parses 2000 levels and the
    # mask then overflows; an interpreter whose parser stops first raises the same
    # InvalidCandidateError (from load_settings_json_obj), so no message is matched here.
    path.write_text('{"hooks": {"Stop": ' + "[" * 2000 + "]" * 2000 + "}}", encoding="utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        hooks.read_project_hooks(tmp_path)


# --- read: secret-bearing fields are masked -----------------------------------------


def _seed(tmp_path: Path, block: dict) -> Path:
    path = cw.project_settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": block, "model": "opus"}), encoding="utf-8")
    return path


_HTTP = {
    "type": "http",
    "url": "https://bot:pw@hooks.example.com:8443/in?team=a",
    "headers": {"Authorization": "Bearer $MY_TOKEN", "Cookie": "sid=abc", "X-Empty": ""},
    "allowedEnvVars": ["MY_TOKEN"],
    "timeout": 30,
}
_MCP = {
    "type": "mcp_tool",
    "server": "scanner",
    "tool": "scan",
    "input": {"api_key": "sk-live-1", "file_path": "src/x.py"},
}


def test_read_masks_headers_url_credentials_and_input(tmp_path: Path) -> None:
    _seed(tmp_path, {"PreToolUse": [{"matcher": "Bash", "hooks": [_HTTP, _MCP]}]})
    block, _ = hooks.read_project_hooks(tmp_path)
    http, mcp = block["PreToolUse"][0]["hooks"]
    # Every non-empty header value is masked, not only credential-named ones.
    assert http["headers"] == {"Authorization": S, "Cookie": S, "X-Empty": ""}
    # Scheme, host, port, plain path and query keys stay visible; user:pw and every query
    # value are masked.
    assert http["url"] == f"https://{S}@hooks.example.com:8443/in?team={S}"
    assert http["allowedEnvVars"] == ["MY_TOKEN"]
    assert mcp["input"] == {"api_key": S, "file_path": "src/x.py"}


def test_read_leaves_command_args_and_prompt_plain(tmp_path: Path) -> None:
    cmd = {"type": "command", "command": "node", "args": ["run.js", "--token-file=t"]}
    prompt = {"type": "prompt", "prompt": "check the api_key handling"}
    _seed(tmp_path, {"Stop": [{"hooks": [cmd, prompt]}]})
    block, _ = hooks.read_project_hooks(tmp_path)
    assert block["Stop"][0]["hooks"] == [cmd, prompt]


def test_read_masks_headers_whatever_the_type_says(tmp_path: Path) -> None:
    odd = {"type": "weird", "url": "https://u:p@h/x", "headers": {"A": "secret"}}
    _seed(tmp_path, {"Stop": [{"hooks": [odd]}]})
    block, _ = hooks.read_project_hooks(tmp_path)
    assert block["Stop"][0]["hooks"][0] == {
        "type": "weird",
        "url": f"https://{S}@h/x",
        "headers": {"A": S},
    }


def test_read_masks_malformed_nodes_structurally(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        {
            "A": {"token": "sk-1"},
            "B": [{"token": "sk-2"}, ["x"]],
            "C": [{"hooks": ["${SECRET}", {"headers": "flat"}], "auth": "sk-3"}],
        },
    )
    block, _ = hooks.read_project_hooks(tmp_path)
    assert block["A"] == {"token": S}
    assert block["B"] == [{"token": S}, ["x"]]
    assert block["C"] == [{"hooks": [S, {"headers": S}], "auth": S}]


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://h/x", "https://h/x"),
        ("http://h/a@b", "http://h/a@b"),  # an @ in the path is not userinfo
        ("https://u@h", f"https://{S}@h"),
        ("https://a@b@h/x?q#f", f"https://{S}@h/x?{S}#{S}"),
        ("https://u:p@h?x=1", f"https://{S}@h?x={S}"),
        ("no-scheme", S),  # not an http(s) URL: masked whole
        # Slack-style webhook: the workspace, channel and token segments are masked.
        (
            _SLACK_BASE + "/".join(_SLACK_PARTS),
            f"{_SLACK_BASE}{S}/{S}/{S}",
        ),
        # Token-bearing query values are masked; keys and plain path stay.
        (
            "https://api.example.com/v1/hooks/pre-tool-use?token=abc&sig=def&key=",
            f"https://api.example.com/v1/hooks/pre-tool-use?token={S}&sig={S}&key=",
        ),
        ("http://localhost:8080/hooks/pre-tool-use", "http://localhost:8080/hooks/pre-tool-use"),
        ("https://h/${HOOK_TOKEN}/in", f"https://h/{S}/in"),  # an interpolation segment
        ("https://h/hooks-for-the-whole-team/x", f"https://h/{S}/x"),  # 20+ chars
        ("https://h/a\\\\0123abcd9876/x", f"https://h/a\\\\{S}/x"),  # backslash splits
        # Punctuation next to a token does not hide it: only letters and digits count.
        ("https://h/;tok=ab12cd34/x", f"https://h/{S}/x"),
        ("https://h/key:ab12cd34ef", f"https://h/{S}"),
        ("https://h/abc!12345678", f"https://h/{S}"),
        # The boundary: 8 letters-and-digits mask, 7 do not; digits alone do not.
        ("https://h/abcd1234", f"https://h/{S}"),
        ("https://h/abc1234", "https://h/abc1234"),
        ("https://h/12345678/x", "https://h/12345678/x"),
        # A token-like query key is masked too, a plain one stays.
        ("https://h/x?SECRETKEY1234567890abc=1&team=a", f"https://h/x?{S}={S}&team={S}"),
        # A tab or line break: the raw text is not what Claude Code posts to.
        ("https:/\t/user:hunter2pass@example.com/x", S),
        ("https://h/a\tbc12345678/", S),
        ("https://h/x\n", S),
        ("https://h/x\r", S),
        (" https://h/x", S),  # urlsplit drops the space, the literal scheme differs
        # An authority that a URL parser would end elsewhere: never shown as a host.
        ("https://user:PASSWORD\\@evil.example/x", S),
        ("https://h:abc/x", S),
        ("https://user:12345678\\@evil.example/x", S),  # an all-digit password
        ("https://user:1234\\@evil.example/x", S),
        # An `@` before the backslash too: urlsplit reads the password up to the last `@`.
        (_cred_url("user", "pw", "12345678\\@evil.example/x"), S),
        ("https://tok@abcdefgh\\@evil.example/x", S),
        ("https://user%40h/x", "https://user%40h/x"),  # an encoded @ is no userinfo
        (_cred_url("user%40x", "pw", "h/x"), f"https://{S}@h/x"),
        ("https://h:/x", "https://h:/x"),  # an empty port is still a port
        ("https://[::1]:8080/x", "https://[::1]:8080/x"),
        ("https://u:p@h:8443/x", f"https://{S}@h:8443/x"),
    ],
)
def test_url_mask(url: str, shown: str) -> None:
    assert hooks._mask_url(url) == shown


SLACK = _SLACK_BASE + "/".join(_SLACK_PARTS)


@pytest.mark.parametrize(
    "resent",
    [
        # urlsplit reads the host of these as hooks.example.com, WHATWG (Claude Code)
        # reads evil.example.com: a parser-based "is the host unchanged?" check would
        # restore the token and send it to the attacker. Exact text comparison refuses.
        f"https://evil.example.com\\{S}@hooks.example.com/x",
        f"https://evil.example.com\\@hooks.example.com/{S}",
        f"https://{S}@hooks.example.com\\x",
        f"https://{S}@HOOKS.example.com/x",  # a case change is a different text too
    ],
)
def test_resent_mask_must_equal_the_stored_view_as_exact_text(tmp_path: Path, resent: str) -> None:
    stored = {"type": "http", "url": "https://tok-1234abcd@hooks.example.com/x"}
    path = _seed(tmp_path, {"Stop": [{"hooks": [stored]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][0]["url"] == f"https://{S}@hooks.example.com/x"
    shown["Stop"][0]["hooks"][0]["url"] = resent
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda u: u.replace("hooks.slack.com", "evil.example"), id="host"),
        pytest.param(lambda u: u.replace("https://", "http://"), id="scheme"),
        pytest.param(lambda u: u.replace("hooks.slack.com", "hooks.slack.com:8443"), id="port"),
        pytest.param(lambda u: u.replace("/services/", "/other/"), id="plain-path"),
        pytest.param(lambda u: u + "?x=1", id="query-key"),
    ],
)
def test_masked_url_token_never_moves(tmp_path: Path, edit) -> None:
    """A resent masked URL keeps the stored one only if every visible part is unchanged."""
    path = _seed(tmp_path, {"Stop": [{"hooks": [{"type": "http", "url": SLACK}]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    entry = shown["Stop"][0]["hooks"][0]
    assert entry["url"] == f"https://hooks.slack.com/services/{S}/{S}/{S}"
    entry["url"] = edit(entry["url"])
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_masked_slack_and_query_token_urls_round_trip(tmp_path: Path) -> None:
    query = "https://api.example.com/v1/in?token=sk-live-9&team=core"
    stored = {
        "Stop": [{"hooks": [{"type": "http", "url": SLACK}, {"type": "http", "url": query}]}]
    }
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    assert "sk-live-9" not in json.dumps(shown)
    assert "aBcD1234eFgH5678iJkL9012" not in json.dumps(shown)
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == stored


def test_fractional_timeout_is_accepted() -> None:
    """The CLI reads timeout as number().positive(), so 1.5 seconds is valid."""
    hooks.validate_hooks(_block({**MINIMAL["command"], "timeout": 1.5}))


_NON_FINITE = [float("inf"), float("-inf"), float("nan")]


@pytest.mark.parametrize("bad", _NON_FINITE)
@pytest.mark.parametrize("hook_type", list(MINIMAL))
def test_non_finite_timeout_is_refused(hook_type: str, bad: float) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="'timeout'"):
        hooks.validate_hooks(_block({**MINIMAL[hook_type], "timeout": bad}))


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_non_finite_number_inside_mcp_input_is_refused(bad: float) -> None:
    entry = {**MINIMAL["mcp_tool"], "input": {"a": [1, {"b": bad}]}}
    with pytest.raises(cw.InvalidCandidateError, match="finite numbers only"):
        hooks.validate_hooks(_block(entry))


@pytest.mark.parametrize(
    "body",
    [
        '{"Stop": [{"hooks": [{"type": "command", "command": "x", "timeout": 1e400}]}]}',
        '{"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", '
        '"input": {"n": NaN}}]}]}',
        '{"Stop": [{"hooks": [{"type": "mcp_tool", "server": "s", "tool": "t", '
        '"input": {"n": [-Infinity]}}]}]}',
    ],
)
def test_route_refuses_a_non_finite_number_and_writes_nothing(
    write_config, tmp_path, projects_root, body: str
) -> None:
    """1e400 parses to inf: a 422, never `Infinity` written into settings.json."""
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {}}', encoding="utf-8")
    before = settings.read_bytes()
    with _client(write_config, tmp_path) as c:
        h = c.get(f"{_URL}?project=alpha").json()["hash"]
        payload = (
            '{"scope": "project", "project": "alpha", "confirm": "alpha", '
            f'"hash": "{h}", "hooks": {body}}}'
        )
        resp = c.put(_URL, content=payload, headers={"Content-Type": "application/json"})
        assert resp.status_code == 422
    assert settings.read_bytes() == before


@pytest.mark.parametrize("bad", _NON_FINITE)
def test_shared_writer_fails_closed_on_a_non_finite_number(tmp_path: Path, bad: float) -> None:
    """render_json refuses Infinity/NaN for every settings surface, before any write."""
    path = tmp_path / "settings.json"
    path.write_text('{"model": "opus"}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="Infinity or NaN"):
        cw.write_settings_subtree(path, "x", {"n": bad}, cw.hash_bytes(before))
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("settings.json.*.tmp"))  # no stray temp file


@pytest.mark.parametrize("bad", [[], {}, 5, None])
def test_malformed_stored_mcp_destination_never_blocks_a_save(tmp_path: Path, bad) -> None:
    """A hand-edited `"server": []` neither 500s every save nor restores through a mask."""
    odd = {"type": "mcp_tool", "server": bad, "tool": {}, "input": {"api_key": "sk-1"}}
    path = _seed(tmp_path, {"Stop": [{"hooks": [odd]}]})
    _, h = hooks.read_project_hooks(tmp_path)
    # A resent mask cannot restore through it (it is refused, never a 500).
    resent = {"type": "mcp_tool", "server": "s", "tool": "t", "input": {"api_key": S}}
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, {"Stop": [{"hooks": [resent]}]}, h)
    assert path.read_bytes() == before
    # Saving `{}` removes it.
    hooks.write_project_hooks(tmp_path, {}, h)
    assert _hooks_on_disk(path) == {}


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            lambda b: {"PreToolUse": b["PreToolUse"] + b["Notification"], "Stop": b["Stop"]},
            id="move-between-events",
        ),
        pytest.param(lambda b: dict(reversed(list(b.items()))), id="reorder-events"),
    ],
)
def test_same_view_hooks_never_swap_values(tmp_path: Path, edit) -> None:
    """Three Slack webhooks (one view): a move refuses, a reorder keeps each hook's own URL."""
    urls = [SLACK.replace("aBcD1234", tag) for tag in ("aaaa1111", "bbbb2222", "cccc3333")]
    a, b, c = ({"type": "http", "url": u} for u in urls)
    stored = {
        "PreToolUse": [{"hooks": [a]}],
        "Stop": [{"hooks": [b]}],
        "Notification": [{"hooks": [c]}],
    }
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    sent = edit(shown)
    before = path.read_bytes()
    if set(sent) == set(stored):  # a pure reorder: every hook keeps its own value
        hooks.write_project_hooks(tmp_path, sent, h)
        on_disk = _hooks_on_disk(path)
        assert {ev: on_disk[ev] for ev in stored} == stored
    else:  # C moved under PreToolUse: it could take A's or B's place, so refuse
        with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
            hooks.write_project_hooks(tmp_path, sent, h)
        assert path.read_bytes() == before


_RM = {"type": "http", "url": SLACK, "if": "Bash(rm *)", "statusMessage": "rm"}
_GIT = {
    "type": "http",
    "url": SLACK.replace("aBcD1234", "zYxW9876"),
    "if": "Bash(git *)",
    "statusMessage": "git",
}


@pytest.mark.parametrize(
    ("stored", "reorder"),
    [
        pytest.param(
            {"PreToolUse": [{"matcher": "Bash", "hooks": [_RM, _GIT]}]},
            lambda blk: blk["PreToolUse"][0]["hooks"].reverse(),
            id="rows-in-a-group",
        ),
        pytest.param(
            {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [_RM]},
                    {"matcher": "Bash", "hooks": [_GIT]},
                ]
            },
            lambda blk: blk["PreToolUse"].reverse(),
            id="groups-in-an-event",
        ),
    ],
)
def test_reorder_never_trades_webhooks_between_hooks_with_one_url_view(
    tmp_path: Path, stored: dict, reorder
) -> None:
    """Two Slack hooks told apart only by `if`/statusMessage keep their own webhook."""
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    reorder(shown)
    hooks.write_project_hooks(tmp_path, shown, h)
    on_disk = _hooks_on_disk(path)
    by_message = {e["statusMessage"]: e["url"] for g in on_disk["PreToolUse"] for e in g["hooks"]}
    assert by_message == {"rm": _RM["url"], "git": _GIT["url"]}


def test_move_between_matchers_in_one_event_refuses(tmp_path: Path) -> None:
    """The matcher is part of where a same-view hook sits: moving it needs a retype."""
    a = {"type": "http", "url": SLACK}
    b = {"type": "http", "url": SLACK.replace("aBcD1234", "zYxW9876")}
    stored = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [a]}, {"matcher": "Edit", "hooks": [b, a]}]
    }
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["PreToolUse"][0]["hooks"].append(shown["PreToolUse"][1]["hooks"].pop(0))
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_hooks_that_differ_only_in_a_token_like_query_key_keep_their_own(tmp_path: Path) -> None:
    a = {"type": "http", "url": "https://h.example/p?SECRETKEY1234567890aaa=1"}
    b = {"type": "http", "url": "https://h.example/p?SECRETKEY1234567890bbb=1"}
    stored = {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b]}]}
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][0]["url"] == shown["SessionEnd"][0]["hooks"][0]["url"]
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == stored
    shown, h = hooks.read_project_hooks(tmp_path)
    del shown["Stop"]
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_same_destination_hidden_values_in_a_different_order(tmp_path: Path) -> None:
    """Hidden parts are compared in order: ?a=x&b=y and ?a=y&b=x are two secrets."""
    a = {"type": "http", "url": "https://h.example/p?a=x&b=y"}
    b = {"type": "http", "url": "https://h.example/p?a=y&b=x"}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][0]["url"] == shown["SessionEnd"][0]["hooks"][0]["url"]
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b]}]}
    shown, h = hooks.read_project_hooks(tmp_path)
    del shown["Stop"]  # the one left could be either: one secret mode would guess
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_set_resave_keeps_an_unmasked_neighbour_and_list_secrets(tmp_path: Path) -> None:
    """Hooks matched as a set: an unmasked one rides along, list-held secrets come back."""
    a = {"type": "http", "url": "https://h.example/x", "headers": {"Authorization": "tok-a"}}
    b = {"type": "http", "url": "https://h.example/x", "headers": {"Authorization": "tok-b"}}
    c = {"type": "http", "url": "https://h.example/x"}  # nothing to mask
    m1 = {**_MCP, "input": {"keys": ["${KEY_1}", "plain"]}}
    m2 = {**_MCP, "input": {"keys": ["${KEY_2}", "plain"]}}
    stored = {"Stop": [{"hooks": [a, b, c, m1, m2]}]}
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][3]["input"] == {"keys": [S, "plain"]}
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == stored


def test_two_slack_webhooks_resave_unchanged_and_refuse_an_edit(tmp_path: Path) -> None:
    """Two hooks with one view but different tokens: kept as a set, never swapped by an edit."""
    a = {"type": "http", "url": SLACK}
    b = {"type": "http", "url": SLACK.replace("aBcD1234", "zYxW9876"), "timeout": 5}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, shown, h)  # unchanged resave: kept as they were
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b]}]}
    shown, h = hooks.read_project_hooks(tmp_path)
    del shown["Stop"]  # B alone, edited: it could be either token
    shown["SessionEnd"][0]["hooks"][0].pop("timeout")
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


# --- write: keep-stored merge, bound to the same destination ------------------------


def _hooks_on_disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["hooks"]


def test_unchanged_resave_of_masked_read_keeps_every_secret(tmp_path: Path) -> None:
    stored = {"PreToolUse": [{"matcher": "Bash", "hooks": [_HTTP, _MCP]}]}
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == stored
    assert json.loads(path.read_text(encoding="utf-8"))["model"] == "opus"


@pytest.mark.parametrize("scope", ["user", "local"])
def test_keep_stored_merge_runs_at_user_and_local_scope(tmp_path: Path, scope: str) -> None:
    stored = {"Stop": [{"hooks": [_HTTP]}]}
    if scope == "user":
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"hooks": stored}), encoding="utf-8")
        shown, h = hooks.read_user_hooks(path)
        hooks.write_user_hooks(path, shown, h)
    else:
        path = cw.project_local_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hooks": stored}), encoding="utf-8")
        shown, h = hooks.read_project_local_hooks(tmp_path)
        hooks.write_project_local_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == stored


def test_masked_header_does_not_follow_a_changed_url(tmp_path: Path) -> None:
    """Editing the URL around a masked header must not send the stored token elsewhere."""
    path = _seed(tmp_path, {"Stop": [{"hooks": [_HTTP]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["Stop"][0]["hooks"][0]["url"] = "https://attacker.example/collect"
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before  # rejected whole: nothing written


def test_masked_url_credentials_with_a_changed_host_are_rejected(tmp_path: Path) -> None:
    path = _seed(tmp_path, {"Stop": [{"hooks": [_HTTP]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["Stop"][0]["hooks"][0]["url"] = f"https://{S}@attacker.example/in?team=a"
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_restore_matches_by_url_not_by_position(tmp_path: Path) -> None:
    """Removing the first of two http hooks restores the second's token, not the first's."""
    a = {"type": "http", "url": "https://a.example/x", "headers": {"Authorization": "tok-a"}}
    b = {"type": "http", "url": "https://b.example/x", "headers": {"Authorization": "tok-b"}}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a, b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    del shown["Stop"][0]["hooks"][0]
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [b]}]}


def test_restore_ignores_stored_hooks_of_other_types(tmp_path: Path) -> None:
    stored = {"Stop": [{"hooks": [MINIMAL["command"], MINIMAL["prompt"], _HTTP]}]}
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == stored


def test_editing_a_non_masked_field_keeps_the_masked_values(tmp_path: Path) -> None:
    """A changed timeout or statusMessage needs no retype: only masked fields are bound."""
    path = _seed(tmp_path, {"Stop": [{"hooks": [_HTTP]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["Stop"][0]["hooks"][0].update(timeout=45, statusMessage="Posting")
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path)["Stop"][0]["hooks"][0] == {
        **_HTTP,
        "timeout": 45,
        "statusMessage": "Posting",
    }


def test_same_destination_with_different_secrets(tmp_path: Path) -> None:
    """Two secrets for one endpoint: an unchanged resave keeps both, an edit needs retyping."""
    a = {"type": "http", "url": "https://h.example/p", "headers": {"Authorization": "tok-a"}}
    b = {"type": "http", "url": "https://h.example/p?x=1", "headers": {"Authorization": "tok-b"}}
    stored = {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b]}]}
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == stored
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["SessionEnd"][0]["hooks"][0]["headers"]["X-New"] = "1"  # an edit among them
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before
    shown["Stop"][0]["hooks"][0]["headers"]["Authorization"] = "tok-a"
    shown["SessionEnd"][0]["hooks"][0]["headers"]["Authorization"] = "tok-b"
    shown["SessionEnd"][0]["hooks"][0]["url"] = "https://h.example/p?x=1"
    hooks.write_project_hooks(tmp_path, shown, h)
    b_new = {**b, "headers": {"Authorization": "tok-b", "X-New": "1"}}
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b_new]}]}


def test_restore_works_after_moving_the_hook_to_another_event(tmp_path: Path) -> None:
    path = _seed(tmp_path, {"Stop": [{"hooks": [_HTTP]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, {"SessionEnd": shown["Stop"]}, h)
    assert _hooks_on_disk(path) == {"SessionEnd": [{"hooks": [_HTTP]}]}


def test_restore_skips_a_too_deep_stored_twin(tmp_path: Path) -> None:
    """A hand-edited stored hook deeper than any documented shape is never walked (no 500)."""
    path = cw.project_settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    twin = '{"type": "http", "url": "https://h.example/x", "headers": {"A": "tok"}}'
    deep = '{"k": ' * 60 + '"v"' + "}" * 60  # past _MAX_BLOCK_DEPTH, any OS can parse
    junk = '{"type": "http", "url": "https://h.example/x", "headers": ' + deep + "}"
    path.write_text(f'{{"hooks": {{"Stop": [{{"hooks": [{twin}, {junk}]}}]}}}}', encoding="utf-8")
    h = cw.hash_bytes(path.read_bytes())
    entry = {"type": "http", "url": "https://h.example/x", "headers": {"A": S}}
    hooks.write_project_hooks(tmp_path, {"Stop": [{"hooks": [entry]}]}, h)
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [{**entry, "headers": {"A": "tok"}}]}]}


def test_restore_skips_malformed_stored_nodes(tmp_path: Path) -> None:
    """A hand-edited stored block with junk around a real hook still restores that hook."""
    path = _seed(tmp_path, {"Stop": [{"hooks": ["junk", _HTTP]}, "junk"], "X": {}})
    shown, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, {"Stop": [{"hooks": [shown["Stop"][0]["hooks"][1]]}]}, h)
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [_HTTP]}]}


def test_two_stored_hooks_same_url_different_tokens_is_ambiguous(tmp_path: Path) -> None:
    """Identical views, different tokens: dropping one leaves no way to tell which is left."""
    a = {"type": "http", "url": "https://a.example/x", "headers": {"Authorization": "tok-1"}}
    b = {"type": "http", "url": "https://a.example/x", "headers": {"Authorization": "tok-2"}}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a, b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    del shown["Stop"][0]["hooks"][0]
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_two_stored_hooks_same_url_same_token_restores(tmp_path: Path) -> None:
    a = {"type": "http", "url": "https://a.example/x", "headers": {"Authorization": "tok"}}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [a]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [a]}]}


def test_masked_header_with_no_stored_value_is_rejected(tmp_path: Path) -> None:
    _seed(tmp_path, {})
    _, h = hooks.read_project_hooks(tmp_path)
    entry = {"type": "http", "url": "https://a.example/x", "headers": {"X-Key": S}}
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, _block(entry), h)


def test_editing_a_masked_hook_needs_its_masks_retyped(tmp_path: Path) -> None:
    """Any change to a hook that shows a mask is refused until every mask is retyped."""
    path = _seed(tmp_path, {"Stop": [{"hooks": [_HTTP]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    entry = shown["Stop"][0]["hooks"][0]
    entry["headers"]["Cookie"] = "sid=new"  # Authorization and the URL stay masked
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before
    # Retyping every masked value lets the edit through.
    entry["headers"]["Authorization"] = "Bearer $NEW_TOKEN"
    entry["url"] = "https://bot:pw2@hooks.example.com:8443/in?team=a"
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path)["Stop"][0]["hooks"][0]["headers"] == {
        "Authorization": "Bearer $NEW_TOKEN",
        "Cookie": "sid=new",
        "X-Empty": "",
    }


@pytest.mark.parametrize(
    ("a", "b", "edit"),
    [
        pytest.param(
            {**_MCP, "input": {"password": S}},
            {**_MCP, "input": {"password": "${TOKEN}", "n": 1}},
            lambda e: e["input"].pop("n"),
            id="input-literal-mask",
        ),
        pytest.param(
            {**_MCP, "input": {"password": "${A_TOKEN}"}},
            {**_MCP, "input": {"password": "${B_TOKEN}", "n": 1}},
            lambda e: e["input"].pop("n"),
            id="input-other-secret",
        ),
        pytest.param(
            {"type": "http", "url": "https://a:pa@h.example/p"},
            {"type": "http", "url": "https://b:pb@h.example/p?x=1"},
            lambda e: e.update(url=e["url"].split("?")[0]),
            id="url-edit-query",
        ),
        pytest.param(
            {"type": "http", "url": "https://h.example/p", "headers": {"Authorization": S}},
            {"type": "http", "url": "https://h.example/p", "headers": {"X-Key": "${B}"}},
            lambda e: e.update(headers={"Authorization": S}),
            id="header-rename-onto-neighbour",
        ),
    ],
)
def test_edited_hook_never_takes_a_neighbours_restored_value(
    tmp_path: Path, a: dict, b: dict, edit
) -> None:
    """Editing B into A's read view must not restore A's value (or A's literal mask) into B."""
    path = _seed(tmp_path, {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    del shown["Stop"]  # only the edited copy of B is sent
    edit(shown["SessionEnd"][0]["hooks"][0])
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_masked_input_does_not_follow_a_changed_tool(tmp_path: Path) -> None:
    path = _seed(tmp_path, {"Stop": [{"hooks": [_MCP]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["Stop"][0]["hooks"][0]["tool"] = "upload"
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_masked_input_does_not_follow_an_edited_sibling_argument(tmp_path: Path) -> None:
    """Same server and tool, but an edited argument next to the mask: retype required."""
    stored = {**_MCP, "input": {"url": "https://good.example/x", "token": "sk-live-2"}}
    path = _seed(tmp_path, {"Stop": [{"hooks": [stored]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][0]["input"] == {"url": "https://good.example/x", "token": S}
    shown["Stop"][0]["hooks"][0]["input"]["url"] = "https://evil.example/"
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_masked_input_key_with_no_stored_value_is_rejected(tmp_path: Path) -> None:
    path = _seed(tmp_path, {"Stop": [{"hooks": [_MCP]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["Stop"][0]["hooks"][0]["input"]["new_key"] = S
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_two_stored_mcp_hooks_same_tool_different_secret_is_ambiguous(tmp_path: Path) -> None:
    a = {**_MCP, "input": {"api_key": "sk-a"}}
    b = {**_MCP, "input": {"api_key": "sk-b"}}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a, b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    hooks.write_project_hooks(tmp_path, shown, h)  # unchanged: kept as a set
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [a, b]}]}
    shown, h = hooks.read_project_hooks(tmp_path)
    del shown["Stop"][0]["hooks"][0]  # one left: which key is it?
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_two_stored_urls_masking_the_same_are_ambiguous(tmp_path: Path) -> None:
    """Same host, different credentials: the masked URL cannot pick one, so retype it."""
    a = {"type": "http", "url": "https://u1:p1@h.example/x"}
    b = {"type": "http", "url": "https://u2:p2@h.example/x"}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a, b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][0]["url"] == shown["Stop"][0]["hooks"][1]["url"]
    hooks.write_project_hooks(tmp_path, shown, h)  # unchanged: kept as a set
    assert _hooks_on_disk(path) == {"Stop": [{"hooks": [a, b]}]}
    for edit in (
        lambda blk: blk["Stop"][0]["hooks"][1].update(timeout=9),  # any shown field binds
        lambda blk: blk["Stop"][0]["hooks"].pop(0),
    ):
        shown, h = hooks.read_project_hooks(tmp_path)
        edit(shown)
        before = path.read_bytes()
        with pytest.raises(cw.InvalidCandidateError, match="keeps masked values"):
            hooks.write_project_hooks(tmp_path, shown, h)
        assert path.read_bytes() == before


def test_matcher_with_interpolation_round_trips_unmasked(tmp_path: Path) -> None:
    """A matcher is a pattern, not a secret: an unchanged resave keeps it exactly."""
    stored = {"PreToolUse": [{"matcher": "mcp__${X}__.*", "hooks": [MINIMAL["command"]]}]}
    path = _seed(tmp_path, stored)
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown == stored
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == stored


def test_bare_mask_in_an_unmasked_field_is_refused(tmp_path: Path) -> None:
    """A mask with nothing stored behind it is never written over a real value."""
    _seed(tmp_path, {})
    _, h = hooks.read_project_hooks(tmp_path)
    entry = {**MINIMAL["command"], "statusMessage": S}
    with pytest.raises(cw.InvalidCandidateError, match="'statusMessage' holds the mask"):
        hooks.write_project_hooks(tmp_path, _block(entry), h)


def test_bare_mask_over_a_different_stored_value_is_refused(tmp_path: Path) -> None:
    """A stored hook whose field holds another value never excuses a mask in that field."""
    stored = {**MINIMAL["command"], "statusMessage": "Checking"}
    path = _seed(tmp_path, {"Stop": [{"hooks": [stored]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["Stop"][0]["hooks"][0]["statusMessage"] = S
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="'statusMessage' holds the mask"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_mask_equal_to_a_neighbours_stored_value_is_refused(tmp_path: Path) -> None:
    """An edited hook whose masked value matches ANOTHER stored hook keeps its own secret.

    A stores a literal mask in ``args`` (valid). B stores ``["${API_TOKEN}", 1]`` (invalid),
    so its read view is ``["********", 1]``. Removing the ``1`` sends B's ``args`` as
    ``["********"]``, which equals A's stored ``args``. That match must not let the mask
    overwrite B's ``${API_TOKEN}``.
    """
    a = {"type": "command", "command": "a", "args": [S]}
    b = {"type": "command", "command": "b", "args": ["${API_TOKEN}", 1]}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a, b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][1]["args"] == [S, 1]
    shown["Stop"][0]["hooks"][1]["args"] = [S]
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="'args' holds the mask"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_mask_equal_to_a_stored_value_in_another_event_is_refused(tmp_path: Path) -> None:
    """The same cross-entry match, with the neighbour under a different event."""
    a = {"type": "command", "command": "a", "statusMessage": S}
    b = {"type": "command", "command": "b", "statusMessage": {"token": "sk-live-5"}}
    path = _seed(tmp_path, {"SessionStart": [{"hooks": [a]}], "Stop": [{"hooks": [b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][0]["statusMessage"] == {"token": S}
    shown["Stop"][0]["hooks"][0]["statusMessage"] = S  # a string now, equal to A's
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="'statusMessage' holds the mask"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad_type", [5, None, ["http"], {"token": "sk-x"}])
def test_read_of_a_non_string_type_is_masked_not_500(tmp_path: Path, bad_type: object) -> None:
    _seed(tmp_path, {"Stop": [{"hooks": [{"type": bad_type, "command": "x"}]}]})
    entry = hooks.read_project_hooks(tmp_path)[0]["Stop"][0]["hooks"][0]
    assert entry["command"] == "x"
    assert "sk-x" not in json.dumps(entry)


def test_stored_value_equal_to_the_mask_does_not_lock_saves_out(tmp_path: Path) -> None:
    """A real stored value that is literally the mask still round-trips, and other edits save."""
    placeholder = {**_MCP, "input": {"password": S}}
    path = _seed(tmp_path, {"Stop": [{"hooks": [placeholder]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    shown["SessionStart"] = [{"hooks": [MINIMAL["command"]]}]
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path) == {
        "Stop": [{"hooks": [placeholder]}],
        "SessionStart": [{"hooks": [MINIMAL["command"]]}],
    }


def test_stored_plain_field_equal_to_the_mask_is_refused_with_a_way_out(tmp_path: Path) -> None:
    """A literal mask in a plain field cannot be told apart from a masked secret: refused.

    Availability only, and the message names the way out: change the value. Once it is
    changed, the save goes through.
    """
    odd = {**MINIMAL["command"], "statusMessage": S}
    path = _seed(tmp_path, {"Stop": [{"hooks": [odd]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="change it if it really is that text"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before
    shown["Stop"][0]["hooks"][0]["statusMessage"] = "Checking"
    hooks.write_project_hooks(tmp_path, shown, h)
    assert _hooks_on_disk(path)["Stop"][0]["hooks"][0]["statusMessage"] == "Checking"


def test_mask_matching_an_identical_neighbour_is_refused(tmp_path: Path) -> None:
    """The neighbour equals the edited hook in every other field: still no overwrite."""
    a = {"type": "command", "command": "run", "args": [S]}
    b = {"type": "command", "command": "run", "args": ["${API_TOKEN}", 1]}
    path = _seed(tmp_path, {"Stop": [{"hooks": [a]}], "SessionEnd": [{"hooks": [b]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    del shown["Stop"]  # only the edited copy of B is sent
    shown["SessionEnd"][0]["hooks"][0]["args"] = [S]
    before = path.read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="'args' holds the mask"):
        hooks.write_project_hooks(tmp_path, shown, h)
    assert path.read_bytes() == before


def test_read_masks_unknown_keys_and_invalid_values(tmp_path: Path) -> None:
    odd = {
        "type": "http",
        "url": ["https://u:p@h"],
        "token": "sk-live-3",
        "env": {"X": "${SECRET}"},
        "statusMessage": {"auth": "sk-4"},
        "command": "echo ${HOME}",
    }
    _seed(tmp_path, {"Stop": [{"hooks": [odd]}]})
    entry = hooks.read_project_hooks(tmp_path)[0]["Stop"][0]["hooks"][0]
    assert entry["token"] == S
    assert entry["env"] == {"X": S}
    assert entry["statusMessage"] == {"auth": S}
    assert "sk-" not in json.dumps(entry)
    # A valid plain field stays plain, even with an interpolation in it.
    assert entry["command"] == "echo ${HOME}"


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https:u:pw@h", S),  # no "//": a lenient parser still reads credentials
        ("https:///u:pw@h/x", S),  # empty authority: WHATWG reads https://u:pw@h/x
        ("https:\\\\u:pw@h", S),
        (["https://u:p@h"], [S]),  # not a string: redact_secrets masks it whole
        # urlsplit reads userinfo "good.example\" here, WHATWG reads host good.example:
        # the parsers disagree, so the URL is masked whole rather than shown either way.
        ("https://good.example\\@evil.example/p", S),
    ],
)
def test_read_masks_url_forms_a_lenient_parser_reads(
    tmp_path: Path, url: object, shown: object
) -> None:
    _seed(tmp_path, {"Stop": [{"hooks": [{"type": "http", "url": url}]}]})
    assert hooks.read_project_hooks(tmp_path)[0]["Stop"][0]["hooks"][0]["url"] == shown


def test_restored_value_is_validated_again(tmp_path: Path) -> None:
    """A stored value that the validator refuses cannot be smuggled in through a mask."""
    bad = {"type": "http", "url": "https://a/x", "headers": {"A": "${CLAUDE_PLUGIN_ROOT}"}}
    _seed(tmp_path, {"Stop": [{"hooks": [bad]}]})
    shown, h = hooks.read_project_hooks(tmp_path)
    assert shown["Stop"][0]["hooks"][0]["headers"] == {"A": S}
    with pytest.raises(cw.InvalidCandidateError, match="plugin"):
        hooks.write_project_hooks(tmp_path, shown, h)


def test_validate_never_contacts_the_url_or_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation is structure only: no socket is opened for an http or mcp_tool hook."""
    import socket

    def _refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("validate_hooks opened a network connection")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    hooks.validate_hooks(
        {"Stop": [{"hooks": [MINIMAL["http"], MINIMAL["mcp_tool"], MINIMAL["agent"]]}]}
    )


# --- routes + dashboard injection ---------------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"
_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_URL = "/api/config-write/hooks"


def _client(write_config, tmp_path: Path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    return TestClient(create_app(load_config(cfg)))


def test_route_get_masks_and_put_round_trips(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    stored = {"PreToolUse": [{"matcher": "Bash", "hooks": [_HTTP, _MCP]}]}
    settings.write_text(json.dumps({"hooks": stored}), encoding="utf-8")
    with _client(write_config, tmp_path) as c:
        read = c.get(f"{_URL}?project=alpha").json()
        assert "Bearer $MY_TOKEN" not in json.dumps(read)
        assert "sk-live-1" not in json.dumps(read)
        assert "bot:pw" not in json.dumps(read)
        body = {"scope": "project", "project": "alpha", "confirm": "alpha"}
        resp = c.put(_URL, json={**body, "hooks": read["hooks"], "hash": read["hash"]})
        assert resp.status_code == 200
        # A changed URL around a masked header is a clean 422, never a 500.
        read = c.get(f"{_URL}?project=alpha").json()
        read["hooks"]["PreToolUse"][0]["hooks"][0]["url"] = "https://elsewhere.example/"
        resp = c.put(_URL, json={**body, "hooks": read["hooks"], "hash": read["hash"]})
        assert resp.status_code == 422
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"] == stored


@pytest.mark.parametrize("hook_type", list(MINIMAL))
def test_route_saves_every_type_with_all_fields(
    write_config, tmp_path, projects_root, hook_type: str
) -> None:
    block = _block({**MINIMAL[hook_type], **DOCUMENTED_FIELDS[hook_type]}, "PostToolUse")
    with _client(write_config, tmp_path) as c:
        body = {"scope": "project", "project": "alpha", "confirm": "alpha", "hooks": block}
        assert c.put(_URL, json=body).status_code == 200
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    assert json.loads(settings.read_text(encoding="utf-8"))["hooks"] == block


def test_dashboard_injects_hook_types_from_the_validator(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path) as c:
        html = c.get("/").text
    m = re.search(r"const HOOK_TYPES = (\[[^\]]*\]);", html)
    assert m, "HOOK_TYPES const not found in the rendered dashboard"
    assert json.loads(m.group(1)) == list(hooks.HOOK_TYPES)
    for field in ("cm-hook-type", "cm-hook-url", "cm-hook-server", "cm-hook-tool"):
        assert f'data-test="{field}"' in html
    assert 'data-test="cm-hook-prompt"' in html
