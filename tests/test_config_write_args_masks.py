"""TASK-558: the value of a secret-named flag in MCP ``args`` is masked in all three editors.

Covers the MCP tab, the frontmatter of ``SKILL.md`` and the frontmatter of a subagent file, in
the forms ``--api-key=X``, ``--token=X`` and the separate items ``--token``, ``X``, written
as a block, a flow and a quoted list. Each surface also has save round trips: an unchanged
save keeps the stored bytes, an edit to another field keeps the value, and a save that cannot
bind a mask is refused with the file unchanged. The fixture values are plain placeholders.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_mcp as mcp
from clauster import config_write_mcp_cli as mcp_cli
from clauster import config_write_skills as sk
from clauster import config_write_subagents as sub
from clauster.app import create_app
from clauster.config import load_config

MASK = cw.REDACTION_SENTINEL
HELD_A = "qv-arg-one"
HELD_B = "qv-arg-two"
FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


# --- the shared rule -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "shown"),
    [
        (["--api-key=x"], [f"--api-key={MASK}"]),
        (["--token=x"], [f"--token={MASK}"]),
        (["--API_KEY=x"], [f"--API_KEY={MASK}"]),
        (["-password=x"], [f"-password={MASK}"]),
        (["--client-secret=a=b"], [f"--client-secret={MASK}"]),
        (["--token", "x"], ["--token", MASK]),
        (["--auth-token", "x", "--port", "1"], ["--auth-token", MASK, "--port", "1"]),
        (["--token", "x", "--api-key", "y"], ["--token", MASK, "--api-key", MASK]),
        (["serve", "--apikey=x", "--verbose"], ["serve", f"--apikey={MASK}", "--verbose"]),
    ],
)
def test_the_value_of_a_secret_named_flag_is_masked(args: list, shown: list) -> None:
    assert cw.mask_secret_args(args) == shown


@pytest.mark.parametrize(
    "args",
    [
        ["--port=1", "--name", "x"],
        ["--author", "x"],  # the secret vocabulary leaves `author` out
        ["--token"],  # no value after it
        ["--token="],  # an empty value hides nothing
        ["--token", ""],
        ["--token", "--verbose"],  # the next item is another flag
        ["--no-auth", "server.py"],  # a --no-* flag takes no value
        ["token=x"],  # not a flag
        ["--token x"],  # one item with a space is not a flag and a value
        ["--token abc", "next"],  # so the item after it is not its value
        ["--NO-auth", "server.py"],  # the --no-* check ignores letter case
        ["-"],
        ["--"],
        ["--token", 1],  # not a string: a validator refuses it
        [None, {"a": 1}, ["--token=x"]],
    ],
)
def test_other_args_are_left_as_they_are(args: list) -> None:
    assert cw.mask_secret_args(args) == args


def test_the_rule_does_not_change_the_list_it_reads() -> None:
    args = ["--token", "x", "--api-key=y"]
    cw.mask_secret_args(args)
    assert args == ["--token", "x", "--api-key=y"]


# --- the MCP tab -----------------------------------------------------------------------------

_ARGS = ["serve", f"--api-key={HELD_A}", "--token", HELD_B, "--port", "1"]
_SHOWN = ["serve", f"--api-key={MASK}", "--token", MASK, "--port", "1"]


def _mcp_file(tmp_path: Path, **extra: object) -> tuple[Path, bytes]:
    path = tmp_path / ".mcp.json"
    entry = {"command": "/bin/foo", "args": list(_ARGS), **extra}
    raw = json.dumps({"mcpServers": {"srv": entry}}, indent=2).encode()
    path.write_bytes(raw)
    return path, raw


def _mcp_view(project: Path) -> tuple[dict, str]:
    """Read the MCP view of a server written by :func:`_mcp_file`, proving its args masked."""
    view, file_hash = mcp.read_project_servers(project)
    assert view["srv"]["args"] == _SHOWN
    assert HELD_A not in json.dumps(view) and HELD_B not in json.dumps(view)
    return view, file_hash


def test_the_mcp_view_masks_each_form(tmp_path: Path) -> None:
    _mcp_file(tmp_path)
    _mcp_view(tmp_path)


def test_a_reference_in_args_keeps_its_whole_mask(tmp_path: Path) -> None:
    # `redact_secrets` already shows these as one mask, and that view does not change.
    entry = {"command": "/bin/foo", "args": ["--token=${VAR}", "https://u:p@h.example.com"]}
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"srv": entry}}))
    view, _hash = mcp.read_project_servers(tmp_path)
    assert view["srv"]["args"] == [MASK, MASK]


def test_an_unchanged_whole_map_save_keeps_the_stored_args(tmp_path: Path) -> None:
    path, _raw = _mcp_file(tmp_path)
    view, file_hash = _mcp_view(tmp_path)
    mcp.write_project_servers(tmp_path, view, file_hash)
    assert json.loads(path.read_text())["mcpServers"]["srv"]["args"] == _ARGS


def test_an_edit_to_another_field_keeps_the_masked_args(tmp_path: Path) -> None:
    path, _raw = _mcp_file(tmp_path, env={"MODE": "fast"})
    view, _hash = _mcp_view(tmp_path)
    entry = {**view["srv"], "timeout": 5000}
    assert entry["env"] == {"MODE": MASK}
    mcp.write_project_server_entry(tmp_path, "srv", entry, op="edit")
    saved = json.loads(path.read_text())["mcpServers"]["srv"]
    assert saved["args"] == _ARGS
    assert saved["env"] == {"MODE": "fast"}
    assert saved["timeout"] == 5000
    assert MASK not in path.read_text()


def test_the_user_and_local_writers_keep_the_masked_args(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    project = tmp_path / "p"
    project.mkdir()
    entry = {"command": "/bin/foo", "args": list(_ARGS)}
    local_block = {str(project): {"mcpServers": {"l": entry}}}
    claude_json.write_text(json.dumps({"mcpServers": {"srv": entry}, "projects": local_block}))

    shown = mcp.read_user_servers(claude_json)["srv"]
    assert shown["args"] == _SHOWN
    mcp.write_user_server_entry(claude_json, "srv", {**shown, "timeout": 9}, op="edit")
    local = mcp.read_project_local_servers(claude_json, project)["l"]
    assert local["args"] == _SHOWN
    mcp.write_project_local_server_entry(
        claude_json, project, "l", {**local, "timeout": 9}, op="edit"
    )

    saved = json.loads(claude_json.read_text())
    assert saved["mcpServers"]["srv"]["args"] == _ARGS
    assert saved["projects"][str(project)]["mcpServers"]["l"]["args"] == _ARGS
    assert MASK not in claude_json.read_text()


@pytest.mark.parametrize(
    "sent",
    [
        # A partly masked item whose visible text changed.
        ["serve", f"--key={MASK}", "--token", MASK, "--port", "1"],
        # A partly masked item moved to another index.
        [f"--api-key={MASK}", "serve", "--token", MASK, "--port", "1"],
        # An item added in front shifts every index.
        ["new", "serve", f"--api-key={MASK}", "--token", MASK, "--port", "1"],
        # The separate value now follows another flag.
        ["serve", f"--api-key={MASK}", "--other", MASK, "--port", "1"],
        # The separate value moved to where it would be handed to another flag.
        ["serve", f"--api-key={MASK}", "--token", "1", "--port", MASK],
        # A mask inside text that was never masked.
        ["serve", f"--api-key={MASK}", "--token", MASK, "--port", f"1{MASK}"],
        # A partly masked item past the end of the stored list.
        [*_SHOWN, f"--extra={MASK}"],
    ],
)
def test_a_mask_that_cannot_be_bound_is_refused(tmp_path: Path, sent: list) -> None:
    path, raw = _mcp_file(tmp_path)
    entry = {"command": "/bin/foo", "args": sent}
    with pytest.raises(cw.InvalidCandidateError) as caught:
        mcp.write_project_server_entry(tmp_path, "srv", entry, op="edit")
    assert path.read_bytes() == raw
    assert HELD_A not in str(caught.value) and HELD_B not in str(caught.value)


def test_a_changed_command_refuses_a_masked_args_item(tmp_path: Path) -> None:
    path, raw = _mcp_file(tmp_path)
    view, _hash = _mcp_view(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="command or args"):
        mcp.write_project_server_entry(
            tmp_path, "srv", {**view["srv"], "command": "/bin/other"}, op="edit"
        )
    assert path.read_bytes() == raw


def test_a_new_server_with_a_masked_args_item_is_refused(tmp_path: Path) -> None:
    path, raw = _mcp_file(tmp_path)
    entry = {"command": "/bin/foo", "args": [f"--token={MASK}"]}
    with pytest.raises(cw.InvalidCandidateError, match="no stored value"):
        mcp.write_project_server_entry(tmp_path, "other", entry, op="add")
    assert path.read_bytes() == raw


def test_a_whole_map_save_refuses_a_changed_masked_item(tmp_path: Path) -> None:
    path, raw = _mcp_file(tmp_path)
    view, file_hash = _mcp_view(tmp_path)
    view["srv"]["args"][1] = f"--other={MASK}"
    with pytest.raises(cw.InvalidCandidateError, match="no stored value"):
        mcp.write_project_servers(tmp_path, view, file_hash)
    assert path.read_bytes() == raw


def test_a_stored_arg_that_holds_the_mask_text_round_trips(tmp_path: Path) -> None:
    # Not a secret-named flag: it shows as stored, so an unchanged save keeps it.
    entry = {"command": "/bin/foo", "args": [f"a{MASK}b"]}
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"srv": entry}}))
    view, _hash = mcp.read_project_servers(tmp_path)
    assert view["srv"]["args"] == [f"a{MASK}b"]
    mcp.write_project_server_entry(tmp_path, "srv", view["srv"], op="edit")
    assert json.loads(path.read_text())["mcpServers"]["srv"]["args"] == [f"a{MASK}b"]


def test_a_masked_entry_never_goes_to_the_cli(tmp_path: Path) -> None:
    _mcp_file(tmp_path)
    view, _hash = _mcp_view(tmp_path)
    assert mcp_cli.entry_needs_direct_write(view["srv"]) is True


def test_the_mcp_routes_mask_and_round_trip(write_config, tmp_path, projects_root) -> None:
    project = projects_root / "alpha"
    path, _raw = _mcp_file(project)
    cfg = write_config(
        f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n"
        "config_write:\n  enabled: true\n"
    )
    with TestClient(create_app(load_config(cfg))) as c:
        got = c.get("/api/config-write/mcp?project=alpha")
        assert got.status_code == 200
        assert HELD_A not in got.text and HELD_B not in got.text
        entry = got.json()["servers"]["srv"]
        assert entry["args"] == _SHOWN
        done = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "edit",
                "name": "srv",
                "entry": {**entry, "timeout": 7000},
            },
        )
        assert done.status_code == 200, done.text
    saved = json.loads(path.read_text())["mcpServers"]["srv"]
    assert saved["args"] == _ARGS and saved["timeout"] == 7000


# --- the frontmatter forms, for the Skills and Subagents editors ------------------------------

#: Each stored spelling of ``args``, at the indent of a server in a mapping.
_FORMS = {
    "block-plain": f"    args:\n      - --api-key={HELD_A}\n      - --token\n      - {HELD_B}\n",
    "block-double": (
        f'    args:\n      - "--api-key={HELD_A}"\n      - "--token"\n      - "{HELD_B}"\n'
    ),
    "block-single": (
        f"    args:\n      - '--token={HELD_A}'\n      - '--token'\n      - '{HELD_B}'\n"
    ),
    "flow-plain": f"    args: [--api-key={HELD_A}, --token, {HELD_B}]\n",
    "flow-double": f'    args: ["--api-key={HELD_A}", "--token", "{HELD_B}"]\n',
    "flow-single": f"    args: ['--token={HELD_A}', '--token', '{HELD_B}']\n",
}


# --- the Skills editor -----------------------------------------------------------------------


def _skill(form: str, description: str = "d") -> str:
    return (
        f"---\ndescription: {description}\nmcpServers:\n  gh:\n    command: npx\n"
        f"{_FORMS[form]}---\nRun it.\n"
    )


def _place_skill(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _skill_view(tmp_path: Path) -> tuple[str, str]:
    content, file_hash, _exists, _note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert HELD_A not in content and HELD_B not in content
    return content, file_hash


def _save_skill(tmp_path: Path, content: str, file_hash: str) -> None:
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: content}, expected_hash=file_hash)


@pytest.mark.parametrize("form", list(_FORMS))
def test_a_skill_masks_each_form(tmp_path: Path, form: str) -> None:
    _place_skill(tmp_path, _skill(form))
    content, _hash = _skill_view(tmp_path)
    assert HELD_A not in content and HELD_B not in content
    assert MASK in content
    assert "--token" in content  # the flag stays readable


@pytest.mark.parametrize(
    ("form", "line"),
    [
        ("block-plain", f"      - --api-key={MASK}\n"),
        ("block-plain", f"      - {MASK}\n"),
        ("block-double", f'      - "--api-key={MASK}"\n'),
        ("block-double", f'      - "{MASK}"\n'),
        ("block-single", f"      - '--token={MASK}'\n"),
        ("flow-plain", f"    args: [--api-key={MASK}, --token, {MASK}]\n"),
        ("flow-double", f'    args: ["--api-key={MASK}", "--token", "{MASK}"]\n'),
        ("flow-single", f"    args: ['--token={MASK}', '--token', '{MASK}']\n"),
    ],
)
def test_a_skill_masks_only_the_value_in_place(tmp_path: Path, form: str, line: str) -> None:
    _place_skill(tmp_path, _skill(form))
    assert line in _skill_view(tmp_path)[0]


def test_a_skill_masks_an_escaped_args_value(tmp_path: Path) -> None:
    stored = (
        "---\nmcpServers:\n  gh:\n    command: npx\n"
        '    args: ["--token", "\\x71v-arg-two"]\n---\nRun it.\n'
    )
    _place_skill(tmp_path, stored)
    content, file_hash = _skill_view(tmp_path)
    assert "x71v" not in content and HELD_B not in content
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    _save_skill(tmp_path, content, file_hash)
    assert path.read_bytes() == stored.encode()


def test_a_skill_reads_args_behind_an_alias(tmp_path: Path) -> None:
    stored = (
        f"---\nx-base: &base [--token, {HELD_B}]\nmcpServers:\n  gh:\n    command: npx\n"
        "    args: *base\n---\nRun it.\n"
    )
    _place_skill(tmp_path, stored)
    assert HELD_B not in _skill_view(tmp_path)[0]


@pytest.mark.parametrize("form", list(_FORMS))
def test_an_unchanged_skill_save_keeps_the_bytes(tmp_path: Path, form: str) -> None:
    stored = _skill(form)
    path = _place_skill(tmp_path, stored)
    content, file_hash = _skill_view(tmp_path)
    _save_skill(tmp_path, content, file_hash)
    assert path.read_bytes() == stored.encode()


@pytest.mark.parametrize("form", list(_FORMS))
def test_a_skill_edit_to_another_field_keeps_the_values(tmp_path: Path, form: str) -> None:
    path = _place_skill(tmp_path, _skill(form))
    content, file_hash = _skill_view(tmp_path)
    _save_skill(tmp_path, content.replace("description: d", "description: new"), file_hash)
    assert path.read_bytes() == _skill(form, "new").encode()


def test_a_crlf_skill_round_trips(tmp_path: Path) -> None:
    stored = _skill("block-plain").replace("\n", "\r\n")
    path = _place_skill(tmp_path, stored)
    content, file_hash = _skill_view(tmp_path)
    assert HELD_A not in content and HELD_B not in content
    _save_skill(tmp_path, content, file_hash)
    assert path.read_bytes() == stored.encode()


@pytest.mark.parametrize(
    ("form", "old", "new"),
    [
        # The flag on the masked line changed.
        ("flow-plain", "--api-key=", "--other="),
        ("block-plain", "--api-key=", "--other="),
        # The flag that names the separate value changed, on another line.
        ("block-plain", "      - --token\n", "      - --other\n"),
        # The other item of a flow list changed.
        ("flow-double", '"--token"', '"--other"'),
    ],
)
def test_a_skill_save_that_cannot_bind_a_mask_is_refused(
    tmp_path: Path, form: str, old: str, new: str
) -> None:
    stored = _skill(form)
    path = _place_skill(tmp_path, stored)
    content, file_hash = _skill_view(tmp_path)
    assert old in content
    with pytest.raises(cw.InvalidCandidateError) as caught:
        _save_skill(tmp_path, content.replace(old, new), file_hash)
    assert path.read_bytes() == stored.encode()
    assert HELD_A not in str(caught.value) and HELD_B not in str(caught.value)


def test_a_new_skill_line_with_a_mask_is_refused(tmp_path: Path) -> None:
    stored = _skill("block-plain")
    path = _place_skill(tmp_path, stored)
    content, file_hash = _skill_view(tmp_path)
    grown = content.replace("      - --token\n", f"      - --extra={MASK}\n      - --token\n")
    with pytest.raises(cw.InvalidCandidateError):
        _save_skill(tmp_path, grown, file_hash)
    assert path.read_bytes() == stored.encode()


def test_args_outside_mcp_servers_are_not_read_as_server_args(tmp_path: Path) -> None:
    # Only the `mcpServers` field holds server args: another `args` key keeps the line view.
    stored = "---\ndescription: d\nmetadata:\n  args: [--token, keep-me]\n---\nRun it.\n"
    _place_skill(tmp_path, stored)
    assert "[--token, keep-me]" in _skill_view(tmp_path)[0]


def test_only_a_server_args_list_is_read_as_args(tmp_path: Path) -> None:
    # An `args` key under `hooks`, and another list under a server, are not server args.
    stored = (
        "---\ndescription: d\nhooks:\n  Stop:\n    - hooks:\n        - type: command\n"
        "          command: run\n          args: [--token, keep-hook]\n"
        "mcpServers:\n  gh:\n    command: npx\n    other: [--token, keep-list]\n---\nRun it.\n"
    )
    _place_skill(tmp_path, stored)
    content = _skill_view(tmp_path)[0]
    assert "keep-hook" in content and "keep-list" in content


def test_a_cyclic_args_header_ends(tmp_path: Path) -> None:
    stored = (
        "---\nmcpServers: &m\n  gh:\n    command: npx\n"
        "    args: &a [--token, *a]\n    x: *m\n---\n"
    )
    _place_skill(tmp_path, stored)
    assert "--token" in _skill_view(tmp_path)[0]


def test_an_args_list_shared_by_many_servers_is_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real = cw.mask_secret_args

    def counting(args: list) -> list:
        nonlocal calls
        calls += 1
        return real(args)

    monkeypatch.setattr(cw, "mask_secret_args", counting)
    servers = "".join(f"  s{i}:\n    command: npx\n    args: *a\n" for i in range(50))
    stored = f"---\nx-base: &a [--token, {HELD_B}]\nmcpServers:\n{servers}---\nRun it.\n"
    _place_skill(tmp_path, stored)
    _skill_view(tmp_path)
    assert calls == 1


def test_a_value_masked_by_two_rules_shows_one_mask(tmp_path: Path) -> None:
    # Under `env` every value is masked, and this `args` key flags the same node again.
    stored = (
        "---\nmcpServers:\n  gh:\n    command: npx\n"
        f"    env: {{args: [--token, {HELD_B}]}}\n---\nRun it.\n"
    )
    _place_skill(tmp_path, stored)
    assert f"    env: {{args: [{MASK}, {MASK}]}}\n" in _skill_view(tmp_path)[0]


# --- the Subagents editor --------------------------------------------------------------------


def _agent(form: str, description: str = "d") -> str:
    # A subagent lists its inline servers; one level deeper than the skill's mapping.
    block = textwrap.indent(_FORMS[form], "  ")
    return (
        f"---\nname: my-agent\ndescription: {description}\nmcpServers:\n  - gh:\n"
        f"      command: npx\n{block}---\nbody\n"
    )


def _place_agent(tmp_path: Path, text: str) -> tuple[Path, Path]:
    root = tmp_path / "agents"
    root.mkdir(exist_ok=True)
    path = root / "my-agent.md"
    path.write_bytes(text.encode("utf-8"))
    return root, path


def _agent_view(root: Path) -> dict:
    doc = sub._read_agent(root, "my-agent", "project")
    assert doc["editable"] is True
    for field in (doc["content"], json.dumps(doc["frontmatter"])):
        assert HELD_A not in field and HELD_B not in field
    return doc


@pytest.mark.parametrize("form", list(_FORMS))
def test_a_subagent_masks_each_form(tmp_path: Path, form: str) -> None:
    root, _path = _place_agent(tmp_path, _agent(form))
    doc = _agent_view(root)
    args = doc["frontmatter"]["mcpServers"][0]["gh"]["args"]
    assert args[1:] == ["--token", MASK]
    assert args[0].endswith(f"={MASK}")


@pytest.mark.parametrize("form", list(_FORMS))
def test_a_subagent_header_pyyaml_refuses_hides_each_form(tmp_path: Path, form: str) -> None:
    # A tab before a comment: PyYAML refuses it, and Claude Code's YAML 1.2 reads it.
    stored = _agent(form).replace("command: npx\n", "command: npx\t# note\n")
    root, path = _place_agent(tmp_path, stored)
    doc = sub._read_agent(root, "my-agent", "project")
    for field in (doc["content"], json.dumps(doc["frontmatter"])):
        assert HELD_A not in field and HELD_B not in field
    with pytest.raises(cw.InvalidCandidateError):
        sub._write_agent(root, "my-agent", doc["content"], doc["hash"])
    assert path.read_bytes() == stored.encode()


#: Text that plain SafeLoader, the editor masker's parser, cannot compose. Claude Code's
#: YAML 1.2 reads it, and so does the seam's FrontmatterLoader since TASK-485.
_NO_COMPOSE = "x-note: y\t# note\n"


@pytest.mark.parametrize(
    "header",
    [
        # The key spelled with an escape or a line continuation.
        f'"mcp\\x53ervers":\n  - gh:\n      args: [--token, {HELD_B}]\n{_NO_COMPOSE}',
        f'"mcp\\u0053ervers":\n  - gh:\n      args: [--token, {HELD_B}]\n{_NO_COMPOSE}',
        f'{{"mcp\\\n  Servers": [{{gh: {{args: [--token, {HELD_B}]}}}}]}}\n{_NO_COMPOSE}',
        # The flag name spelled with an escape or a line continuation.
        f'mcpServers:\n  - gh:\n      args: ["--api\\x2dkey={HELD_B}"]\n{_NO_COMPOSE}',
        f'mcpServers:\n  - gh:\n      args: ["--t\\u006fken", {HELD_B}]\n{_NO_COMPOSE}',
        f'mcpServers:\n  - gh:\n      args: ["--to\\\n        ken", {HELD_B}]\n{_NO_COMPOSE}',
        # A binary tag can hold the value too.
        f"x-v: !!binary {HELD_B}\n{_NO_COMPOSE}",
        # A value that no flag names, by its place under the server.
        f"mcpServers:\n  - gh:\n      env: {{MODE: {HELD_B}}}\n{_NO_COMPOSE}",
        f"mcpServers:\n  - gh:\n      headers: {{X-A: {HELD_B}}}\n{_NO_COMPOSE}",
        f"mcpServers:\n  - gh:\n      headersHelper: echo {HELD_B}\n{_NO_COMPOSE}",
    ],
)
def test_a_header_that_does_not_compose_is_masked_whole(tmp_path: Path, header: str) -> None:
    stored = f"---\nname: my-agent\ndescription: d\n{header}---\nbody\n"
    root, path = _place_agent(tmp_path, stored)
    doc = sub._read_agent(root, "my-agent", "project")
    assert HELD_B not in doc["content"] and HELD_B not in json.dumps(doc["frontmatter"])
    assert "name: my-agent\n" not in doc["content"]
    with pytest.raises(cw.InvalidCandidateError):
        sub._write_agent(root, "my-agent", doc["content"], doc["hash"])
    assert path.read_bytes() == stored.encode()


@pytest.mark.parametrize(
    "item",
    [f'"--api\\x2dkey={HELD_B}"', f'"--t\\u006fken", {HELD_B}', f"--token, {HELD_B}"],
)
def test_a_refused_header_that_composes_reads_the_loaded_flag(tmp_path: Path, item: str) -> None:
    # `DEBUG: yes` composes, and the shared seam refuses it (a YAML 1.1-only boolean).
    stored = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  - gh:\n      command: npx\n"
        f"      args: [{item}]\n      env:\n        DEBUG: yes\n---\nbody\n"
    )
    root, path = _place_agent(tmp_path, stored)
    doc = sub._read_agent(root, "my-agent", "project")
    assert HELD_B not in doc["content"]
    with pytest.raises(cw.InvalidCandidateError):
        sub._write_agent(root, "my-agent", doc["content"], doc["hash"])
    assert path.read_bytes() == stored.encode()


#: Headers the shared seam refuses where PyYAML and Claude Code's YAML 1.2 can disagree, or
#: where a by-place value sits beside a YAML 1.1-only spelling. Each hides ``HELD_B``.
_READ_ANOTHER_WAY = [
    # A flow `?`: a mapping key to PyYAML, a plain value to YAML 1.2.
    f"mcpServers:\n  - gh:\n      command: c\n      args: [--token, ?{HELD_B}]\n",
    # A quoted value that goes on at column 0: the CLI's retry reads a real server.
    f'description2: "d: e\nmcpServers:\n  - gh:\n      command: c\n'
    f'      args: [--token, {HELD_B}]\nz: end"\n',
    f'description2: "d: e\nmcpServers:\n  - gh:\n      command: c\n      args:\n'
    f'        - --password\n        - {HELD_B}\nz: end"\n',
    f'description2: "d: e\nmcpServers:\n  - gh:\n      command: c\n'
    f'      env: {{FOO: {HELD_B}}}\nz: end"\n',
    # A by-place value beside a YAML 1.1-only boolean, which is all the seam refuses.
    f"mcpServers:\n  - gh:\n      command: c\n      env: {{DEBUG: yes, FOO: {HELD_B}}}\n",
    f"mcpServers:\n  - gh:\n      type: http\n      url: https://e.example.com\n"
    f"      headers: {{X-Key: {HELD_B}, X-On: yes}}\n",
    # The old line masks hid a deeper line after `{token:`; the header view masks that text
    # first, so the old view must still hold for the lines it masked.
    f'mcpServers:\n  - url: "https://h.example.com/?q={{token:"\n    note: {HELD_B}\n'
    "    env: {DEBUG: yes}\n",
    f'mcpServers:\n  - args: [--token, "{{api_key:"]\n    command: run --password {HELD_B}\n'
    "    env: {DEBUG: yes}\n",
    # The deeper line is masked by both views, differently: the new view alone would show
    # the key `X`, which the old view hid with the rest of the line.
    f'mcpServers:\n  - url: "https://h.example.com/?q={{token:"\n'
    f"    env: {{X: {HELD_B}, D: yes}}\n",
]


def _hides_at_least(raw: str, old: str, new: str) -> bool:
    """Whether the line view ``new`` shows nothing that the line view ``old`` hid.

    An oracle kept apart from the code: ``new`` is ``old``, the whole-line mask, or each of
    its unmasked pieces is text that a piece of ``old`` shows too.
    """
    if old == raw or new == old or new == sk._masked_whole(raw):
        return True
    shown = old.split(MASK)
    return all(any(piece in s for s in shown) for piece in new.split(MASK) if piece)


@pytest.mark.parametrize(
    ("raw", "old", "new", "view"),
    [
        # Both views agree, or the old view left the line alone.
        ("a: b\n", "a: b\n", f"a: {MASK}\n", f"a: {MASK}\n"),
        ("t: x\n", f"t: {MASK}\n", f"t: {MASK}\n", f"t: {MASK}\n"),
        # The new view left the line as stored: the old view is kept.
        ("    note: x\n", f"    {MASK}\n", "    note: x\n", f"    {MASK}\n"),
        # The new view masks more (a credential URL masked whole): it is kept.
        (
            "url: https://u:p@h.example.com/p\n",
            f"url: {MASK}@h.example.com/p\n",
            f"url: {MASK}\n",
            f"url: {MASK}\n",
        ),
        # The new view would show a key the old view hid: the whole line is masked.
        ("    env: {X: v}\n", f"    {MASK}\n", f"    env: {{X: {MASK}}}\n", f"{MASK}\n"),
        # Pieces that the old view shows, but out of order, are not trusted.
        ("abXYcd\n", f"ab{MASK}cd\n", f"{MASK}cd{MASK}ab{MASK}\n", f"{MASK}\n"),
    ],
)
def test_each_line_keeps_what_the_line_masks_hid(raw: str, old: str, new: str, view: str) -> None:
    assert sk._no_less_masked(raw, old, new) == view


@pytest.mark.parametrize("header", _READ_ANOTHER_WAY)
def test_a_header_read_another_way_hides_the_value(tmp_path: Path, header: str) -> None:
    stored = f"---\nname: my-agent\ndescription: d\n{header}---\nbody\n"
    with pytest.raises(cw.InvalidCandidateError):
        sub.parse_frontmatter(stored)  # the fallback is the path under test
    root, path = _place_agent(tmp_path, stored)
    doc = sub._read_agent(root, "my-agent", "project")
    assert HELD_B not in doc["content"] and HELD_B not in json.dumps(doc["frontmatter"])
    with pytest.raises(cw.InvalidCandidateError):
        sub._write_agent(root, "my-agent", doc["content"], doc["hash"])
    assert path.read_bytes() == stored.encode()


@pytest.mark.parametrize("header", _READ_ANOTHER_WAY)
def test_a_skill_never_shows_a_line_the_line_masks_hide(tmp_path: Path, header: str) -> None:
    # The args mask can remove the `{api_key:` that made the line masker hide a deeper
    # line; that line keeps the line masks' view.
    stored = f"---\ndescription: d\n{header}---\nRun it.\n"
    _place_skill(tmp_path, stored)
    content = _skill_view(tmp_path)[0]
    before = cw.redact_secret_lines(stored)
    lines = zip(stored.splitlines(), before.splitlines(), content.splitlines(), strict=True)
    for raw, old, new in lines:
        assert _hides_at_least(raw, old, new), new


def _old_fallback(text: str) -> str:
    """Return the view origin/main gave a header the seam refuses: the line masks alone."""
    match = sub._FRONTMATTER_RE.match(text)
    assert match is not None
    return text[: match.start(1)] + cw.redact_secret_lines(match.group(1)) + text[match.end(1) :]


#: Every refused header in this file, each a whole stored file. A tab after a QUOTED value
#: is still refused; after an unquoted one it loads since TASK-485 (see _TAB_LOADS).
_REFUSED = [
    *(_agent(form).replace("command: npx\n", 'command: "npx"\t# note\n') for form in _FORMS),
    *(
        f"---\nname: my-agent\ndescription: d\n{header}---\nbody\n"
        for header in [*_READ_ANOTHER_WAY, f'token: "{HELD_A}"\t# c\nmcpServers: [\n']
    ),
    f"---\nname: my-agent\ndescription: d\napi_key: |\n  {HELD_A}\nx: [\t# n\n---\nbody\n",
    "---\nname: my-agent\nmcpServers:\n  a: {command: x}\nmcpServers:\n  b: {command: y}\n---\n",
]

#: The refused headers this file used before TASK-485: a tab after an unquoted value. They
#: load now, and the editor's masker, which composes with plain SafeLoader, still cannot
#: read them, so they must be masked no less than the line masks alone masked them.
_TAB_LOADS = [
    *(_agent(form).replace("command: npx\n", "command: npx\t# note\n") for form in _FORMS),
]


def test_a_tab_header_that_loads_through_the_retry_keeps_its_secret_masked(
    tmp_path: Path,
) -> None:
    # The unclosed `[` fails YAML 1.2 too, so Claude Code reads its re-quoting retry, where
    # `token` holds the tab and the comment (checked against eemeli/yaml). Before TASK-485
    # the tab made this tier refuse the header; now it reads what the CLI reads, and the
    # view writes the masked value in the retry's quoted spelling.
    stored = (
        f"---\nname: my-agent\ndescription: d\ntoken: {HELD_A}\t# c\nmcpServers: [\n---\nbody\n"
    )
    assert sub.parse_frontmatter(stored)[0]["token"] == f"{HELD_A}\t# c"
    root, _path = _place_agent(tmp_path, stored)
    doc = sub._read_agent(root, "my-agent", "project")
    assert HELD_A not in doc["content"] and HELD_A not in json.dumps(doc["frontmatter"])
    assert f'token: "{MASK}"\n' in doc["content"]


@pytest.mark.parametrize("stored", _REFUSED)
def test_no_refused_header_is_masked_less_than_before(tmp_path: Path, stored: str) -> None:
    # Differential against origin/main's fallback: every line it masked is masked now, and
    # no value it hid shows now.
    with pytest.raises(cw.InvalidCandidateError):
        sub.parse_frontmatter(stored)
    _assert_masked_no_less(tmp_path, stored)


@pytest.mark.parametrize("stored", _TAB_LOADS)
def test_a_header_only_the_tab_loader_reads_is_masked_no_less(tmp_path: Path, stored: str) -> None:
    sub.parse_frontmatter(stored)  # loads since TASK-485
    _assert_masked_no_less(tmp_path, stored)


def _assert_masked_no_less(tmp_path: Path, stored: str) -> None:
    """Assert the subagent view of ``stored`` masks no less than origin/main's fallback."""
    root, _path = _place_agent(tmp_path, stored)
    now = sub._read_agent(root, "my-agent", "project")["content"]
    before = _old_fallback(stored)
    lines = stored.splitlines()
    assert len(now.splitlines()) == len(lines) == len(before.splitlines())
    for raw, old, new in zip(lines, before.splitlines(), now.splitlines(), strict=True):
        assert _hides_at_least(raw, old, new), new
    for value in (HELD_A, HELD_B):
        if value not in before:
            assert value not in now


