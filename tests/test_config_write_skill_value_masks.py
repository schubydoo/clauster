"""Server and hook secrets in SKILL.md frontmatter are masked as the MCP and Hooks tabs mask them.

TASK-529: the Skills editor masked SKILL.md line by line and each frontmatter ``url``, so an
``mcpServers`` ``headers`` or ``env`` value, or a hook ``headers`` value, showed in clear
under a key that does not look secret (``X-Custom: <token>``). The read view now
masks each of them by its YAML node. A resent masked line keeps the stored line only when
it is exactly the read view of that line and ``hooks`` and ``mcpServers`` load as stored;
any other change is refused (422) and nothing is written.

TASK-535: an ``mcpServers`` ``args`` item shows as the MCP tab shows it, in clear unless the
line masker masks it (a ``${...}`` reference, a URL with a user name or password, or a
``--api-key=x`` list item). The value is read as YAML loads it, as the tab reads it, so an
escape, a line break or ``!!binary`` does not hide a secret-shaped value.

TASK-497: the shared line masker missed a ``- token: x`` list item, a secret-keyed pair in
a flow mapping under a benign key (``env: {API_KEY: x}``), and the lines of a block value
under a secret key. Each shape is masked now, and each round-trips on save.

The fixture values are plain placeholders (no high-entropy strings), and no variable is
named like a credential, so gitleaks and CodeQL do not flag the test data.
"""

from __future__ import annotations

import math
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_skills as sk
from clauster.app import create_app
from clauster.config import load_config

MASK = cw.REDACTION_SENTINEL
#: ``https://u:qv-bin@h.example.com`` as base64, for a ``!!binary`` value.
_B64_URL = "aHR0cHM6Ly91OnF2LWJpbkBoLmV4YW1wbGUuY29t"
#: ``password=qv-b64kv`` as base64.
_B64_KV = "cGFzc3dvcmQ9cXYtYjY0a3Y="
FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"

