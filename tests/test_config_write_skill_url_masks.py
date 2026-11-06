"""A ``url`` in SKILL.md frontmatter is masked as the MCP and Hooks tabs mask it (TASK-525).

Before this, the Skills editor masked SKILL.md line by line only, so a token in the query
string of an ``mcpServers`` or ``hooks`` url came back in clear. The read view now masks
each frontmatter ``url`` with :func:`clauster.config_write_hooks.mask_url`. A resent masked
line keeps the stored line only when it is exactly the read view of that line; any other
change is refused (422) and nothing is written.

The fixture values are plain placeholders (no high-entropy strings), and no variable is
named like a credential, so gitleaks and CodeQL do not flag the test data.
"""

from __future__ import annotations

import os
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

_MCP = (
    "---\ndescription: d\nmcpServers:\n  - srv:\n      type: http\n"
    "      url: https://mcp.example.com/v1?key=qv-mcp-one\n---\nRun it.\n"
)
_MCP_VIEW_LINE = f"      url: https://mcp.example.com/v1?key={MASK}\n"

_HOOK = (
    "---\ndescription: d\nhooks:\n  PostToolUse:\n    - matcher: Bash\n      hooks:\n"
    "        - type: http\n"
    "          url: https://hooks.example.com/services/T0123ABCD/run?sig=qv-hook-one\n"
    "---\nRun it.\n"
)
_HOOK_VIEW_LINE = f"          url: https://hooks.example.com/services/{MASK}/run?sig={MASK}\n"


def _place(tmp_path: Path, text: str, name: str = "s") -> Path:
    """Write ``text`` as the stored SKILL.md of skill ``name``, byte for byte."""
    path = tmp_path / "skills" / name / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _view(tmp_path: Path, name: str = "s") -> tuple[str, str]:
    content, file_hash, _exists, _note = sk.read_skill_file_for_edit(tmp_path, name)
    return content, file_hash


def _save(tmp_path: Path, content: str, file_hash: str) -> None:
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: content}, expected_hash=file_hash)


def _no_stored_digits(message: str) -> bool:
    """Whether ``message`` holds no number but the line number of the sent text."""
    return re.search(r"\d", re.sub(r"\bline \d+\b", "", message)) is None


# --- the read view -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "line", "hidden"),
    [
        (_MCP, _MCP_VIEW_LINE, ["qv-mcp-one"]),
        (_HOOK, _HOOK_VIEW_LINE, ["qv-hook-one", "T0123ABCD"]),
    ],
)
def test_a_frontmatter_url_shows_its_destination_and_masks_its_tokens(
    tmp_path: Path, stored: str, line: str, hidden: list[str]
) -> None:
    _place(tmp_path, stored)
    for content in (_view(tmp_path)[0], sk.read_skill_file(tmp_path, "s")[0]):
        assert line in content
        for value in hidden:
            assert value not in content
    # Every other line is shown as stored.
    assert _view(tmp_path)[0].replace(line, "") == "".join(
        kept for kept in stored.splitlines(keepends=True) if "url:" not in kept
    )


def test_the_route_masks_a_frontmatter_url(write_config, tmp_path, projects_root) -> None:
    _place(projects_root / "alpha" / ".claude", _MCP, name="greeter")
    cfg = write_config(
        f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n"
        "config_write:\n  enabled: true\n"
    )
    with TestClient(create_app(load_config(cfg))) as c:
        got = c.get("/api/config-write/skills/file?project=alpha&name=greeter")
    assert got.status_code == 200
    assert "qv-mcp-one" not in got.text
    assert _MCP_VIEW_LINE in got.json()["content"]