@pytest.mark.parametrize(
    "header",
    [
        # Does not compose, and holds nothing the editor masks by place.
        "name: my-agent\ndescription: --token=x\t# note\n",
        # Composes to nothing: a header of comments only.
        "# only a comment\n",
        # Composes, is refused, and no server has a secret-named flag (hyphens are not flags).
        "name: my-auth-agent\ndescription: re-authenticate\nmcpServers:\n  - a:\n"
        "      command: x\n      env:\n        DEBUG: yes\n",
    ],
)
def test_another_broken_header_stays_readable_for_repair(tmp_path: Path, header: str) -> None:
    root, _path = _place_agent(tmp_path, f"---\n{header}---\nbody\n")
    doc = sub._read_agent(root, "my-agent", "project")
    assert header.splitlines()[0] in doc["content"]


def test_an_oversized_refused_header_is_masked_whole_without_a_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sk, "MAX_FILE_BYTES", 40)
    stored = f'---\nname: my-agent\ndescription: "d"\t# note\nx-pad: {"p" * 40}\n---\nbody\n'
    root, _path = _place_agent(tmp_path, stored)
    assert "name: my-agent\n" not in sub._read_agent(root, "my-agent", "project")["content"]


@pytest.mark.parametrize("form", list(_FORMS))
def test_an_unchanged_subagent_save_keeps_the_bytes(tmp_path: Path, form: str) -> None:
    stored = _agent(form)
    root, path = _place_agent(tmp_path, stored)
    doc = _agent_view(root)
    sub._write_agent(root, "my-agent", doc["content"], doc["hash"])
    assert path.read_bytes() == stored.encode()


