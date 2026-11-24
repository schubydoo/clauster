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


@pytest.mark.parametrize(
    "header",
    [
        "name: my-agent\ndescription: --token=x\t# note\n",  # no mcpServers
        "name: my-agent\nmcpServers:\n  - a:\n      command: x\t# note\n",  # no secret flag
    ],
)
def test_another_broken_header_stays_readable_for_repair(tmp_path: Path, header: str) -> None:
    root, _path = _place_agent(tmp_path, f"---\n{header}---\nbody\n")
    doc = sub._read_agent(root, "my-agent", "project")
    assert "name: my-agent\n" in doc["content"]


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