@pytest.mark.parametrize(
    ("stored", "hidden", "shown"),
    [
        # Quoted, with a comment after it: only the value is masked.
        (
            '---\nmcpServers:\n  - a:\n      url: "https://h.example.com/p?k=qv-dq" # note\n---\n',
            "qv-dq",
            f'url: "https://h.example.com/p?k={MASK}" # note',
        ),
        (
            "---\nmcpServers:\n  - a:\n      url: 'https://h.example.com/p?k=qv-sq'\n---\n",
            "qv-sq",
            f"url: 'https://h.example.com/p?k={MASK}'",
        ),
        # A flow mapping: the url is found by its YAML node, not by a `url:` line start.
        (
            "---\nmcpServers: [{name: a, url: 'https://h.example.com/?k=qv-flow'}, "
            '{url: "https://h.example.com/?k=qv-flow2"}]\n---\n',
            "qv-flow",
            f"url: 'https://h.example.com/?k={MASK}'}}, "
            f'{{url: "https://h.example.com/?k={MASK}"}}',
        ),
        # A folded value spans two lines: both lines are masked whole.
        (
            "---\nmcpServers:\n  - a:\n      url: >-\n        https://h.example.com/?k=qv-fold\n"
            "---\n",
            "qv-fold",
            f"  - a:\n{MASK}\n{MASK}\n",
        ),
        # An alias: the anchored value is masked where it is written.
        (
            "---\nx-shared: &u https://h.example.com/?k=qv-alias\nmcpServers:\n  - a:\n"
            "      url: *u\n---\n",
            "qv-alias",
            f"---\n{MASK}\nmcpServers:",
        ),
        # An escape: the source text is not the value, so the line is masked whole.
        (
            '---\nmcpServers:\n  - a:\n      url: "https://h.example.com/?k=\\x41qv-esc"\n---\n',
            "qv-esc",
            f"  - a:\n{MASK}\n",
        ),
        # A byte order mark and CRLF line endings.
        (
            "\ufeff---\r\nmcpServers:\r\n  - a:\r\n      url: https://h.example.com/?k=qv-crlf\r\n"
            "---\r\nRun it.\r\n",
            "qv-crlf",
            f"      url: https://h.example.com/?k={MASK}\r\n",
        ),
        # A url outside hooks and mcpServers is masked too: over-masking only costs a retype.
        (
            "---\nmetadata:\n  url: https://h.example.com/?k=qv-meta\n---\n",
            "qv-meta",
            f"  url: https://h.example.com/?k={MASK}\n",
        ),
    ],
)
def test_every_spelling_of_a_frontmatter_url_is_masked(
    tmp_path: Path, stored: str, hidden: str, shown: str
) -> None:
    _place(tmp_path, stored)
    content = sk.read_skill_file(tmp_path, "s")[0]
    assert hidden not in content
    assert shown in content
    assert len(content.splitlines()) == len(stored.splitlines())


#: A tab before a comment: a syntax error to PyYAML (YAML 1.1) only, so Claude Code's YAML
#: 1.2 reads the header as written and this editor cannot compose it at all.
_TAB_COMMENT = "---\nname: x\t# note\nmcpServers:\n  srv:\n    type: http\n"