@pytest.mark.parametrize("form", list(_FORMS))
def test_a_subagent_edit_to_another_field_keeps_the_values(tmp_path: Path, form: str) -> None:
    root, path = _place_agent(tmp_path, _agent(form))
    doc = _agent_view(root)
    edited = doc["content"].replace("description: d", "description: new")
    sub._write_agent(root, "my-agent", edited, doc["hash"])
    assert path.read_bytes() == _agent(form, "new").encode()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (f'"--api-key={MASK}"', f'"--other={MASK}"'),  # the masked item changed
        ('"--token"', '"--other"'),  # the flag of the separate value changed
        ("command: npx", "command: other"),  # the program changed
    ],
)
def test_a_subagent_save_that_cannot_bind_a_mask_is_refused(
    tmp_path: Path, old: str, new: str
) -> None:
    stored = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  - gh:\n      command: npx\n"
        f'      args: ["--api-key={HELD_A}", "--token", "{HELD_B}"]\n---\nbody\n'
    )
    root, path = _place_agent(tmp_path, stored)
    doc = _agent_view(root)
    assert old in doc["content"]
    with pytest.raises(cw.InvalidCandidateError) as caught:
        sub._write_agent(root, "my-agent", doc["content"].replace(old, new), doc["hash"])
    assert path.read_bytes() == stored.encode()
    assert HELD_A not in str(caught.value) and HELD_B not in str(caught.value)
