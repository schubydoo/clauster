"""Server and hook secrets in SKILL.md frontmatter are masked as the MCP and Hooks tabs mask them.

TASK-529: the Skills editor masked SKILL.md line by line and each frontmatter ``url``, so an
``mcpServers`` ``headers``, ``env`` or ``args`` value, or a hook ``headers`` value, showed in
clear under a key that does not look secret (``X-Custom: <token>``). The read view now
masks each of them by its YAML node. A resent masked line keeps the stored line only when
it is exactly the read view of that line and ``hooks`` and ``mcpServers`` load as stored;
any other change is refused (422) and nothing is written.

TASK-497: the shared line masker missed a ``- token: x`` list item, a secret-keyed pair in
a flow mapping under a benign key (``env: {API_KEY: x}``), and the lines of a block value
under a secret key. Each shape is masked now, and each round-trips on save.

The fixture values are plain placeholders (no high-entropy strings), and no variable is
named like a credential, so gitleaks and CodeQL do not flag the test data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_skills as sk
from clauster.app import create_app
from clauster.config import load_config

MASK = cw.REDACTION_SENTINEL
FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"

_STDIO = (
    "---\ndescription: d\nmcpServers:\n  gh:\n    command: npx\n"
    "    args: [-y, qv-arg-one]\n    env:\n      GH_PAT: qv-env-one\n---\nRun it.\n"
)
_HTTP = (
    "---\ndescription: d\nmcpServers:\n  api:\n    type: http\n"
    "    url: https://mcp.example.com/v1\n    headers:\n      X-Custom: qv-header-one\n"
    "    headersHelper: echo qv-helper-one\n---\nRun it.\n"
)
_HOOK = (
    "---\ndescription: d\nhooks:\n  PostToolUse:\n    - matcher: Bash\n      hooks:\n"
    "        - type: http\n          url: https://hooks.example.com/run\n"
    "          headers:\n            X-Custom: qv-hook-one\n---\nRun it.\n"
)


def _place(tmp_path: Path, text: str, name: str = "s") -> Path:
    """Write ``text`` as the stored SKILL.md of skill ``name``, byte for byte."""
    path = tmp_path / "skills" / name / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _view(tmp_path: Path) -> tuple[str, str]:
    content, file_hash, _exists, _note = sk.read_skill_file_for_edit(tmp_path, "s")
    return content, file_hash


def _save(tmp_path: Path, content: str, file_hash: str) -> None:
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: content}, expected_hash=file_hash)


def _no_stored_digits(message: str) -> bool:
    """Whether ``message`` holds no number but the line number of the sent text."""
    return re.search(r"\d", re.sub(r"\bline \d+\b", "", message)) is None


# --- TASK-529: the read view -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        (_STDIO, [f"    args: [{MASK}, {MASK}]\n", f"      GH_PAT: {MASK}\n"]),
        (_HTTP, [f"      X-Custom: {MASK}\n", f"    headersHelper: {MASK}\n"]),
        (_HOOK, [f"            X-Custom: {MASK}\n"]),
    ],
)
def test_server_and_hook_secrets_show_masked(
    tmp_path: Path, stored: str, shown: list[str]
) -> None:
    _place(tmp_path, stored)
    for content in (_view(tmp_path)[0], sk.read_skill_file(tmp_path, "s")[0]):
        assert "qv-" not in content
        for line in shown:
            assert line in content
        # The key names, the destination and every other line stay as stored.
        assert len(content.splitlines()) == len(stored.splitlines())
        assert "description: d\n" in content and "Run it.\n" in content


def test_the_route_masks_a_server_header(write_config, tmp_path, projects_root) -> None:
    _place(projects_root / "alpha" / ".claude", _HTTP, name="greeter")
    cfg = write_config(
        f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n"
        "config_write:\n  enabled: true\n"
    )
    with TestClient(create_app(load_config(cfg))) as c:
        got = c.get("/api/config-write/skills/file?project=alpha&name=greeter")
    assert got.status_code == 200
    assert "qv-header-one" not in got.text
    assert f"      X-Custom: {MASK}\n" in got.json()["content"]


@pytest.mark.parametrize(
    ("stored", "hidden", "shown"),
    [
        # A flow mapping: a quote around the value stays, an empty value stays.
        (
            '---\nmcpServers: {a: {headers: {X-A: "qv-dq", X-B: \'qv-sq\', X-C: ""}}}\n---\n',
            "qv-",
            f'{{X-A: "{MASK}", X-B: \'{MASK}\', X-C: ""}}',
        ),
        # A flow sequence of args, with a url that the args mask wins over.
        (
            "---\nmcpServers: {a: {args: [--url, 'https://h.example.com/?k=qv-arg']}}\n---\n",
            "qv-arg",
            f"args: [{MASK}, '{MASK}']",
        ),
        # A list of servers, as an `!!omap`-like list.
        (
            "---\nmcpServers:\n  - a:\n      env:\n        A: qv-list\n---\n",
            "qv-list",
            f"        A: {MASK}\n",
        ),
        # An alias: the anchored value is masked where it is written.
        (
            "---\nx-shared: &h {X-A: qv-alias}\nmcpServers:\n  a:\n    headers: *h\n---\n",
            "qv-alias",
            f"x-shared: &h {{X-A: {MASK}}}\n",
        ),
        # A merge key inside a server.
        (
            "---\nbase: &b {headers: {X-A: qv-merge}}\nmcpServers:\n  a: {<<: *b}\n---\n",
            "qv-merge",
            f"base: &b {{headers: {{X-A: {MASK}}}}}\n",
        ),
        # A top-level merge key that brings in the whole field.
        (
            "---\n<<: {hooks: {Stop: [{hooks: [{type: http, url: https://h.example.com, "
            "headers: {X-A: qv-top}}]}]}}\n---\n",
            "qv-top",
            f"headers: {{X-A: {MASK}}}",
        ),
        # A top-level merge key with a list, naming one mapping twice; a nested value.
        (
            "---\nx: &m {mcpServers: {a: {env: {A: {B: qv-nested}}}}}\n<<: [*m, *m]\n---\n",
            "qv-nested",
            f"{{A: {{B: {MASK}}}}}",
        ),
        # A url under a masked key: masked whole, not given the url view.
        (
            "---\nmcpServers: {a: {headers: {url: 'https://h.example.com/?k=qv-u'}}}\n---\n",
            "h.example.com",
            f"{{url: '{MASK}'}}",
        ),
        # A block value: each line of it is masked whole.
        (
            "---\nmcpServers:\n  a:\n    headers:\n      X-A: |\n        qv-block\n---\n",
            "qv-block",
            f"    headers:\n{MASK}\n{MASK}\n---",
        ),
        # A byte order mark and CRLF line endings.
        (
            "\ufeff---\r\nmcpServers:\r\n  a:\r\n    env:\r\n      A: qv-crlf\r\n---\r\nb\r\n",
            "qv-crlf",
            f"      A: {MASK}\r\n",
        ),
        # A tagged value: the tag sits before the value, so the line is masked whole.
        (
            "---\nmcpServers:\n  a:\n    env:\n      A: !!str qv-tag\n---\n",
            "qv-tag",
            f"    env:\n{MASK}\n---",
        ),
    ],
)
def test_every_spelling_of_a_server_or_hook_secret_is_masked(
    tmp_path: Path, stored: str, hidden: str, shown: str
) -> None:
    _place(tmp_path, stored)
    content = sk.read_skill_file(tmp_path, "s")[0]
    assert hidden not in content
    assert shown in content
    assert len(content.splitlines()) == len(stored.splitlines())


@pytest.mark.parametrize(
    "stored",
    [
        # The masked keys outside mcpServers and hooks are not server or hook fields.
        "---\ndescription: d\nmetadata:\n  headers: {X-A: plain-value}\n---\nb\n",
        # A header that is one plain scalar holds no field.
        "---\nsee the hooks docs\n---\nb\n",
        # The key names stay: an env variable name, a header name.
        "---\nmcpServers:\n  a:\n    env: {}\n    headers: {}\n---\nb\n",
    ],
)
def test_other_fields_are_shown_as_stored(tmp_path: Path, stored: str) -> None:
    _place(tmp_path, stored)
    assert sk.read_skill_file(tmp_path, "s")[0] == stored


@pytest.mark.parametrize(
    "stored",
    [
        "---\nmcpServers:\n  a:\n    env: {A: qv-noscheme}\n---\nb\n",
        "---\nhooks:\n  Stop:\n    - hooks:\n        - type: command\n          command: x\n"
        "          headers: {X-A: qv-nohost}\n---\nb\n",
    ],
)
def test_a_header_with_only_the_field_name_is_not_skipped(tmp_path: Path, stored: str) -> None:
    # No `url`, no scheme and no backslash: the early return must not skip the field.
    assert re.search(r"url|https?:|wss?:|\\", stored, re.IGNORECASE) is None
    _place(tmp_path, stored)
    assert "qv-no" not in sk.read_skill_file(tmp_path, "s")[0]


def test_an_alias_pyramid_under_a_field_is_walked_once_per_node(tmp_path: Path) -> None:
    # Each level holds the one below twice: without a visited set the walk would take
    # 2**40 steps. With it, each node is visited once.
    levels = ["l0: &l0 qv-leaf"] + [f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]" for i in range(1, 41)]
    stored = "---\n" + "\n".join(levels) + "\nmcpServers:\n  a:\n    args: *l40\n---\nb\n"
    _place(tmp_path, stored)
    content = sk.read_skill_file(tmp_path, "s")[0]
    assert "qv-leaf" not in content
    assert content.startswith(f"---\n{MASK}\nl1: &l1 [*l0, *l0]\n")


# --- TASK-529: the save ----------------------------------------------------------------------


@pytest.mark.parametrize("stored", [_STDIO, _HTTP, _HOOK])
def test_an_unchanged_resend_keeps_the_stored_values(tmp_path: Path, stored: str) -> None:
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert MASK in content and "qv-" not in content
    _save(tmp_path, content.replace("description: d", "description: e"), file_hash)
    assert path.read_bytes() == stored.replace("description: d", "description: e").encode()


@pytest.mark.parametrize(
    ("stored", "old", "new", "refusal"),
    [
        # The key beside the mask: the line is no longer the view of any stored line.
        (_HTTP, f"X-Custom: {MASK}", f"X-Other: {MASK}", "no stored line matches"),
        (_HOOK, f"X-Custom: {MASK}", f"Authorization: {MASK}", "no stored line matches"),
        (_STDIO, f"[{MASK}, {MASK}]", f"[{MASK}, {MASK}, more]", "no stored line matches"),
        # The destination on another line: the stored header would go somewhere new.
        (_HTTP, "mcp.example.com", "evil.example.com", "'hooks' or 'mcpServers' changed"),
        (_HOOK, "hooks.example.com", "evil.example.com", "'hooks' or 'mcpServers' changed"),
        (_STDIO, "command: npx", "command: other", "'hooks' or 'mcpServers' changed"),
        (_HTTP, "type: http", "type: sse", "'hooks' or 'mcpServers' changed"),
    ],
)
def test_any_other_change_beside_a_mask_is_refused_without_a_size(
    tmp_path: Path, stored: str, old: str, new: str, refusal: str
) -> None:
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    changed = content.replace(old, new)
    assert changed != content
    with pytest.raises(cw.InvalidCandidateError, match=re.escape(refusal)) as caught:
        _save(tmp_path, changed, file_hash)
    message = str(caught.value)
    assert "qv-" not in message
    assert _no_stored_digits(message)
    assert path.read_bytes() == stored.encode("utf-8")


def test_a_masked_header_line_moved_to_another_server_is_refused(tmp_path: Path) -> None:
    stored = _HTTP.replace(
        "---\nRun",
        "  other:\n    type: http\n    url: https://x.example.com\n"
        "    headers:\n      X-Keep: plain\n---\nRun",
    )
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    line = f"      X-Custom: {MASK}\n"
    moved = content.replace(line, "", 1).replace(
        "      X-Keep: plain\n", line + "      X-Keep: plain\n"
    )
    with pytest.raises(cw.InvalidCandidateError, match="'hooks' or 'mcpServers' changed"):
        _save(tmp_path, moved, file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


# --- TASK-497: the line masker ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "view"),
    [
        ("- token: qv-a\n", f"- token: {MASK}\n"),
        ("  - - api_key: 'qv-b'  \n", f"  - - api_key: {MASK}  \n"),
        ("-\ttoken = qv-c\n", f"-\ttoken = {MASK}\n"),
        ("-   token: qv-c2\n", f"-   token: {MASK}\n"),
        ("env: {token: qv-c3 , a: b}\n", f"env: {{token: {MASK} , a: b}}\n"),
        ("env: {token: , a: b, secret:}\n", "env: {token: , a: b, secret:}\n"),
        ("env: {API_KEY: qv-d, MODE: fast}\n", f"env: {{API_KEY: {MASK}, MODE: fast}}\n"),
        (
            'h: {"Authorization": "Bearer qv-e, x", n: 1}\n',
            f'h: {{"Authorization": {MASK}, n: 1}}\n',
        ),
        ("- {token: qv-f}\n", f"- {{token: {MASK}}}\n"),
        ("e: {token: 'qv-g, x: y}\n", f"e: {{token: {MASK}\n"),
        ("env: {secret:qv-h}\n", f"env: {{secret:{MASK}}}\n"),
        (
            "api_key: |\n  qv-i\n\n  qv-j\nnext: ok\n",
            f"api_key: {MASK}\n  {MASK}\n\n  {MASK}\nnext: ok\n",
        ),
        (
            "password: >-\r\n    qv-k\r\nnext: ok\r\n",
            f"password: {MASK}\r\n    {MASK}\r\nnext: ok\r\n",
        ),
        ("token:\n- qv-l\n- qv-m\nother: z\n", f"token:\n{MASK}\n{MASK}\nother: z\n"),
        ("- auth:\n    user: qv-n\n  name: x\n", f"- auth:\n    {MASK}\n  name: x\n"),
        ("a\rtoken:\r  qv-o\rb\r", f"a\rtoken:\r  {MASK}\rb\r"),
        # The value is on the key's own line, so the next line is not part of it.
        ("token: qv-p\n  indented: y\n", f"token: {MASK}\n  indented: y\n"),
        # A comment does not end a value; a deeper one is text of a literal value.
        ("auth:\n# note\n  user: qv-q\n", f"auth:\n# note\n  {MASK}\n"),
        (
            "api_key: |\n  qv-r\n  # qv-s\nnext: ok\n",
            f"api_key: {MASK}\n  {MASK}\n  {MASK}\nnext: ok\n",
        ),
        # Node properties and a quoted key before the value on the next lines.
        ("token: &a |\n  qv-t\n", f"token: {MASK}\n  {MASK}\n"),
        ("token: !!binary |\n  qv-u\n", f"token: {MASK}\n  {MASK}\n"),
        ("token: !!str\n  qv-v\n", f"token: {MASK}\n  {MASK}\n"),
        ('"token": |\n  qv-w\n', f'"token": |\n  {MASK}\n'),
        # Only a `- ` item continues a bare key at its column: not a fence, not `-foo`.
        ("api_key:\n---\n", "api_key:\n---\n"),
        ("token:\n-foo: v\n", "token:\n-foo: v\n"),
        ("token:\n-\tqv-tab\n-\n", f"token:\n{MASK}\n{MASK}\n"),
        # A mask that runs to the end of a line keeps every line ending `splitlines` knows.
        *(
            (f"env: {{TOKEN: [x, qv-sep]}}{sep}next{sep}", f"env: {{TOKEN: {MASK}{sep}next{sep}")
            for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ")
        ),
        ("a, token: 'qv-cr2\rnext\r", f"a, token: {MASK}\rnext\r"),
        ("api_key: |\n  qv-x\n- item\n", f"api_key: {MASK}\n  {MASK}\n- item\n"),
        # Flow sequences, a pair continued from the line above, nested values.
        ("env: [TOKEN: qv-y]\n", f"env: [TOKEN: {MASK}]\n"),
        ("env: [a, TOKEN: qv-z]\n", f"env: [a, TOKEN: {MASK}]\n"),
        ("  a: 1, TOKEN: qv-aa}\n", f"  a: 1, TOKEN: {MASK}}}\n"),
        ("env: {TOKEN: [x, qv-ab]}\n", f"env: {{TOKEN: {MASK}\n"),
        ("env: {TOKEN: {a: x, b: qv-ac}}\n", f"env: {{TOKEN: {MASK}\n"),
        ("env: {token: 'x, secret: qv-ad'}\n", f"env: {{token: {MASK}}}\n"),
        ("plain text, token: qv-ae\n", f"plain text, token: {MASK}\n"),
    ],
)
def test_the_line_masker_masks_list_flow_and_block_shapes(text: str, view: str) -> None:
    assert cw.redact_secret_lines(text) == view
    restored, _lines = cw.restore_masked_lines(view, text, what="f", view=cw.redact_secret_lines)
    assert restored == text


@pytest.mark.parametrize(
    "text",
    [
        "- name: x\n",
        "env: {MODE: fast, LEVEL: 2}\n",
        "description: |\n  token words\n",
        "author:\n  - a name\n",  # `author` is not an `auth` key
    ],
)
def test_the_line_masker_leaves_other_lines(text: str) -> None:
    assert cw.redact_secret_lines(text) == text


def test_a_skill_with_each_line_shape_round_trips(tmp_path: Path) -> None:
    stored = (
        "---\ndescription: d\n---\nRun it.\n- token: qv-li\nenv: {API_KEY: qv-flow}\n"
        "api_key: |\n  qv-block\n"
    )
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert "qv-" not in content
    _save(tmp_path, content.replace("Run it.", "Run it now."), file_hash)
    assert path.read_bytes() == stored.replace("Run it.", "Run it now.").encode("utf-8")
    # A changed flow line around the mask has no stored line to bind to.
    content, file_hash = _view(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="no stored line matches"):
        _save(tmp_path, content.replace("{API_KEY:", "{OTHER_KEY:"), file_hash)