@pytest.mark.parametrize(
    "stored",
    [
        # Composes neither as written nor through the retry, yet YAML 1.2 reads each url:
        # WHATWG takes a backslash as a slash and needs no `//` after a web scheme.
        _TAB_COMMENT + "    url: https:\\\\h.example.com\\p?k=qv-c1\n---\nRun it.\n",
        _TAB_COMMENT + "    url: https:h.example.com/p?k=qv-c2\n---\nRun it.\n",
        _TAB_COMMENT + "    url:\n      https://h.example.com/p?k=\n      qv-c3\n---\n",
        # A duplicate anchor, which PyYAML refuses, around a block url split over lines.
        "---\na: &n 1\nb: &n 2\nmcpServers:\n  srv:\n    url: |-\n      https:\n"
        "      //h.example.com/p?k=qv-c4\n---\n",
        "---\nm: [x\nurl: https://h.example.com/?k=qv-bad\n---\nRun it.\n",
        # Composes only through Claude Code's re-quoting retry, which reads it as a string;
        # YAML 1.2 may need no retry and read the mapping as written.
        "---\nmcpServers: [{name: a, url: https://h.example.com/?k=qv-retry}]\n---\n",
        "---\nmcpServers: {srv: {type: http, url: https:h.example.com/p?k=qv-flow}}\n---\n",
        # Tabs: YAML refuses them, Claude Code's retry turns them into spaces and reads it.
        "---\nmcpServers:\n\t- a:\n\t\t\turl: https://h.example.com/p?k=qv-tab\n---\n",
        # Claude Code ends the header at the first `---`, even in a quoted string. PyYAML
        # reads one description here and no url at all.
        '---\ndescription: "see #1\nmcpServers:\n  srv:\n    type: http\n'
        '    url: https:h.example.com/p?k=qv-dash\n ---"\n---\n',
        # A line break to YAML 1.1 only, in a header that PyYAML composes.
        "---\ndescription: a\u2028  b\nmcpServers:\n  - a:\n"
        "      url: https://h.example.com/?k=qv-u2028\n---\n",
        # A quoted value that continues at column 0: PyYAML reads one description, YAML 1.2
        # refuses it, and Claude Code's retry closes the quote on its first line, so the
        # lines after it become real keys.
        '---\ndescription: "abc #x\nmcpServers:\n  srv:\n    type: http\n'
        '    url: https://h.example.com/p?k=qv-col0\nz: y"\n---\n',
        "---\ndescription: 'abc #x\nmcpServers:\n  srv:\n    type: http\n"
        "    url: https://h.example.com/p?k=qv-col0sq\nz: y'\n---\n",
        # Escapes spell the key and the scheme. YAML 1.2 reads this as written; PyYAML needs
        # the retry, which turns both lines into strings. Only the backslash shows a URL.
        '---\nx: {a: b?c}\nmcpServers: {srv: {type: http, "u\\x72l": '
        '"\\x68ttps://h.example.com/p?k=qv-esc"}}\n---\n',
        # A no-break space after the fence: Claude Code reads a header, FRONTMATTER_RE does not.
        "---\u00a0\nmcpServers:\n  - a:\n      url: https://h.example.com/?k=qv-nbsp\n---\n",
    ],
)
def test_a_header_this_editor_cannot_read_as_the_cli_does_is_masked_whole(
    tmp_path: Path, stored: str
) -> None:
    _place(tmp_path, stored)
    content = sk.read_skill_file(tmp_path, "s")[0]
    assert "qv-" not in content
    assert len(content.splitlines()) == len(stored.splitlines())
    header = content.splitlines()[1 : content.splitlines().index("---", 1)]
    assert header and all(line in ("", MASK) for line in header)


def test_a_yaml12_check_that_cannot_finish_masks_the_header_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object) -> None:
        raise ValueError("cannot finish")

    monkeypatch.setattr(cw, "_yaml12_refuses", broken)
    _place(tmp_path, _MCP)
    content = sk.read_skill_file(tmp_path, "s")[0]
    assert "qv-" not in content
    assert "mcp.example.com" not in content


def test_a_retry_header_with_a_url_masked_whole_still_saves_unchanged(tmp_path: Path) -> None:
    stored = (
        "---\ndescription: d\nargument-hint: [a] [b]\nmcpServers:\n  - a:\n"
        "      url: https://h.example.com/?k=qv-r\n---\nRun it.\n"
    )
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert "qv-" not in content and "argument-hint" not in content
    _save(tmp_path, content.replace("Run it.", "Run it now."), file_hash)
    assert path.read_bytes() == stored.replace("Run it.", "Run it now.").encode("utf-8")


def test_a_retry_header_with_no_url_is_shown_as_stored(tmp_path: Path) -> None:
    # The common retry case: an unquoted `[...]` hint. Nothing in it can be a URL.
    stored = "---\ndescription: d\nargument-hint: [filename] [format]\n---\nRun it.\n"
    _place(tmp_path, stored)
    assert sk.read_skill_file(tmp_path, "s")[0] == stored