_STDIO = (
    "---\ndescription: d\nmcpServers:\n  gh:\n    command: npx\n"
    "    args: [-y, pkg-arg-one]\n    env:\n      GH_PAT: qv-env-one\n---\nRun it.\n"
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
        (_STDIO, ["    args: [-y, pkg-arg-one]\n", f"      GH_PAT: {MASK}\n"]),
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
        # A flow sequence of args: an item is masked only as the line masker masks it.
        (
            "---\nmcpServers: {a: {args: [-y, '${GH_TOKEN}', 'https://u:qv-pw@h.example.com']}}"
            "\n---\n",
            "qv-pw",
            f"args: [-y, '{MASK}', '{MASK}@h.example.com']",
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
    stored = "---\n" + "\n".join(levels) + "\nmcpServers:\n  a:\n    env: {A: *l40}\n---\nb\n"
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
        (_STDIO, f"GH_PAT: {MASK}", f"GH_OTHER: {MASK}", "no stored line matches"),
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


# --- TASK-535: an args item shows as the MCP tab shows it ------------------------------------


def test_a_secret_shaped_args_item_is_masked_and_round_trips(tmp_path: Path) -> None:
    stored = (
        "---\ndescription: d\nmcpServers:\n  gh:\n    command: npx\n    args:\n      - -y\n"
        "      - pkg-arg-two\n      - --api-key=qv-key\n      - ${GH_TOKEN}\n---\nRun it.\n"
    )
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert "qv-" not in content and "GH_TOKEN" not in content
    # The plain items show as stored, the secret-shaped ones as the mask.
    assert "      - -y\n      - pkg-arg-two\n" in content
    assert f"      - --api-key={MASK}\n      - {MASK}\n" in content
    _save(tmp_path, content.replace("description: d", "description: e"), file_hash)
    assert path.read_bytes() == stored.replace("description: d", "description: e").encode()


@pytest.mark.parametrize(
    ("stored", "hidden"),
    [
        # An escaped `@`: the line masker sees no `user@host`, the MCP tab does.
        ('---\nmcpServers: {a: {args: ["https://u:qv-pw\\u0040h.example.com"]}}\n---\n', "qv-pw"),
        ('---\nmcpServers: {a: {args: ["https://u:qv-px\\x40h.example.com"]}}\n---\n', "qv-px"),
        # An escaped `$`: a `${...}` reference.
        ('---\nmcpServers: {a: {args: ["--k=\\x24{qv_ref}"]}}\n---\n', "qv_ref"),
        # A double-quoted value whose escaped line break joins the URL across two lines.
        (
            "---\nmcpServers:\n  a:\n    args:\n"
            '      - "https://u:qv-py\\\n        @h.example.com"\n---\n',
            "qv-py",
        ),
        # Another server field, and a hook field, spelled with an escape.
        ('---\nmcpServers: {a: {command: "https://u:qv-pz\\u0040h"}}\n---\n', "qv-pz"),
        (
            '---\nhooks: {Stop: [{hooks: [{type: command, command: "echo \\x24{qv_hook}"}]}]}'
            "\n---\n",
            "qv_hook",
        ),
        # An escaped `key=value` or `key: value` secret: the whole line masker reads the
        # loaded text, as it reads the same text written without the escape.
        ('---\nmcpServers: {a: {args: ["--api\\x2dkey=qv-eq"]}}\n---\n', "qv-eq"),
        ('---\nmcpServers: {a: {args: ["tok\\x65n: qv-kv"]}}\n---\n', "qv-kv"),
        # A loaded `${...}` that spans a line break: the line masker reads each line alone,
        # so the whole text is scanned too.
        ('---\nmcpServers: {a: {args: ["\\x24{\\nqv_nl}"]}}\n---\n', "qv_nl"),
        ('---\ndescription: "see \\x24{\\nqv_nl2}"\n---\nb\n', "qv_nl2"),
        # The loaded text is read as a line of its own.
        ('---\ndescription: "API_KEY\\x3dqv-desc"\n---\nb\n', "qv-desc"),
        ('---\nmetadata: {k: "env: {t\\x6fken: qv-flow}"}\n---\n', "qv-flow"),
        (f"---\nmetadata: {{k: !!binary {_B64_KV}}}\n---\n", "cGFzc3dv"),
        # Any other frontmatter field: the decoded value is read there too.
        ('---\ndescription: "see https://u:qv-top\\u0040h.example.com"\n---\nb\n', "qv-top"),
        ("---\nmetadata: {k: !!binary aHR0cHM6Ly91OnF2LWJpbkBoLmV4YW1wbGUuY29t}\n---\n", "aHR0"),
        # A key that looks secret only once its escape is read masks its value, as on the tab.
        ('---\nmcpServers: {a: {"client\\x53ecret": qv-hint}}\n---\n', "qv-hint"),
        # A base64 `!!binary` value holds the URL in its decoded bytes.
        (
            "---\nmcpServers: {a: {args: [!!binary aHR0cHM6Ly91OnF2LWJpbkBoLmV4YW1wbGUuY29t]}}"
            "\n---\n",
            "aHR0cHM6",
        ),
    ],
)
def test_a_secret_shaped_value_is_masked_however_it_is_spelled(
    tmp_path: Path, stored: str, hidden: str
) -> None:
    # TASK-535: the MCP and Hooks tabs read the decoded value, so the editor reads it too.
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert hidden not in content
    assert MASK in content
    assert len(content.splitlines()) == len(stored.splitlines())
    _save(tmp_path, content, file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


@pytest.mark.parametrize(
    "stored",
    [
        # A `!!binary` value that does not decode is masked (fail closed).
        '---\nmcpServers:\n  a:\n    args:\n      - !!binary "qv-bad"\n---\n',
        '---\nmcpServers:\n  a:\n    args:\n      - !!binary "qv-\u00e9"\n---\n',
        # A `url` value that is not a string: the url view cannot read its base64 text.
        f"---\nmcpServers:\n  s:\n    url: !!binary {_B64_URL}\n---\nb\n",
        f"---\nurl: !!binary {_B64_URL}\n---\nb\n",
        # A binary tag spelled with a `%` escape, with no other text the pre-check reads.
        f"---\nmetadata: {{k: !!%62inary {_B64_URL}}}\n---\n",
        f"---\nmetadata: {{k: !<tag:yaml.org,2002:%62inary> {_B64_URL}}}\n---\n",
        # A key that looks secret only once its escape is read, outside the two fields.
        '---\nmetadata:\n  "api\\x5fkey": qv-k\n---\n',
    ],
)
def test_a_value_that_does_not_load_or_hides_its_text_is_masked(
    tmp_path: Path, stored: str
) -> None:
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert "qv-" not in content and "aHR0cHM6" not in content
    assert len(content.splitlines()) == len(stored.splitlines())
    if "qv-bad" not in stored and "qv-\u00e9" not in stored:  # a save loads the value
        _save(tmp_path, content, file_hash)
        assert path.read_bytes() == stored.encode("utf-8")


def test_a_value_reached_under_a_secret_key_and_as_an_item_shows_as_the_mask(
    tmp_path: Path,
) -> None:
    # The key path wins whichever path the walk takes first; an `args` item alone would
    # show the host (`********@h.example.com`).
    stored = (
        "---\nx: &u https://u:qv-al@h.example.com\nmcpServers: {a: {args: [*u], token: *u}}\n---\n"
    )
    _place(tmp_path, stored)
    content = sk.read_skill_file(tmp_path, "s")[0]
    assert content.splitlines()[1] == MASK


@pytest.mark.parametrize(
    "stored",
    [
        # A block value under a secret key outside the two fields: the line masker keeps
        # the key visible, and the header view does not mask the key line.
        "---\nurl: https://h.example.com\ntoken: >\n  qv-a\n  qv-b\n---\n",
        # An empty value under a secret key, and a `url` that is no string, stay as written.
        "---\nmcpServers:\n  s:\n    apiKey:\n    url: https://h.example.com\n---\n",
        "---\nmcpServers: {s: {apiKey: '', url: null}}\n---\n",
        "---\nurl: 8080\nhooks: {}\n---\n",
    ],
)
def test_the_header_view_leaves_what_the_line_masker_shows(tmp_path: Path, stored: str) -> None:
    _place(tmp_path, stored)
    assert sk.read_skill_file(tmp_path, "s")[0] == cw.redact_secret_lines(stored)


def test_a_literal_value_with_a_backslash_is_masked_in_place(tmp_path: Path) -> None:
    # A plain scalar reads its backslash as text, so its source is the loaded value: the
    # line masker's view of that value replaces it in place, and the key stays.
    stored = "---\nmetadata: {k: API_KEY=qv-v\\y}\n---\n"
    _place(tmp_path, stored)
    assert sk.read_skill_file(tmp_path, "s")[0] == f"---\nmetadata: {{k: API_KEY={MASK}}}\n---\n"


def test_a_folded_value_with_a_reference_is_masked_in_place(tmp_path: Path) -> None:
    # The value reads in its source as it loads, so only the reference is masked, as by
    # the line masker, and the key and the other lines stay.
    stored = (
        "---\ndescription: >\n  Use ${CLAUDE_SKILL_DIR}/x\n  more\n"
        "url: https://h.example.com\n---\nb\n"
    )
    _place(tmp_path, stored)
    content = sk.read_skill_file(tmp_path, "s")[0]
    assert content == stored.replace("${CLAUDE_SKILL_DIR}", MASK)


@pytest.mark.parametrize(
    ("header", "held"),
    [
        ("k: !!%62inary x\n", True),
        ("k: !<tag:yaml.org,2002:%62inary> x\n", True),
        ("k: a!b c%d\n", False),  # the `%` is in another word than the `!`
        ("k: 100%!\n", False),  # the `%` is before the `!`
        ("k: " + "!" * 50 + "\n", False),
    ],
)
def test_the_tag_pre_check_reads_a_percent_escape_in_one_word(header: str, held: bool) -> None:
    assert sk._may_hold_masked(header) is held


def test_the_pre_check_is_linear_in_a_run_of_tag_marks() -> None:
    # Greptile on PR 1855: `![^\s]*%` scanned the rest of a run of `!` again from each `!`.
    # A ratio, not a time bound: eight times the text must cost far less than 64 times.
    def cost(n: int) -> float:
        header = "k: " + "!" * n + "\n"
        best = math.inf
        for _ in range(5):
            start = time.perf_counter()
            sk._may_hold_masked(header)
            best = min(best, time.perf_counter() - start)
        return best

    small, large = cost(20_000), cost(160_000)
    assert large < 24 * max(small, 1e-6)


def test_an_oversized_header_is_masked_whole_before_any_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The size check comes first, so no scan reads a header that no save can write.
    monkeypatch.setattr(sk, "_may_hold_masked", lambda text: pytest.fail("scanned"))
    header = "k: v\n" * (sk.MAX_FILE_BYTES // 5 + 1)
    assert sk._mask_header_values(header, readable=True) == f"{MASK}\n" * header.count("\n")


def test_a_plain_args_item_spelled_with_an_escape_still_shows(tmp_path: Path) -> None:
    # Only a secret-shaped value is masked: an escape alone does not hide an item.
    stored = '---\nmcpServers: {a: {args: ["pkg\\u002done", -y]}}\n---\n'
    _place(tmp_path, stored)
    assert sk.read_skill_file(tmp_path, "s")[0] == stored


# --- TASK-536: a secret value that goes on past its line ------------------------------------


@pytest.mark.parametrize(
    ("text", "view"),
    [
        # The three shapes of the task: an empty value at the end of the line, the second
        # half of a quoted value, and a value behind an anchor.
        ("env: {token:\n  qv-a}\n", f"env: {{token:\n  {MASK}\n"),
        ('env: {token: "qv-b\n  qv-c"}\n', f"env: {{token: {MASK}\n  {MASK}\n"),
        ("env: {token: &a\n  qv-d}\n", f"env: {{token: {MASK}\n  {MASK}\n"),
        # A single-quoted value, a plain value, a nested value and a tag that go on.
        ("env: {token: 'qv-e\n  qv-f'}\n", f"env: {{token: {MASK}\n  {MASK}\n"),
        ("env: {token: qv-g\n  qv-h}\n", f"env: {{token: {MASK}\n  {MASK}\n"),
        ("env: {token: {a: qv-i,\n  b: qv-j}}\n", f"env: {{token: {MASK}\n  {MASK}\n"),
        ("env: {token: !!str\n  qv-k}\n", f"env: {{token: {MASK}\n  {MASK}\n"),
        # More lines, a blank line, CRLF, a list item, and the value ends at the column.
        (
            'env: {token: "qv-l\n\n    qv-m\n    qv-n"}\nnext: ok\n',
            f"env: {{token: {MASK}\n\n    {MASK}\n    {MASK}\nnext: ok\n",
        ),
        ("env: {token:\r\n  qv-o}\r\nnext: ok\r\n", f"env: {{token:\r\n  {MASK}\r\nnext: ok\r\n"),
        ("- env: {token:\n  qv-p}\n- b\n", f"- env: {{token:\n  {MASK}\n- b\n"),
        ("env: {token:\n  qv-q\n}\n", f"env: {{token:\n  {MASK}\n}}\n"),
        # A secret `key: value` whose value is an unclosed quote or a flow value.
        ('token: "qv-r\n  qv-s"\nnext: ok\n', f"token: {MASK}\n  {MASK}\nnext: ok\n"),
        ("token: 'qv-t\n  qv-u'\n", f"token: {MASK}\n  {MASK}\n"),
        ("token: {a: qv-v,\n  b: qv-w}\n", f"token: {MASK}\n  {MASK}\n"),
        ("token: [qv-x,\n  qv-y]\n", f"token: {MASK}\n  {MASK}\n"),
        ('token: &a "qv-z\n  qv-aa"\n', f"token: {MASK}\n  {MASK}\n"),
        # In a `- ` item the key is past the dash, so these lines are measured from the line.
        ("- token: {a: qv-ab,\n  b: qv-ac}\n- next\n", f"- token: {MASK}\n  {MASK}\n- next\n"),
        ("- token: [qv-ad,\n  qv-ae]\n", f"- token: {MASK}\n  {MASK}\n"),
        # The line that closes a quote does not end the value: it goes on until a line that
        # is not indented deeper, because the rest of that line can open another secret
        # value (in a flow opened on this line or on an earlier one).
        (
            '- token: "qv-af\n  qv-ag"\n  name: x\n- next\n',
            f"- token: {MASK}\n  {MASK}\n  {MASK}\n- next\n",
        ),
        (
            "token: 'qv-ah\n  it''s qv-ai\n  qv-aj'\nb: c\n",
            f"token: {MASK}\n  {MASK}\n  {MASK}\nb: c\n",
        ),
        (
            'token: "qv-ak\n  \\" qv-al\n  qv-am"\nb: c\n',
            f"token: {MASK}\n  {MASK}\n  {MASK}\nb: c\n",
        ),
        (
            'env: {\n  token: "qv-au\n    qv-av", password: "qv-aw\n    qv-ax"}\n',
            f"env: {{\n  token: {MASK}\n    {MASK}\n    {MASK}\n",
        ),
        (
            'env: {\n  token: "qv-ay\n    qv-az", password:\n    qv-ca}\n',
            f"env: {{\n  token: {MASK}\n    {MASK}\n    {MASK}\n",
        ),
        (
            'env: {token: "qv-an\n  qv-ao", mode: x,\n  level: 2}\nnext: ok\n',
            f"env: {{token: {MASK}\n  {MASK}\n  {MASK}\nnext: ok\n",
        ),
        (
            'env: {token: "qv-ba\n  qv-bb", password: "qv-bc\n  qv-bd"}\n',
            f"env: {{token: {MASK}\n  {MASK}\n  {MASK}\n",
        ),
        (
            'env: {token: "qv-be\n  qv-bf", password:\n  qv-bg}\n',
            f"env: {{token: {MASK}\n  {MASK}\n  {MASK}\n",
        ),
        (
            "env: {token: 'qv-bh\n  qv-bi', password: 'qv-bj\n  qv-bk'}\n",
            f"env: {{token: {MASK}\n  {MASK}\n  {MASK}\n",
        ),
        # Node properties before an open quote, and a one-space continuation.
        ('- token: &a "qv-bl\n  qv-bm"\n', f"- token: {MASK}\n  {MASK}\n"),
        ('token: !t "qv-bn\n  qv-bo: x"\n', f"token: {MASK}\n  {MASK}\n"),
        ("env: {token: qv-bp\n qv-bq}\n", f"env: {{token: {MASK}\n {MASK}\n"),
        # A `- ` line deeper than the key is text of a plain value in YAML.
        ("token: qv-br\n  - qv-bs\n", f"token: {MASK}\n  {MASK}\n"),
        # A `- key:` line ends a plain value, so a secret key on it opens its own value.
        (
            "token = qv-cb\n  - token:\n      env: qv-cc\n",
            f"token = {MASK}\n  - token:\n      {MASK}\n",
        ),
        (
            "token = qv-cd\n  - - token:\n        env: qv-ce\n",
            f"token = {MASK}\n  - - token:\n        {MASK}\n",
        ),
        # A `:` with no space after it does not start a key, so YAML folds the line in.
        ("token: qv-cf\n  - foo:qv-cg\n  bar:qv-ch\n", f"token: {MASK}\n  {MASK}\n  {MASK}\n"),
        # A plain value goes on in the deeper lines that are not a key or a comment.
        (
            "token: qv-ap\n  qv-aq\n  qv-ar\nnext: ok\n",
            f"token: {MASK}\n  {MASK}\n  {MASK}\nnext: ok\n",
        ),
        ("api_key = qv-as\n    qv-at)\n", f"api_key = {MASK}\n    {MASK}\n"),
        # A secret `key: value` that ends on its line, with another secret flow value after
        # it on the same line that goes on: the deeper lines hold that value.
        (
            'metadata: {\n  token: "qv-da", password: "qv-db\n    qv-dc"}\n',
            f"metadata: {{\n  token: {MASK}\n    {MASK}\n",
        ),
        (
            "metadata: {\n  token: 'qv-dd', password: 'qv-de\n    qv-df: x'}\n",
            f"metadata: {{\n  token: {MASK}\n    {MASK}\n",
        ),
        (
            'metadata: {\n  token: qv-dg, password: "qv-dh\n    qv-di: x"}\n',
            f"metadata: {{\n  token: {MASK}\n    {MASK}\n",
        ),
        (
            "metadata: {\n  token: &a 'qv-dj', password:\n    qv-dk}\n",
            f"metadata: {{\n  token: {MASK}\n    {MASK}\n",
        ),
        (
            '- token: "qv-dl", api_key: {a: qv-dm,\n    b: qv-dn}\n',
            f"- token: {MASK}\n    {MASK}\n",
        ),
        (
            'metadata: {\n  token: &a, password: "qv-do\n    qv-dp: x"}\n',
            f"metadata: {{\n  token: {MASK}\n    {MASK}\n",
        ),
    ],
)
def test_a_secret_value_that_goes_on_is_masked_on_each_line(text: str, view: str) -> None:
    assert cw.redact_secret_lines(text) == view
    restored, _lines = cw.restore_masked_lines(view, text, what="f", view=cw.redact_secret_lines)
    assert restored == text


@pytest.mark.parametrize(
    "text",
    [
        # The value ends on its line: the next line is not part of it.
        "env: {token: qv-a, mode: fast,\n  level: 2}\n",
        'env: {token: "qv-b"\n  , level: 2}\n',
        'token: "qv-c"\n  indented: y\n',
        "token: qv-d\n  indented: y\n",
        "token: qv-e\n  # a comment\n",
        "- token: qv-g\n  name: x\n",
        "- token: qv-h\n  more text\n",
        "env: {mode:\n  fast}\n",
        # A closed quote with a closed or benign pair after it on the same line.
        '  token: "qv-f", password: "qv-i"\n    , level: 2}\n',
        '  token: "qv-j", mode: "fast\n    , level: 2"}\n',
    ],
)
def test_a_secret_value_that_ends_on_its_line_leaves_the_next_line(text: str) -> None:
    second = text.splitlines(keepends=True)[1]
    view = cw.redact_secret_lines(text)
    assert view.splitlines(keepends=True)[1] == second
    assert "qv-" not in view


def test_a_skill_with_a_value_that_goes_on_round_trips(tmp_path: Path) -> None:
    stored = (
        '---\ndescription: d\nmetadata: {token: "qv-one\n  qv-two"}\n---\nRun it.\n'
        "env: {api_key:\n  qv-three}\n"
    )
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert "qv-" not in content
    _save(tmp_path, content.replace("Run it.", "Run it now."), file_hash)
    assert path.read_bytes() == stored.replace("Run it.", "Run it now.").encode("utf-8")
