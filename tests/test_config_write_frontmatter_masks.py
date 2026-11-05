"""Masked frontmatter in the subagent and skill editors, and keep-stored on save (TASK-427).

The editor views mask a secret value as ``********``. A save that sends the mask back
keeps the stored value only where the mask can be bound to it, and refuses everything
else (fail closed): a mask is never written as the literal ``********``. The Markdown body
of a subagent is never masked or rewritten.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_skills as sk
from clauster import config_write_subagents as sub
from clauster.app import create_app
from clauster.config import load_config

MASK = cw.REDACTION_SENTINEL
QUOTED = f'"{MASK}"'

_BODY = "You are a helper.\napi_key: fake-BODY\nUse ${HOME} here.\n"

_STDIO = (
    "---\nname: my-agent\ndescription: d\nmcpServers:\n"
    "  - github\n"
    "  - local:\n"
    "      command: npx\n"
    "      args: [-y, '@acme/mcp']\n"
    "      env:\n"
    "        GH_PAT: fake-pat-SECRET\n"
    "        DEBUG: '1'\n"
    "---\n" + _BODY
)

_REMOTE = (
    "---\nname: my-agent\ndescription: d\nmcpServers:\n"
    "  - remote:\n"
    "      type: http\n"
    "      url: https://good.example.com/mcp\n"
    "      headers:\n"
    "        Authorization: Bearer tok-SECRET\n"
    "---\n" + _BODY
)

_CRED_URL = (
    "---\nname: my-agent\ndescription: d\nmcpServers:\n"
    "  - remote:\n"
    "      type: http\n"
    "      url: https://user:pw-SECRET@good.example.com/mcp\n"
    "---\n" + _BODY
)


def _agents(project: Path) -> Path:
    return sub.project_agents_dir(project)


def _place(project: Path, text: str, name: str = "my-agent") -> Path:
    agents = _agents(project)
    agents.mkdir(parents=True, exist_ok=True)
    path = agents / f"{name}.md"
    path.write_bytes(text.encode("utf-8"))
    return path


def _load(project: Path, name: str = "my-agent") -> dict:
    return sub.read_project_agent(project, name)


def _save(project: Path, content: str, file_hash: str | None, name: str = "my-agent") -> None:
    sub.write_project_agent(project, name, content, file_hash)


# --- subagents: what the editor shows -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "hidden"),
    [(_STDIO, "fake-pat-SECRET"), (_REMOTE, "tok-SECRET"), (_CRED_URL, "pw-SECRET")],
)
def test_subagent_editor_masks_frontmatter_secrets_and_leaves_the_body(
    tmp_path: Path, text: str, hidden: str
) -> None:
    _place(tmp_path, text)
    doc = _load(tmp_path)
    assert hidden not in doc["content"]
    assert hidden not in json.dumps(doc["frontmatter"])
    assert QUOTED in doc["content"]
    # The body is never masked, even where it looks like a secret.
    assert doc["content"].endswith("---\n" + _BODY)


def test_subagent_editor_masks_every_inline_env_value_and_keeps_structure_visible(
    tmp_path: Path,
) -> None:
    _place(tmp_path, _STDIO)
    content = _load(tmp_path)["content"]
    # Every env value is masked, as on the MCP tab; the command and args stay visible.
    assert f"        GH_PAT: {QUOTED}\n" in content
    assert f"        DEBUG: {QUOTED}\n" in content
    assert "      command: npx\n" in content
    assert "      args: [-y, '@acme/mcp']\n" in content
    assert "  - github\n" in content


def test_a_secret_like_server_name_masks_only_its_secret_values(tmp_path: Path) -> None:
    # PR 1804 follow-up: in the list form a server called `oauth-gw` was a secret hint, so
    # its own `type` and `url` showed as the mask. Only secret values are masked now.
    text = _REMOTE.replace("  - remote:", "  - oauth-gw:")
    _place(tmp_path, text)
    doc = _load(tmp_path)
    assert "      type: http\n" in doc["content"]
    assert "      url: https://good.example.com/mcp\n" in doc["content"]
    server = doc["frontmatter"]["mcpServers"][0]["oauth-gw"]
    assert (server["type"], server["url"]) == ("http", "https://good.example.com/mcp")
    assert server["headers"]["Authorization"] == MASK


def test_subagent_editor_masks_top_level_env_and_secret_keys(tmp_path: Path) -> None:
    text = (
        "---\nname: my-agent\ndescription: d\napi_token: fake-TOP\n"
        "env:\n  PLAIN: hello\n  COUNT: 3\n---\nbody\n"
    )
    _place(tmp_path, text)
    content = _load(tmp_path)["content"]
    assert content == (
        f"---\nname: my-agent\ndescription: d\napi_token: {QUOTED}\n"
        f"env:\n  PLAIN: {QUOTED}\n  COUNT: 3\n---\nbody\n"
    )


def test_subagent_editor_masks_a_hook_url_token_and_its_headers(tmp_path: Path) -> None:
    text = (
        "---\nname: my-agent\ndescription: d\nhooks:\n  PostToolUse:\n"
        "    - matcher: Bash\n      hooks:\n        - type: http\n"
        "          url: https://hooks.example.com/notify?key=k-SECRET\n"
        "          headers:\n            X-Custom: v-SECRET\n---\nbody\n"
    )
    _place(tmp_path, text)
    content = _load(tmp_path)["content"]
    assert "k-SECRET" not in content and "v-SECRET" not in content
    assert f'url: "https://hooks.example.com/notify?key={MASK}"' in content


def test_the_subagent_listing_masks_a_secret_shaped_description(tmp_path: Path) -> None:
    _place(tmp_path, "---\nname: my-agent\ndescription: uses ${API_TOKEN}\n---\nbody\n")
    (entry,) = [a for a in sub.list_project_agents(tmp_path) if a["name"] == "my-agent"]
    assert entry["description"] == MASK


def test_a_crlf_file_keeps_its_line_endings_and_a_bom_is_kept(tmp_path: Path) -> None:
    crlf = _STDIO.replace("\n", "\r\n")
    _place(tmp_path, crlf)
    doc = _load(tmp_path)
    assert "fake-pat-SECRET" not in doc["content"]
    assert doc["content"].count("\r\n") == crlf.count("\r\n")
    _save(tmp_path, doc["content"], doc["hash"])
    assert (_agents(tmp_path) / "my-agent.md").read_bytes() == crlf.encode("utf-8")
    # A byte order mark ahead of the fence stays, and the header behind it is still masked.
    _place(tmp_path, "\ufeff" + _STDIO, name="bom-agent")
    shown = _load(tmp_path, "bom-agent")["content"]
    assert shown.startswith("\ufeff---\n")
    assert "fake-pat-SECRET" not in shown


def test_a_header_only_the_cli_retry_loads_is_shown_quoted_and_masked(tmp_path: Path) -> None:
    # `description: Use when: x` is not valid YAML; Claude Code re-quotes it on a retry.
    # The masked view is that retry text, and an unchanged save keeps the secret.
    text = "---\nname: my-agent\ndescription: Use when: x\napi_token: fake-RETRY\n---\nbody\n"
    path = _place(tmp_path, text)
    doc = _load(tmp_path)
    assert doc["content"] == (
        f'---\nname: my-agent\ndescription: "Use when: x"\napi_token: {QUOTED}\n---\nbody\n'
    )
    _save(tmp_path, doc["content"], doc["hash"])
    frontmatter, body = sub.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert frontmatter["api_token"] == "fake-RETRY"
    assert (frontmatter["description"], body) == ("Use when: x", "body\n")


def test_an_unloadable_header_is_masked_line_by_line(tmp_path: Path) -> None:
    text = "---\nname: my-agent\npassword: not-a-real-secret\nbad: [unclosed\n---\nbody ${X}\n"
    _place(tmp_path, text)
    content = _load(tmp_path)["content"]
    assert "not-a-real-secret" not in content
    assert content.endswith("---\nbody ${X}\n")  # the body is still never masked


def test_a_secret_behind_an_anchor_is_not_shown_at_all(tmp_path: Path) -> None:
    # The mask cannot be written in place (the alias needs the anchor), so the editor
    # shows nothing rather than the secret.
    text = (
        "---\nname: my-agent\ndescription: d\napi_token: &t fake-ANCHOR\n"
        "other_token: *t\n---\nbody\n"
    )
    _place(tmp_path, text)
    doc = _load(tmp_path)
    assert (doc["content"], doc["editable"]) == ("", False)
    assert doc["converted"] == sub.MASK_UNAVAILABLE_NOTE
    assert "fake-ANCHOR" not in json.dumps(doc)


# --- subagents: what a save keeps -----------------------------------------------------------


@pytest.mark.parametrize("text", [_STDIO, _REMOTE, _CRED_URL])
def test_an_unchanged_save_keeps_every_masked_value_byte_for_byte(
    tmp_path: Path, text: str
) -> None:
    path = _place(tmp_path, text)
    doc = _load(tmp_path)
    _save(tmp_path, doc["content"], doc["hash"])
    assert path.read_bytes() == text.encode("utf-8")


def test_a_converted_mapping_form_file_keeps_its_masked_values(tmp_path: Path) -> None:
    # The old mapping form loads converted to the list form; the masks in it are bound to
    # the stored definitions by server name.
    text = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  srv:\n    command: npx\n"
        "    env:\n      GH_PAT: fake-pat-SECRET\n---\nbody\n"
    )
    path = _place(tmp_path, text)
    doc = _load(tmp_path)
    assert doc["converted"] == sub.MCP_SERVERS_CONVERSION_NOTE
    assert "fake-pat-SECRET" not in doc["content"]
    _save(tmp_path, doc["content"], doc["hash"])
    assert path.read_text(encoding="utf-8") == (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n  - srv:\n      command: npx\n"
        "      env:\n        GH_PAT: fake-pat-SECRET\n---\nbody\n"
    )


def test_an_edit_elsewhere_keeps_the_masks_and_writes_the_edit(tmp_path: Path) -> None:
    path = _place(tmp_path, _STDIO)
    doc = _load(tmp_path)
    edited = doc["content"].replace("description: d", "description: changed")
    edited = edited.replace("You are a helper.", "You are a careful helper.")
    _save(tmp_path, edited, doc["hash"])
    assert path.read_text(encoding="utf-8") == _STDIO.replace(
        "description: d", "description: changed"
    ).replace("You are a helper.", "You are a careful helper.")


def test_a_retyped_value_writes_through(tmp_path: Path) -> None:
    path = _place(tmp_path, _STDIO)
    doc = _load(tmp_path)
    edited = doc["content"].replace(f"GH_PAT: {QUOTED}", "GH_PAT: fake-pat-NEW")
    _save(tmp_path, edited, doc["hash"])
    frontmatter, _body = sub.parse_frontmatter(path.read_text(encoding="utf-8"))
    env = frontmatter["mcpServers"][1]["local"]["env"]
    assert env == {"GH_PAT": "fake-pat-NEW", "DEBUG": "1"}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("command: npx", "command: npx2"),  # a new program would receive the stored env
        ("args: [-y, '@acme/mcp']", "args: [-y, '@evil/mcp']"),
        ("args: [-y, '@acme/mcp']", "args: [-y, '@acme/mcp', --x]"),
    ],
)
def test_a_masked_stdio_env_value_is_refused_when_command_or_args_change(
    tmp_path: Path, old: str, new: str
) -> None:
    path = _place(tmp_path, _STDIO)
    doc = _load(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="command or args changed"):
        _save(tmp_path, doc["content"].replace(old, new), doc["hash"])
    assert path.read_bytes() == _STDIO.encode("utf-8")  # nothing written


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/mcp",
        # Python's urlsplit reads host `good`, the WHATWG parser Node uses reads `evil`.
        "https://evil.example.com\\@good.example.com/mcp",
        "https://GOOD.example.com/mcp",  # any change to the text, letter case included
        "https://good.example.com:443/mcp",
    ],
)
def test_a_masked_header_is_refused_when_the_server_url_text_changes(
    tmp_path: Path, url: str
) -> None:
    path = _place(tmp_path, _REMOTE)
    doc = _load(tmp_path)
    edited = doc["content"].replace("url: https://good.example.com/mcp", f"url: '{url}'")
    with pytest.raises(cw.InvalidCandidateError, match="type or a url changed"):
        _save(tmp_path, edited, doc["hash"])
    assert path.read_bytes() == _REMOTE.encode("utf-8")


def test_a_masked_credentialed_url_cannot_be_moved_to_another_server(tmp_path: Path) -> None:
    path = _place(tmp_path, _CRED_URL)
    doc = _load(tmp_path)
    renamed = doc["content"].replace("  - remote:", "  - other:")
    with pytest.raises(cw.InvalidCandidateError, match="nothing is stored to keep"):
        _save(tmp_path, renamed, doc["hash"])
    assert path.read_bytes() == _CRED_URL.encode("utf-8")


def test_a_mask_with_no_stored_value_is_refused_not_written(tmp_path: Path) -> None:
    # A new file cannot keep anything, and a mask moved to a new key has nothing behind it.
    with pytest.raises(cw.InvalidCandidateError, match="no stored value"):
        _save(tmp_path, f"---\nname: my-agent\ndescription: d\ntoken: {QUOTED}\n---\nb\n", None)
    assert not (_agents(tmp_path) / "my-agent.md").exists()
    path = _place(tmp_path, "---\nname: my-agent\ndescription: d\ntoken: fake-T\n---\nb\n")
    doc = _load(tmp_path)
    moved = doc["content"].replace("token:", "token2:")
    with pytest.raises(cw.InvalidCandidateError, match="no stored value"):
        _save(tmp_path, moved, doc["hash"])
    assert b"fake-T" in path.read_bytes()


def test_a_masked_server_reference_is_kept_only_in_its_place(tmp_path: Path) -> None:
    text = "---\nname: my-agent\ndescription: d\nmcpServers:\n  - ${SERVER}\n  - github\n---\nb\n"
    path = _place(tmp_path, text)
    doc = _load(tmp_path)
    assert f"  - {QUOTED}\n" in doc["content"]
    _save(tmp_path, doc["content"], doc["hash"])
    assert path.read_bytes() == text.encode("utf-8")
    doc = _load(tmp_path)
    reordered = doc["content"].replace("  - github\n", "") + ""
    with pytest.raises(cw.InvalidCandidateError, match="no stored server name"):
        _save(tmp_path, reordered, doc["hash"])


def test_a_masked_hook_url_is_kept_only_while_the_hook_is_unchanged(tmp_path: Path) -> None:
    text = (
        "---\nname: my-agent\ndescription: d\nhooks:\n  PostToolUse:\n"
        "    - matcher: Bash\n      hooks:\n        - type: http\n"
        "          url: https://hooks.example.com/notify?key=k-SECRET\n---\nbody\n"
    )
    path = _place(tmp_path, text)
    doc = _load(tmp_path)
    _save(tmp_path, doc["content"], doc["hash"])
    frontmatter, _body = sub.parse_frontmatter(path.read_text(encoding="utf-8"))
    hook = frontmatter["hooks"]["PostToolUse"][0]["hooks"][0]
    assert hook["url"] == "https://hooks.example.com/notify?key=k-SECRET"
    doc = _load(tmp_path)
    moved = doc["content"].replace("hooks.example.com", "evil.example.com")
    with pytest.raises(cw.InvalidCandidateError, match="frontmatter 'hooks'"):
        _save(tmp_path, moved, doc["hash"])


def test_a_mask_on_an_aliased_value_is_refused(tmp_path: Path) -> None:
    # Typed by hand: the editor never shows a mask behind an anchor. Both keys are stored,
    # so the merge keeps two different values for one node, which no text can hold.
    stored = "---\nname: my-agent\ndescription: d\napi_token: sk-A\nother_token: sk-B\n---\nb\n"
    path = _place(tmp_path, stored)
    doc = _load(tmp_path)
    aliased = (
        f"---\nname: my-agent\ndescription: d\napi_token: &t {QUOTED}\nother_token: *t\n---\nb\n"
    )
    with pytest.raises(cw.InvalidCandidateError, match="cannot be put back in its place"):
        _save(tmp_path, aliased, doc["hash"])
    assert path.read_bytes() == stored.encode("utf-8")


def test_unrelated_anchors_and_merge_keys_do_not_block_a_kept_mask(tmp_path: Path) -> None:
    text = (
        "---\nname: my-agent\ndescription: d\nbase: &b {a: 1}\nextra: {<<: *b, c: 2}\n"
        "label: &l plain\nagain: *l\napi_token: fake-K\n---\nb\n"
    )
    path = _place(tmp_path, text)
    doc = _load(tmp_path)
    assert "fake-K" not in doc["content"]
    _save(tmp_path, doc["content"], doc["hash"])
    assert path.read_bytes() == text.encode("utf-8")


def test_a_mask_with_no_stored_value_is_refused_for_servers_and_a_lost_twin(
    tmp_path: Path,
) -> None:
    new = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n"
        f"  - srv:\n      command: npx\n      env:\n        K: {QUOTED}\n---\nb\n"
    )
    with pytest.raises(cw.InvalidCandidateError, match="nothing is stored to keep"):
        _save(tmp_path, new, None)
    # A stored file that defines a server twice cannot say which one a mask belongs to.
    twice = (
        "---\nname: my-agent\ndescription: d\nmcpServers:\n"
        "  - srv:\n      command: npx\n      env:\n        K: k-1\n"
        "  - srv:\n      command: npx\n      env:\n        K: k-2\n---\nb\n"
    )
    path = _place(tmp_path, twice)
    doc = _load(tmp_path)
    one = doc["content"].split("  - srv:")
    kept_one = "  - srv:".join(one[:2]) + "---\nb\n"
    with pytest.raises(cw.InvalidCandidateError, match="defines it twice"):
        _save(tmp_path, kept_one, doc["hash"])
    assert path.read_bytes() == twice.encode("utf-8")


def test_a_masked_save_over_an_unloadable_stored_file_is_refused(tmp_path: Path) -> None:
    # The view masked the stored header line by line; with nothing loadable on disk, no
    # mask can be bound, even after the operator repairs the YAML.
    # Nested, so Claude Code's re-quoting retry (top-level lines only) cannot repair it.
    # The line masker leaves a mask INSIDE the value (`run ********`), which no structural
    # merge reads as a mask: without the refusal it was written as typed, losing `${API_KEY}`.
    stored = "---\nname: my-agent\ndescription: run ${API_KEY}\nm:\n  n: [x\n---\nb\n"
    path = _place(tmp_path, stored)
    doc = _load(tmp_path)
    assert f"description: run {MASK}\n" in doc["content"]
    repaired = doc["content"].replace("n: [x", "n: [x]")
    with pytest.raises(cw.InvalidCandidateError, match="stored frontmatter does not load"):
        _save(tmp_path, repaired, doc["hash"])
    assert path.read_bytes() == stored.encode("utf-8")


def test_hooks_without_a_mask_pass_through_a_masked_save(tmp_path: Path) -> None:
    text = (
        "---\nname: my-agent\ndescription: d\napi_token: fake-H\nhooks:\n  Stop:\n"
        "    - hooks:\n        - type: command\n          command: echo done\n---\nb\n"
    )
    path = _place(tmp_path, text)
    doc = _load(tmp_path)
    _save(tmp_path, doc["content"].replace("echo done", "echo finished"), doc["hash"])
    assert path.read_text(encoding="utf-8") == text.replace("echo done", "echo finished")


def test_a_mask_inside_an_ordered_map_is_refused(tmp_path: Path) -> None:
    path = _place(tmp_path, "---\nname: my-agent\ndescription: d\napi_token: fake-A\n---\nb\n")
    doc = _load(tmp_path)
    omap = f"---\nname: my-agent\ndescription: d\nextra: !!omap\n  - token: {QUOTED}\n---\nb\n"
    with pytest.raises(cw.InvalidCandidateError, match="ordered map"):
        _save(tmp_path, omap, doc["hash"])
    assert b"fake-A" in path.read_bytes()


def test_a_bom_file_saved_without_its_bom_keeps_the_masked_values(tmp_path: Path) -> None:
    # The editor shows the BOM, and its save strips it (`_syncAgentName`): the stored text
    # must be read without it too, or the masks could never be bound.
    path = _place(tmp_path, "\ufeff" + _STDIO)
    doc = _load(tmp_path)
    sent = doc["content"].removeprefix("\ufeff").replace("description: d", "description: e")
    _save(tmp_path, sent, doc["hash"])
    assert path.read_text(encoding="utf-8") == _STDIO.replace("description: d", "description: e")


def test_masks_bind_only_to_the_stored_bytes_the_editor_loaded(tmp_path: Path) -> None:
    # The bytes used for the merge must hash to the loaded hash, even when the file under
    # the writer lock is back to those bytes (changed, then changed back).
    _place(tmp_path, _STDIO)
    doc = _load(tmp_path)
    other = _STDIO.replace("fake-pat-SECRET", "fake-pat-INTERIM").encode("utf-8")
    with pytest.raises(cw.StaleConfigWriteError):
        sub._keep_masked_values(doc["content"], other, doc["hash"], "my-agent")


def test_a_subagent_that_fails_its_checks_once_restored_names_no_size(tmp_path: Path) -> None:
    # The masked text fits the 64 KiB cap; with the long stored value kept, it does not.
    # The check's own message would name the restored size, which, less the masked size,
    # gives the length of the hidden value.
    stored_value = "s" * 40_000
    text = f"---\nname: my-agent\ndescription: d\napi_token: {stored_value}\n---\nbody\n"
    _place(tmp_path, text)
    doc = _load(tmp_path)
    grown = doc["content"] + "x" * 30_000 + "\n"
    with pytest.raises(cw.InvalidCandidateError, match="once its masked values are kept") as ex:
        _save(tmp_path, grown, doc["hash"])
    assert re.findall(r"\d+", str(ex.value)) == []


def test_a_masked_save_over_a_changed_file_is_stale(tmp_path: Path) -> None:
    path = _place(tmp_path, _STDIO)
    doc = _load(tmp_path)
    path.write_bytes(_STDIO.replace("fake-pat-SECRET", "fake-pat-OTHER").encode("utf-8"))
    with pytest.raises(cw.StaleConfigWriteError):
        _save(tmp_path, doc["content"], doc["hash"])
    with pytest.raises(cw.StaleConfigWriteError):
        _save(tmp_path, doc["content"], None)


def test_a_mask_text_that_is_not_a_whole_value_is_written_as_typed(tmp_path: Path) -> None:
    # Only a value that IS the mask is a mask; the masker never produces a partial one here.
    path = _place(tmp_path, "---\nname: my-agent\ndescription: d\n---\nb\n")
    doc = _load(tmp_path)
    typed = doc["content"].replace("description: d", f"description: 'see {MASK}'")
    _save(tmp_path, typed, doc["hash"])
    assert f"description: 'see {MASK}'" in path.read_text(encoding="utf-8")


# --- the rewrite helpers --------------------------------------------------------------------


def test_rewrite_quotes_a_value_that_has_no_stored_spelling() -> None:
    text = "---\nname: x\ndescription: d\nk: |\n  multi\n  line\nz: 1\n---\nb\n"
    target = {"name": "x", "description": "d", "k": 'a "q"\nb', "z": 1}
    out = sub._rewrite_values(text, target, stored=None)
    assert out == '---\nname: x\ndescription: d\nk: "a \\"q\\"\\nb"\nz: 1\n---\nb\n'


def test_rewrite_quotes_a_stored_spelling_that_reads_differently_in_its_new_place() -> None:
    # The stored `a, b` is one plain string in block context and two items in a flow list,
    # so the first try (the stored spelling) loads wrong and the quoted spelling is used.
    stored = "---\nname: x\ndescription: d\nv: a, b\n---\nb\n"
    text = f"---\nname: x\ndescription: d\nk: [x, {QUOTED}]\n---\nb\n"
    target = {"name": "x", "description": "d", "k": ["x", "a, b"]}
    out = sub._rewrite_values(text, target, stored=stored)
    assert out == '---\nname: x\ndescription: d\nk: [x, "a, b"]\n---\nb\n'


def test_rewrite_refuses_what_it_cannot_place() -> None:
    text = "---\nname: x\ndescription: d\nk: v\n---\nb\n"
    # A new key, a container in place of a scalar, and a value with no one-line spelling.
    assert sub._rewrite_values(text, {"name": "x", "description": "d"}, stored=None) is None
    target = {"name": "x", "description": "d", "k": ["v"]}
    assert sub._rewrite_values(text, target, stored=None) is None
    target = {"name": "x", "description": "d", "k": b"v"}
    assert sub._rewrite_values(text, target, stored=None) is None
    assert sub._rewrite_values("no fence\n", {}, stored=None) is None


def test_rewrite_helpers_fail_closed_on_odd_input() -> None:
    assert sub._rewrite_values("---\nm:\n  n: [x\n---\nb\n", {}, stored=None) is None
    assert sub._scalar_spellings(None) == {} and sub._scalar_spellings("no fence\n") == {}
    assert not sub._loads_as("no fence\n", {}, "")
    assert not sub._loads_as("---\nname: x\n---\nb\n", {"name": "x"}, "other\n")
    assert not sub._loads_as("---\nm:\n  n: [x\n---\nb\n", {}, "b\n")
    assert [sub._render_scalar(v) for v in (5, True, None, 1.5, b"x")] == [
        "5",
        "true",
        "null",
        "1.5",
        None,
    ]


def test_same_yaml_value_tells_types_apart() -> None:
    assert cw.same_yaml_value({"a": [1, "x"]}, {"a": [1, "x"]})
    assert not cw.same_yaml_value({"a": 1}, {"a": True})
    assert not cw.same_yaml_value([1], [1, 2])
    assert not cw.same_yaml_value({"a": 1}, {"b": 1})
    assert cw.same_yaml_value(float("nan"), float("nan"))
    assert not cw.same_yaml_value(float("nan"), 1.0)


# --- subagents: through the routes ----------------------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"
_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"


def _client(write_config, tmp_path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    return TestClient(create_app(load_config(cfg)))


def test_route_get_masks_and_put_keeps_or_refuses(write_config, tmp_path, projects_root) -> None:
    project = projects_root / "alpha"
    path = _place(project, _STDIO)
    url = "/api/config-write/subagents/my-agent"
    body = {"scope": "project", "project": "alpha", "confirm": "alpha"}
    with _client(write_config, tmp_path) as c:
        got = c.get(f"{url}?project=alpha")
        assert got.status_code == 200
        assert "fake-pat-SECRET" not in got.text
        doc = got.json()
        moved = doc["content"].replace("command: npx", "command: node")
        refused = c.put(url, json={**body, "content": moved, "hash": doc["hash"]})
        assert refused.status_code == 422
        assert "fake-pat-SECRET" not in refused.text
        assert path.read_bytes() == _STDIO.encode("utf-8")
        kept = c.put(url, json={**body, "content": doc["content"], "hash": doc["hash"]})
        assert kept.status_code == 200, kept.text
    assert path.read_bytes() == _STDIO.encode("utf-8")


# --- skills: masked lines are kept or refused -----------------------------------------------


def _skill(tmp_path: Path, text: str, name: str = "s") -> Path:
    sk.write_skill(tmp_path, name, {sk.SKILL_FILENAME: text}, expected_hash=None)
    return tmp_path / "skills" / name / sk.SKILL_FILENAME


def _skill_view(tmp_path: Path, name: str = "s") -> tuple[str, str]:
    view, file_hash, _exists, _note = sk.read_skill_file_for_edit(tmp_path, name)
    return view, file_hash


@pytest.mark.parametrize(
    "stored",
    [
        # The data-loss proof: these lines are masked on read, and an unchanged save wrote
        # the literal mask over them before TASK-427.
        "---\ndescription: d\n---\nRun it.\napi_key: fake-SECRET\n",
        "---\ndescription: d\napi_key: fake-SECRET\n---\nRun it.\n",
        "---\ndescription: d\n---\nRun ${CLAUDE_SKILL_DIR}/scripts/x.sh\n",
        "---\r\ndescription: d\r\n---\r\ntoken = fake-CRLF\r\n",
    ],
)
def test_skill_unchanged_save_keeps_every_masked_line(tmp_path: Path, stored: str) -> None:
    path = _skill(tmp_path, stored)
    view, file_hash = _skill_view(tmp_path)
    assert MASK in view
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: view}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


def test_skill_edit_elsewhere_keeps_the_masked_lines(tmp_path: Path) -> None:
    stored = "---\ndescription: d\n---\nRun it.\napi_key: fake-SECRET\n"
    path = _skill(tmp_path, stored)
    view, file_hash = _skill_view(tmp_path)
    edited = view.replace("description: d", "description: e") + "More.\n"
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: edited}, expected_hash=file_hash)
    assert path.read_text(encoding="utf-8") == stored.replace("d\n", "e\n", 1) + "More.\n"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # Every unmasked character of the line must be unchanged, compared as text.
        ("good.example.com", "evil.example.com"),
        ("good.example.com", "evil.example.com\\@good.example.com"),
        ("/path", "/other"),
    ],
)
def test_skill_masked_line_is_refused_when_its_unmasked_text_changes(
    tmp_path: Path, old: str, new: str
) -> None:
    stored = "---\ndescription: d\n---\ncurl https://user:pw-SECRET@good.example.com/path\n"
    path = _skill(tmp_path, stored)
    view, file_hash = _skill_view(tmp_path)
    assert "pw-SECRET" not in view
    with pytest.raises(cw.InvalidCandidateError, match="no stored line matches"):
        sk.write_skill(
            tmp_path, "s", {sk.SKILL_FILENAME: view.replace(old, new)}, expected_hash=file_hash
        )
    assert path.read_bytes() == stored.encode("utf-8")


def test_skill_new_line_with_the_mask_is_refused(tmp_path: Path) -> None:
    path = _skill(tmp_path, "---\ndescription: d\n---\nRun it.\n")
    view, file_hash = _skill_view(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="line 5 holds the mask"):
        sk.write_skill(
            tmp_path, "s", {sk.SKILL_FILENAME: view + f"token: {MASK}\n"}, expected_hash=file_hash
        )
    assert MASK not in path.read_text(encoding="utf-8")


def test_skill_lines_that_look_alike_pair_in_order_and_refuse_a_removal(tmp_path: Path) -> None:
    stored = "---\ndescription: d\n---\ntoken: A-SECRET\nmiddle\ntoken: B-SECRET\n"
    path = _skill(tmp_path, stored)
    view, file_hash = _skill_view(tmp_path)
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: view}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")
    view, file_hash = _skill_view(tmp_path)
    removed = view.replace(f"token: {MASK}\nmiddle\n", "middle\n")
    with pytest.raises(cw.InvalidCandidateError, match="not sent back one for one"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: removed}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


_SKILL_HOOK = (
    "---\ndescription: d\nhooks:\n  PostToolUse:\n    - matcher: Bash\n      hooks:\n"
    "        - type: http\n          url: https://good.example.com/hook\n"
    "          headers:\n            Authorization: Bearer tok-SECRET\n---\nRun it.\n"
)


def test_skill_masked_hook_header_is_kept_while_the_hooks_are_unchanged(tmp_path: Path) -> None:
    path = _skill(tmp_path, _SKILL_HOOK)
    view, file_hash = _skill_view(tmp_path)
    assert "tok-SECRET" not in view
    edited = view.replace("Run it.", "Run it twice.")
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: edited}, expected_hash=file_hash)
    assert path.read_text(encoding="utf-8") == _SKILL_HOOK.replace("Run it.", "Run it twice.")


def test_skill_masked_hook_header_is_refused_when_the_hook_url_changes(tmp_path: Path) -> None:
    # The url is on another line, so the masked line itself is unchanged; the hooks block
    # is not, so the stored credential is not sent to the new url.
    path = _skill(tmp_path, _SKILL_HOOK)
    view, file_hash = _skill_view(tmp_path)
    moved = view.replace("good.example.com", "evil.example.com")
    with pytest.raises(cw.InvalidCandidateError, match="'hooks' or 'mcpServers' changed"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: moved}, expected_hash=file_hash)
    assert path.read_bytes() == _SKILL_HOOK.encode("utf-8")


_HOOK_BLOCK = (
    "  PostToolUse:\n    - matcher: Bash\n      hooks:\n        - type: http\n"
    "          url: https://good.example.com/hook\n"
    "          headers:\n            Authorization: Bearer tok-SECRET\n"
)


@pytest.mark.parametrize(
    "stored",
    [
        "\ufeff" + _SKILL_HOOK,  # a byte order mark before the fence
        # An alias: the masked line sits in the anchor, outside the `hooks: *h` line.
        "---\ndescription: d\nx-shared: &h\n"
        + _HOOK_BLOCK.replace("\n  ", "\n    ").replace("  PostToolUse", "    PostToolUse", 1)
        + "hooks: *h\n---\nRun it.\n",
        # A merge key: there is no `hooks` key line at all.
        "---\ndescription: d\n<<:\n  hooks:\n"
        + "".join("  " + line + "\n" for line in _HOOK_BLOCK.splitlines())
        + "---\nRun it.\n",
        # An inline MCP server: its url and its masked header sit on different lines.
        "---\ndescription: d\nmcpServers:\n  - srv:\n      type: http\n"
        "      url: https://good.example.com/mcp\n"
        "      headers:\n        Authorization: Bearer tok-SECRET\n---\nRun it.\n",
    ],
)
def test_skill_masked_frontmatter_line_is_refused_when_a_destination_changes_anywhere(
    tmp_path: Path, stored: str
) -> None:
    # Placed directly: some of these spellings are hand-written, not what the editor writes.
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(stored.encode("utf-8"))
    view, file_hash = _skill_view(tmp_path)
    assert "tok-SECRET" not in view
    moved = view.replace("good.example.com", "evil.example.com")
    with pytest.raises(cw.InvalidCandidateError, match="'hooks' or 'mcpServers' changed"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: moved}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


def test_skill_masked_save_over_an_undecodable_skill_md_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(b"token: \xff\n")
    file_hash = cw.hash_bytes(path.read_bytes())
    with pytest.raises(cw.InvalidCandidateError, match="no stored line matches"):
        sk.write_skill(
            tmp_path, "s", {sk.SKILL_FILENAME: f"token: {MASK}\n"}, expected_hash=file_hash
        )
    assert path.read_bytes() == b"token: \xff\n"


def test_one_save_keeps_an_unnamed_script_and_restores_a_masked_skill_md_line(
    tmp_path: Path,
) -> None:
    # The editor sends SKILL.md alone: the unnamed script is kept as stored (PR 1826), and
    # the masked SKILL.md line gets its stored text back (TASK-427), in the same save.
    stored = "---\ndescription: d\n---\nRun it.\napi_key: fake-MIX\n"
    files = {sk.SKILL_FILENAME: stored, "scripts/run.sh": "TOKEN=t-SCRIPT\n"}
    sk.write_skill(
        tmp_path, "s", files, expected_hash=None, confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN
    )
    view, file_hash = _skill_view(tmp_path)
    assert "fake-MIX" not in view
    edited = view.replace("Run it.", "Run it now.")
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: edited}, expected_hash=file_hash)
    skill = tmp_path / "skills" / "s"
    assert (skill / sk.SKILL_FILENAME).read_text(encoding="utf-8") == stored.replace(
        "Run it.", "Run it now."
    )
    assert (skill / "scripts" / "run.sh").read_text(encoding="utf-8") == "TOKEN=t-SCRIPT\n"


def test_skill_masks_bind_to_the_bytes_the_stale_guard_checked(tmp_path: Path) -> None:
    # The restore uses the SKILL.md text passed in (read once with its hash), not a second
    # read of the disk.
    disk = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"token: from-DISK\n")
    out = sk._keep_masked_lines(
        tmp_path / "skills", "s", {sk.SKILL_FILENAME: f"token: {MASK}\n"}, skill_md="token: T\n"
    )
    assert out == {sk.SKILL_FILENAME: "token: T\n"}


def test_skill_script_masked_line_is_kept(tmp_path: Path) -> None:
    files = {sk.SKILL_FILENAME: "---\ndescription: d\n---\nRun it.\n", "run.sh": "TOKEN=t-S\n"}
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    sk.write_skill(tmp_path, "s", files, expected_hash=None, confirm_scripts=confirm)
    script, _h, _e = sk.read_skill_file(tmp_path, "s", "run.sh")
    _md, file_hash = _skill_view(tmp_path)
    resent = {**files, "run.sh": script}
    sk.write_skill(tmp_path, "s", resent, expected_hash=file_hash, confirm_scripts=confirm)
    assert (tmp_path / "skills" / "s" / "run.sh").read_text(encoding="utf-8") == "TOKEN=t-S\n"


def test_skill_without_frontmatter_keeps_a_masked_line(tmp_path: Path) -> None:
    path = _skill(tmp_path, "Run it.\ntoken: t-NOFM\n")
    view, file_hash = _skill_view(tmp_path)
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: view}, expected_hash=file_hash)
    assert path.read_text(encoding="utf-8") == "Run it.\ntoken: t-NOFM\n"


@pytest.mark.parametrize(
    "stored",
    [
        "---\nm:\n  n: [x\ntoken: t-BAD\n---\nRun it.\n",  # the header does not compose
        "---\n- a\n- ${T_LIST}\n---\nRun it.\n",  # the header is not a mapping
    ],
)
def test_skill_masked_save_of_a_header_that_does_not_load_is_refused(
    tmp_path: Path, stored: str
) -> None:
    # Placed on disk directly: the editor would never write either one.
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(stored.encode("utf-8"))
    view, file_hash = _skill_view(tmp_path)
    assert MASK in view
    with pytest.raises(cw.InvalidCandidateError):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: view}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


def test_skill_masked_hook_line_is_refused_when_the_stored_header_does_not_load(
    tmp_path: Path,
) -> None:
    # The operator repaired a bad line beside the hooks; the stored hooks cannot be compared.
    stored = _SKILL_HOOK.replace("description: d\n", "description: d\nx: !!int abc\n")
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(stored.encode("utf-8"))
    view, file_hash = _skill_view(tmp_path)
    repaired = view.replace("x: !!int abc\n", "")
    with pytest.raises(cw.InvalidCandidateError, match="'hooks' or 'mcpServers' changed"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: repaired}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


_SCRIPT_FILES = {sk.SKILL_FILENAME: "---\ndescription: d\n---\nRun it.\n", "run.sh": "T=t\n"}


def _masked_script_save(tmp_path: Path) -> None:
    """Send a masked `run.sh` for skill `s`, as the editor would after loading it."""
    _md, file_hash = _skill_view(tmp_path)
    files = {**_SCRIPT_FILES, "run.sh": f"T={MASK}\n"}
    sk.write_skill(
        tmp_path, "s", files, expected_hash=file_hash, confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN
    )


def _stored_script_skill(tmp_path: Path) -> Path:
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    sk.write_skill(tmp_path, "s", _SCRIPT_FILES, expected_hash=None, confirm_scripts=confirm)
    return tmp_path / "skills" / "s" / "run.sh"


@pytest.mark.timeout(5)
@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo is POSIX only")
def test_a_stored_fifo_named_by_a_masked_member_is_refused_without_blocking(
    tmp_path: Path,
) -> None:
    # The stored member is read outside the save's lock. Opening a FIFO for reading blocks
    # until a writer appears, which would hold a worker thread for good.
    script = _stored_script_skill(tmp_path)
    script.unlink()
    os.mkfifo(script)
    started = time.monotonic()
    with pytest.raises(cw.InvalidCandidateError, match="not a regular file"):
        _masked_script_save(tmp_path)
    assert time.monotonic() - started < 0.5


@pytest.mark.timeout(5)
@pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs privileges")
def test_a_stored_symlink_named_by_a_masked_member_is_refused(tmp_path: Path) -> None:
    # Even one that points at a file inside the skill: following it would bind the mask to
    # another file's text.
    script = _stored_script_skill(tmp_path)
    other = script.with_name("other.sh")
    other.write_text("T=from-OTHER\n", encoding="utf-8")
    script.unlink()
    script.symlink_to(other.name)
    started = time.monotonic()
    with pytest.raises(cw.InvalidCandidateError, match="symlink"):
        _masked_script_save(tmp_path)
    assert time.monotonic() - started < 0.5
    assert other.read_text(encoding="utf-8") == "T=from-OTHER\n"


@pytest.mark.timeout(5)
@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo and symlinks are POSIX here")
@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_the_lstat_check_alone_refuses_a_fifo_or_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # Without O_NOFOLLOW and O_NONBLOCK (Windows has neither), the lstat check is what stops
    # the open from blocking on a FIFO or reading through a symlink.
    script = _stored_script_skill(tmp_path)
    script.unlink()
    if kind == "fifo":
        os.mkfifo(script)
    else:
        script.with_name("other.sh").write_text("T=x\n", encoding="utf-8")
        script.symlink_to("other.sh")
    monkeypatch.setattr(sk, "_OPEN_FLAGS", os.O_RDONLY)
    started = time.monotonic()
    with pytest.raises(cw.InvalidCandidateError, match="not a regular file"):
        _masked_script_save(tmp_path)
    assert time.monotonic() - started < 0.5


def test_a_masked_member_with_nothing_stored_is_refused(tmp_path: Path) -> None:
    _stored_script_skill(tmp_path).unlink()
    with pytest.raises(cw.InvalidCandidateError, match="no stored line matches"):
        _masked_script_save(tmp_path)


def test_a_stored_member_too_large_to_bind_is_refused(tmp_path: Path) -> None:
    script = _stored_script_skill(tmp_path)
    script.write_bytes(b"x" * (sk.MAX_FILE_BYTES + 1))
    with pytest.raises(cw.InvalidCandidateError, match="too large"):
        _masked_script_save(tmp_path)


@pytest.mark.parametrize("call", ["lstat", "open", "fstat-fifo", "fstat-file"])
def test_a_stored_member_that_cannot_be_read_safely_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    # An lstat or open error, or a descriptor that is not the entry the lstat saw (a swap
    # in between), is a refusal, never a silent empty read. The two fstat cases pin the
    # two halves of that check on their own: the same object that is no longer a regular
    # file, and another regular file (not the same object).
    script = _stored_script_skill(tmp_path)
    func = call.split("-")[0]
    real = getattr(os, func)
    sibling = script.with_name("sibling.sh")
    sibling.write_text("T=other\n", encoding="utf-8")
    fields = list(os.stat(script))[:10]
    fields[0] = stat.S_IFIFO | 0o644  # st_mode; st_ino and st_dev stay the stored file's
    other = os.stat_result(fields) if call == "fstat-fifo" else os.stat(sibling)

    def broken(target, *args, **kwargs):
        if func == "fstat":
            return other
        if Path(target) == script:
            raise PermissionError("denied")
        return real(target, *args, **kwargs)

    _md, file_hash = _skill_view(tmp_path)
    files = {**_SCRIPT_FILES, "run.sh": f"T={MASK}\n"}
    monkeypatch.setattr(sk.os, func, broken)
    with pytest.raises(cw.InvalidCandidateError, match="unreadable"):
        sk.write_skill(
            tmp_path, "s", files, expected_hash=file_hash, confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN
        )


def test_skill_member_masks_fail_closed_on_odd_paths_and_bytes(tmp_path: Path) -> None:
    files = {sk.SKILL_FILENAME: "---\ndescription: d\n---\nRun it.\n", "run.sh": "TOKEN=t\n"}
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    sk.write_skill(tmp_path, "s", files, expected_hash=None, confirm_scripts=confirm)
    _md, file_hash = _skill_view(tmp_path)
    escape = {**files, "../x.sh": f"TOKEN={MASK}\n"}
    with pytest.raises(cw.PathEscapeError):
        sk.write_skill(tmp_path, "s", escape, expected_hash=file_hash, confirm_scripts=confirm)
    (tmp_path / "skills" / "s" / "run.sh").write_bytes(b"TOKEN=\xff\n")
    unreadable = {**files, "run.sh": f"TOKEN={MASK}\n"}
    with pytest.raises(cw.InvalidCandidateError, match="no stored line matches"):
        sk.write_skill(tmp_path, "s", unreadable, expected_hash=file_hash, confirm_scripts=confirm)


def test_one_stored_line_is_kept_at_most_once(tmp_path: Path) -> None:
    # Many masked copies of one long stored line used to bind to it again and again, and
    # the restored text grew by the stored length per copy before any cap ran.
    long_line = "TOKEN=" + "s" * 200_000
    copies = f"TOKEN={MASK}\n" * 4000
    started = time.monotonic()
    with pytest.raises(cw.InvalidCandidateError, match="not sent back one for one"):
        cw.restore_masked_lines(copies, long_line + "\n", what="x.sh")
    assert time.monotonic() - started < 0.5
    # Two copies of a line stored once are refused too; one copy is kept.
    with pytest.raises(cw.InvalidCandidateError, match="one for one"):
        cw.restore_masked_lines(f"token: {MASK}\ntoken: {MASK}\n", "token: x\n", what="f")
    assert cw.restore_masked_lines(f"token: {MASK}\n", "token: x\n", what="f")[0] == "token: x\n"


def test_the_restored_skill_must_fit_the_directory_cap(tmp_path: Path) -> None:
    # Every member is named, so no kept file brings the save's own cap check into play: the
    # restored total is checked on its own.
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    names = [f"s{i}.sh" for i in range(9)]
    files = {sk.SKILL_FILENAME: _SCRIPT_FILES[sk.SKILL_FILENAME], **dict.fromkeys(names, "x\n")}
    sk.write_skill(tmp_path, "s", files, expected_hash=None, confirm_scripts=confirm)
    skill = tmp_path / "skills" / "s"
    for script in names:  # each one under the file cap, all together over the directory cap
        (skill / script).write_text("TOKEN=" + "s" * 250_000 + "\n", encoding="utf-8")
    _md, file_hash = _skill_view(tmp_path)
    masked = {**files, **dict.fromkeys(names, f"TOKEN={MASK}\n")}
    with pytest.raises(cw.InvalidCandidateError, match="with its masked values kept"):
        sk.write_skill(tmp_path, "s", masked, expected_hash=file_hash, confirm_scripts=confirm)


@pytest.mark.parametrize("member", [sk.SKILL_FILENAME, "run.sh"])
def test_a_restored_member_over_its_file_cap_is_refused(tmp_path: Path, member: str) -> None:
    # The kept line makes the member longer than the masked text that was validated first.
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    cap = sk.MAX_SKILL_MD_BYTES if member == sk.SKILL_FILENAME else sk.MAX_FILE_BYTES
    head = "---\ndescription: d\n---\n" if member == sk.SKILL_FILENAME else ""
    sk.write_skill(tmp_path, "s", _SCRIPT_FILES, expected_hash=None, confirm_scripts=confirm)
    stored_line = "token: " + "s" * (cap * 2 // 3) + "\n"
    (tmp_path / "skills" / "s" / member).write_text(head + stored_line, encoding="utf-8")
    _md, file_hash = _skill_view(tmp_path)
    filler = "# " + "f" * (cap // 2) + "\n"
    sent = {**_SCRIPT_FILES, member: head + f"token: {MASK}\n" + filler}
    with pytest.raises(cw.InvalidCandidateError, match="byte cap") as excinfo:
        sk.write_skill(tmp_path, "s", sent, expected_hash=file_hash, confirm_scripts=confirm)
    # The size of the restored file would tell the length of the stored value: not named.
    restored = len((head + stored_line + filler).encode("utf-8"))
    assert str(restored) not in str(excinfo.value)


@pytest.mark.parametrize("member", [sk.SKILL_FILENAME, "run.sh"])
def test_a_restored_member_is_validated_again(tmp_path: Path, member: str) -> None:
    # The masked text hid a `${CLAUDE_PLUGIN_ROOT}` reference that a write must refuse;
    # putting the stored line back brings it back, so the restored member is validated.
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    head = "---\ndescription: d\n---\n" if member == sk.SKILL_FILENAME else ""
    sk.write_skill(tmp_path, "s", _SCRIPT_FILES, expected_hash=None, confirm_scripts=confirm)
    stored = head + "run ${CLAUDE_PLUGIN_ROOT}/x.sh\n"
    (tmp_path / "skills" / "s" / member).write_text(stored, encoding="utf-8")
    _md, file_hash = _skill_view(tmp_path)
    sent = {**_SCRIPT_FILES, member: head + f"run {MASK}/x.sh\n"}
    # The sent text passes, so the stored text is the cause, and the refusal says so
    # without the check's own message (which can name a size or a column of hidden text).
    with pytest.raises(cw.InvalidCandidateError, match="once its masked values are kept"):
        sk.write_skill(tmp_path, "s", sent, expected_hash=file_hash, confirm_scripts=confirm)
    # When the operator's own text fails too, that error is the one reported.
    own = {**sent, member: sent[member] + "also ${CLAUDE_PLUGIN_ROOT}/y.sh\n"}
    with pytest.raises(cw.InvalidCandidateError, match="CLAUDE_PLUGIN_ROOT"):
        sk.write_skill(tmp_path, "s", own, expected_hash=file_hash, confirm_scripts=confirm)


def test_a_frontmatter_mask_that_fails_once_restored_is_not_blamed_on_the_sent_text(
    tmp_path: Path,
) -> None:
    # A frontmatter mask (`api_token: ********`) is not valid YAML on its own. The sent
    # text is checked with the mask neutralized, so an unchanged save gets the refusal
    # that names the stored text, not a false YAML error at the mask.
    stored = "---\ndescription: d\napi_token: ${CLAUDE_PLUGIN_ROOT}\n---\nbody\n"
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(stored.encode("utf-8"))
    view, file_hash = _skill_view(tmp_path)
    assert f"api_token: {MASK}\n" in view
    with pytest.raises(cw.InvalidCandidateError, match="once its masked values are kept"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: view}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


@pytest.mark.parametrize(
    "header",
    [
        # A masked credential URL (`********@example.test/h`) must still read as a URL.
        "hooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n"
        "        - type: http\n          url: https://hook:pw-SECRET@example.test/h\n"
        "        - type: command\n          command: ${CLAUDE_PLUGIN_ROOT}/run.sh\n",
        # Two masked keys must not collide as one repeated key.
        "${A}: 1\n${B}: 2\napi_token: ${CLAUDE_PLUGIN_ROOT}\n",
    ],
)
def test_masks_in_urls_and_keys_are_not_blamed_on_the_sent_text(
    tmp_path: Path, header: str
) -> None:
    stored = f"---\ndescription: d\n{header}---\nbody\n"
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(stored.encode("utf-8"))
    view, file_hash = _skill_view(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="once its masked values are kept"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: view}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


def test_neutralized_keeps_url_shape_distinct_keys_and_length() -> None:
    sent = f"url: {MASK}@example.test/h\n{MASK}: 1\n{MASK}: 2\n"
    out = sk._neutralized(sent)
    assert out == "url: http://x@example.test/h\nx0: 1\nx1: 2\n"
    assert len(out) <= len(sent)


def test_a_neutralized_sent_text_near_the_cap_is_not_blamed_for_its_size(tmp_path: Path) -> None:
    # The stand-ins are never longer than the masks, so a sent SKILL.md just under its cap
    # whose stored text fails gets the refusal about the stored text, not a size error.
    head = "---\ndescription: d\n---\nrun ${CLAUDE_PLUGIN_ROOT}/x.sh\n"
    filler = "f" * (sk.MAX_SKILL_MD_BYTES - len(head) - 100) + "\n"
    stored = head + filler
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(stored.encode("utf-8"))
    view, file_hash = _skill_view(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="once its masked values are kept"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: view}, expected_hash=file_hash)


def test_the_cap_runs_before_the_restored_skill_md_is_parsed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A restored SKILL.md over its cap is refused before the destination check parses the
    # restored and the stored text a second time.
    cap = sk.MAX_SKILL_MD_BYTES
    head = "---\ndescription: d\n---\n"
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(head + "token: " + "s" * (cap * 2 // 3) + "\n", encoding="utf-8")
    _md, file_hash = _skill_view(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        sk, "_refuse_masks_with_changed_destinations", lambda *a: calls.append("parsed")
    )
    sent = head + f"token: {MASK}\n" + "# " + "f" * (cap // 2) + "\n"
    with pytest.raises(cw.InvalidCandidateError, match="byte cap"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: sent}, expected_hash=file_hash)
    assert calls == []


def test_the_kept_members_cap_names_no_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cap check of PR 1826 counts the RESTORED text. A size in its message, less the
    # masked size the operator sent, gives the length of the hidden value.
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    stored_value = "s" * 777
    files = {**_SCRIPT_FILES, "run.sh": f"token: {stored_value}\n"}
    files.update({f"pad{i}.txt": "p" * 6000 for i in range(3)})
    sk.write_skill(tmp_path, "s", files, expected_hash=None, confirm_scripts=confirm)
    _md, file_hash = _skill_view(tmp_path)
    monkeypatch.setattr(sk, "MAX_TOTAL_BYTES", 20_000)
    sent = {sk.SKILL_FILENAME: files[sk.SKILL_FILENAME], "run.sh": f"token: {MASK}\n"}
    sent["new.txt"] = "q" * 1500
    with pytest.raises(cw.InvalidCandidateError, match="too large together") as excinfo:
        sk.write_skill(tmp_path, "s", sent, expected_hash=file_hash, confirm_scripts=confirm)
    # The only number in the message is the cap.
    assert re.findall(r"\d+", str(excinfo.value)) == ["20000"]


def test_a_masked_member_under_a_case_variant_directory_is_restored(tmp_path: Path) -> None:
    # On a case-insensitive file system (macOS, Windows) `D/x.sh` IS the stored `d/x.sh`.
    # On Windows `resolve()` returns the on-disk case, and the parent check compares with
    # normcase so that this is not taken for a symlinked directory.
    if not _case_insensitive_fs(tmp_path):
        pytest.skip("needs a case-insensitive file system")
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    files = {**_SCRIPT_FILES, "d/x.sh": "token: t-CASE\n"}
    sk.write_skill(tmp_path, "s", files, expected_hash=None, confirm_scripts=confirm)
    _md, file_hash = _skill_view(tmp_path)
    sent = {**_SCRIPT_FILES, "D/x.sh": f"token: {MASK}\n"}
    sk.write_skill(tmp_path, "s", sent, expected_hash=file_hash, confirm_scripts=confirm)
    assert (tmp_path / "skills" / "s" / "d" / "x.sh").read_text(encoding="utf-8") == (
        "token: t-CASE\n"
    )


def _case_insensitive_fs(directory: Path) -> bool:
    """Whether ``directory`` sits on a case-insensitive file system (as in the skills tests)."""
    probe = directory / "case-probe-427"
    probe.write_bytes(b"")
    try:
        return (directory / "CASE-PROBE-427").exists()
    finally:
        probe.unlink()


def test_two_keys_for_one_masked_member_are_refused_before_any_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `d/x.sh`, `./d/x.sh` and `d//x.sh` name one file; each would build its own restored
    # copy of the stored member, so the save refuses them before it reads anything.
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    files = {**_SCRIPT_FILES, "d/x.sh": "token: " + "s" * 1000 + "\n"}
    sk.write_skill(tmp_path, "s", files, expected_hash=None, confirm_scripts=confirm)
    _md, file_hash = _skill_view(tmp_path)
    reads: list[str] = []
    real = sk._read_stored_member
    monkeypatch.setattr(sk, "_read_stored_member", lambda *a: reads.append(a[2]) or real(*a))
    keys = ["d/x.sh", "./d/x.sh", "d//x.sh"]
    sent = {**_SCRIPT_FILES, **dict.fromkeys(keys, f"token: {MASK}\n")}
    with pytest.raises(cw.InvalidCandidateError, match="another key names it too"):
        sk.write_skill(tmp_path, "s", sent, expected_hash=file_hash, confirm_scripts=confirm)
    assert reads == []


def test_a_mask_on_the_first_header_line_counts_as_a_frontmatter_line(tmp_path: Path) -> None:
    # The hook's own header is not masked here, so the first line is the only restored one.
    stored = _SKILL_HOOK.replace("---\ndescription: d\n", "---\napi_key: sk-FIRST\n", 1).replace(
        "Authorization: Bearer tok-SECRET", "X-Custom: plain"
    )
    path = tmp_path / "skills" / "s" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    path.write_bytes(stored.encode("utf-8"))
    view, file_hash = _skill_view(tmp_path)
    assert view.startswith(f"---\napi_key: {MASK}\n")
    moved = view.replace("good.example.com", "evil.example.com")
    with pytest.raises(cw.InvalidCandidateError, match="'hooks' or 'mcpServers' changed"):
        sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: moved}, expected_hash=file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs privileges")
def test_a_masked_member_behind_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    # `sub -> ../b` would lend skill b's file to skill s's mask.
    confirm = sk.SCRIPT_CONFIRM_TOKEN
    sk.write_skill(
        tmp_path,
        "b",
        {**_SCRIPT_FILES, "x.sh": "TOKEN=b-SECRET\n"},
        expected_hash=None,
        confirm_scripts=confirm,
    )
    _stored_script_skill(tmp_path)
    (tmp_path / "skills" / "s" / "sub").symlink_to("../b")
    _md, file_hash = _skill_view(tmp_path)
    sent = {**_SCRIPT_FILES, "sub/x.sh": f"TOKEN={MASK}\n"}
    with pytest.raises(cw.InvalidCandidateError, match="holds the mask") as excinfo:
        sk.write_skill(tmp_path, "s", sent, expected_hash=file_hash, confirm_scripts=confirm)
    assert "b-SECRET" not in str(excinfo.value)
    assert (tmp_path / "skills" / "b" / "x.sh").read_text(encoding="utf-8") == "TOKEN=b-SECRET\n"


def test_restore_masked_lines_reports_the_lines_it_restored() -> None:
    restored, lines = cw.restore_masked_lines(f"a\ntoken: {MASK}\nb", "token: x\na\n", what="f")
    assert (restored, lines) == ("a\ntoken: x\nb", {1})