def test_the_markdown_body_is_not_given_the_url_view(tmp_path: Path) -> None:
    stored = "---\ndescription: d\n---\nurl: https://h.example.com/p\n"
    _place(tmp_path, stored)
    assert sk.read_skill_file(tmp_path, "s")[0] == stored


def test_a_plain_url_with_nothing_to_hide_is_shown_as_stored(tmp_path: Path) -> None:
    stored = "---\nmcpServers:\n  - a:\n      url: https://h.example.com/mcp\n---\n"
    _place(tmp_path, stored)
    assert sk.read_skill_file(tmp_path, "s")[0] == stored


# --- the save --------------------------------------------------------------------------------


@pytest.mark.parametrize("stored", [_MCP, _HOOK])
def test_an_unchanged_resend_keeps_the_stored_url(tmp_path: Path, stored: str) -> None:
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert MASK in content
    _save(tmp_path, content.replace("Run it.", "Run it twice."), file_hash)
    assert path.read_bytes() == stored.replace("Run it.", "Run it twice.").encode("utf-8")


@pytest.mark.parametrize(
    "stored",
    [
        "---\nmcpServers: [{name: a, url: 'https://h.example.com/?k=qv-flow'}]\n---\nRun it.\n",
        "\ufeff---\r\nmcpServers:\r\n  - a:\r\n      url: https://h.example.com/?k=qv-crlf\r\n"
        "---\r\nRun it.\r\n",
        "---\nx-shared: &u https://h.example.com/?k=qv-alias\nmcpServers:\n  - a:\n"
        "      url: *u\n---\nRun it.\n",
    ],
)
def test_an_unchanged_resend_of_other_spellings_keeps_the_stored_text(
    tmp_path: Path, stored: str
) -> None:
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    assert "qv-" not in content
    _save(tmp_path, content.replace("Run it.", "Run it now."), file_hash)
    assert path.read_bytes() == stored.replace("Run it.", "Run it now.").encode("utf-8")


@pytest.mark.parametrize(
    ("stored", "old", "new"),
    [
        # The host, the path, a query key, and an added parameter: each is an unmasked part
        # of the line, so the line is no longer the read view of the stored line.
        (_MCP, "mcp.example.com", "evil.example.com"),
        (_MCP, "/v1?", "/v2?"),
        (_MCP, "?key=", "?kee="),
        (_MCP, f"key={MASK}", f"key={MASK}&extra=1"),
        (_MCP, f"key={MASK}", f"key={MASK}#frag"),
        (_HOOK, "hooks.example.com", "evil.example.com"),
        (_HOOK, "/run?", "/walk?"),
        (_HOOK, "?sig=", "?sag="),
        (_HOOK, f"sig={MASK}", f"sig={MASK}&more=x"),
        (_HOOK, "https://hooks", "http://hooks"),
    ],
)
def test_a_changed_masked_url_is_refused_and_nothing_is_written(
    tmp_path: Path, stored: str, old: str, new: str
) -> None:
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    changed = content.replace(old, new)
    assert changed != content
    with pytest.raises(cw.InvalidCandidateError, match="no stored line matches") as caught:
        _save(tmp_path, changed, file_hash)
    message = str(caught.value)
    assert "qv-" not in message and "T0123ABCD" not in message
    assert _no_stored_digits(message)
    assert path.read_bytes() == stored.encode("utf-8")


def test_a_masked_url_line_moved_into_the_body_is_refused(tmp_path: Path) -> None:
    # In the body the line gets no URL view, so a kept line would show its token in clear
    # on the next read. The restore checks the view of what it would write.
    path = _place(tmp_path, _MCP)
    content, file_hash = _view(tmp_path)
    moved = content.replace(_MCP_VIEW_LINE, "") + _MCP_VIEW_LINE
    with pytest.raises(cw.InvalidCandidateError, match="would no longer mask it") as caught:
        _save(tmp_path, moved, file_hash)
    assert "qv-" not in str(caught.value)
    assert _no_stored_digits(str(caught.value))
    assert path.read_bytes() == _MCP.encode("utf-8")


def test_a_masked_url_moved_out_of_its_server_is_refused(tmp_path: Path) -> None:
    stored = _MCP.replace("---\nRun", "x-other:\n  - b:\n      type: http\n---\nRun")
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    # Same indentation, same text: the line stays a url line, but under another field.
    swapped = content.replace(_MCP_VIEW_LINE, "").replace(
        "      type: http\n---", "      type: http\n" + _MCP_VIEW_LINE + "---"
    )
    with pytest.raises(cw.InvalidCandidateError, match="'hooks' or 'mcpServers' changed"):
        _save(tmp_path, swapped, file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


def test_the_existing_line_masks_still_apply_beside_a_url(tmp_path: Path) -> None:
    stored = _MCP.replace("description: d\n", "description: d\napi_key: plain-placeholder\n")
    stored = stored.replace("Run it.\n", "Run ${HOME}/x.sh\ncurl https://u:pw@h.example.com/\n")
    path = _place(tmp_path, stored)
    content, file_hash = _view(tmp_path)
    for value in ("plain-placeholder", "${HOME}", "u:pw", "qv-mcp-one"):
        assert value not in content
    _save(tmp_path, content, file_hash)
    assert path.read_bytes() == stored.encode("utf-8")


def test_the_restore_refuses_a_view_that_changes_the_line_count() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="cannot be masked line for line"):
        cw.restore_masked_lines(
            f"token: {MASK}\n", "token: x\n", what="f", view=lambda text: text + "extra\n"
        )


def test_the_view_and_the_restore_are_linear_in_url_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Counted, not timed: each url value is passed to mask_url once per view, and a save
    # runs the view twice (the stored text and the restored text).
    n = 600  # under the 64 KB SKILL.md cap
    servers = "".join(
        f"  - s{i}:\n      url: https://h.example.com/v1?key=qv-{i}\n" for i in range(n)
    )
    stored = f"---\nmcpServers:\n{servers}---\nRun it.\n"
    path = _place(tmp_path, stored)
    calls: list[str] = []
    real = sk.mask_url

    def counting(url: str) -> str:
        calls.append(url)
        return real(url)

    monkeypatch.setattr(sk, "mask_url", counting)
    content, file_hash = _view(tmp_path)
    assert len(calls) == n
    calls.clear()
    _save(tmp_path, content, file_hash)
    assert len(calls) == 2 * n
    assert path.read_bytes() == stored.encode("utf-8")


# --- other spellings of SKILL.md (Greptile on PR 1839) -----------------------------------
#
# The resolver reads `./SKILL.md` and friends as SKILL.md, and a case variant, a symlink or
# a hard link reaches the same file. The view must follow the file, not the request string.

_ROUTE_CFG = "config_write:\n  enabled: true\n"


def _route_client(write_config, tmp_path: Path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ROUTE_CFG}")
    return TestClient(create_app(load_config(cfg)))


@pytest.mark.parametrize(
    "relative",
    [
        "./SKILL.md",
        "SKILL.md/",
        ".//SKILL.md",
        "SKILL.md/.",
        "././SKILL.md",
        # Reaches SKILL.md where the file system ignores case (macOS, Windows), and nothing
        # elsewhere. Either way the token must not show.
        "skill.md",
        "Skill.MD",
        # A backslash separates path parts on Windows only.
        ".\\SKILL.md",
    ],
)
def test_the_route_masks_skill_md_under_every_spelling(
    write_config, tmp_path, projects_root, relative: str
) -> None:
    _place(projects_root / "alpha" / ".claude", _MCP, name="greeter")
    with _route_client(write_config, tmp_path) as c:
        got = c.get(
            "/api/config-write/skills/file",
            params={"project": "alpha", "name": "greeter", "relative": relative},
        )
    assert "qv-mcp-one" not in got.text
    if got.status_code == 200 and got.json()["exists"]:
        assert _MCP_VIEW_LINE in got.json()["content"]


@pytest.mark.parametrize("relative", ["./SKILL.md", "SKILL.md/", ".//SKILL.md", "SKILL.md/."])
def test_a_spelling_the_resolver_reads_as_skill_md_gets_its_view(
    write_config, tmp_path, projects_root, relative: str
) -> None:
    _place(projects_root / "alpha" / ".claude", _MCP, name="greeter")
    with _route_client(write_config, tmp_path) as c:
        got = c.get(
            "/api/config-write/skills/file",
            params={"project": "alpha", "name": "greeter", "relative": relative},
        )
    assert got.status_code == 200
    assert got.json()["exists"] is True
    assert _MCP_VIEW_LINE in got.json()["content"]


def test_the_route_refuses_a_dotdot_spelling(write_config, tmp_path, projects_root) -> None:
    _place(projects_root / "alpha" / ".claude", _MCP, name="greeter")
    with _route_client(write_config, tmp_path) as c:
        got = c.get(
            "/api/config-write/skills/file",
            params={"project": "alpha", "name": "greeter", "relative": "sub/../SKILL.md"},
        )
    assert got.status_code == 400  # a path escape
    assert "qv-mcp-one" not in got.text


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_a_link_to_skill_md_gets_its_view(tmp_path: Path, kind: str) -> None:
    path = _place(tmp_path, _MCP)
    link = path.parent / "other.md"
    try:
        if kind == "symlink":
            link.symlink_to(sk.SKILL_FILENAME)
        else:
            os.link(path, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without rights
        pytest.skip(f"cannot make a {kind} here: {exc}")
    content = sk.read_skill_file(tmp_path, "s", "other.md")[0]
    assert "qv-mcp-one" not in content
    assert _MCP_VIEW_LINE in content


@pytest.mark.parametrize(
    "relative", ["./SKILL.md", "SKILL.md/", ".//SKILL.md", "SKILL.md/.", "skill.md", "Skill.MD"]
)
def test_a_save_refuses_another_spelling_of_skill_md(
    write_config, tmp_path, projects_root, relative: str
) -> None:
    path = _place(projects_root / "alpha" / ".claude", _MCP, name="greeter")
    content, file_hash = _view(projects_root / "alpha" / ".claude", name="greeter")
    body = {
        "scope": "project",
        "project": "alpha",
        "confirm": "alpha",
        "name": "greeter",
        "files": {sk.SKILL_FILENAME: content, relative: content},
        "hash": file_hash,
        "confirm_scripts": sk.SCRIPT_CONFIRM_TOKEN,
    }
    with _route_client(write_config, tmp_path) as c:
        got = c.put("/api/config-write/skills", json=body)
    assert got.status_code == 422
    assert "under another spelling or case" in got.json()["detail"]
    assert "qv-mcp-one" not in got.text
    assert path.read_bytes() == _MCP.encode("utf-8")
    assert sorted(p.name for p in path.parent.iterdir()) == [sk.SKILL_FILENAME]


def test_a_save_through_a_hard_link_binds_with_the_skill_md_view(tmp_path: Path) -> None:
    path = _place(tmp_path, _MCP)
    link = path.parent / "other.md"
    try:
        os.link(path, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - no hard links here
        pytest.skip(f"cannot make a hard link here: {exc}")
    content, file_hash = _view(tmp_path)
    linked = sk.read_skill_file(tmp_path, "s", "other.md")[0]
    sk.write_skill(
        tmp_path,
        "s",
        {sk.SKILL_FILENAME: content, "other.md": linked},
        expected_hash=file_hash,
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert path.read_bytes() == _MCP.encode("utf-8")
    assert link.read_bytes() == _MCP.encode("utf-8")
