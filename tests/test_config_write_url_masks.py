"""A token in a marketplace or MCP server URL is masked in place, and bound on save (TASK-474).

The MCP tab, the Plugins surface, the Settings tab's ``extraKnownMarketplaces`` and a
subagent's inline ``mcpServers`` show a url through the http-hook URL masker
(:func:`clauster.config_write_hooks.mask_url`): a query value, the fragment and a
token-like path segment become ``********``, while the scheme, host, plain path and plain
query keys stay visible. A resent url that still holds the mask keeps the stored url only
when it is exactly the read view of that stored url. Any other change refuses the save
(422) with a message that carries no size, offset or count.

Fixture values are short, low-entropy placeholders (gitleaks), and no variable that holds
one is named like a credential (CodeQL taints by variable name).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_mcp as mcp
from clauster import config_write_mcp_cli as mcp_cli
from clauster import config_write_plugins as plugins
from clauster import config_write_settings as cws
from clauster import config_write_subagents as sub
from clauster.app import create_app
from clauster.config import load_config
from clauster.config_write_hooks import mask_url

MASK = cw.REDACTION_SENTINEL
_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"

#: A placeholder the masked views must never show (low entropy, no credential name).
_HIDDEN = "q-4498-zz"
_REPO_URL = f"https://git.example.com/q.git?token={_HIDDEN}"
_REPO_VIEW = f"https://git.example.com/q.git?token={MASK}"
_MCP_URL = f"https://mcp.example.com/v1/mcp?key={_HIDDEN}&ref=main"
_MCP_VIEW = f"https://mcp.example.com/v1/mcp?key={MASK}&ref={MASK}"


def _client(write_config, tmp_path: Path) -> TestClient:
    return TestClient(create_app(load_config(write_config(f"state_dir: {tmp_path}/.s\n{_ON}"))))


def _no_digits(message: str) -> None:
    # A refusal never prints a size, offset or count derived from the hidden value.
    assert not re.search(r"\d", message), message


# --- the shared view -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "view"),
    [
        pytest.param(_REPO_URL, _REPO_VIEW, id="query-value"),
        pytest.param(
            "https://git.example.com/q.git?ref=main",
            f"https://git.example.com/q.git?ref={MASK}",
            id="plain-query-key-stays-visible",
        ),
        pytest.param(
            "https://h.example.com/hooks/T0123ABCD/x",
            f"https://h.example.com/hooks/{MASK}/x",
            id="token-like-path-segment",
        ),
        pytest.param("https://h.example.com/a#frag", f"https://h.example.com/a#{MASK}", id="frag"),
        pytest.param(
            "https://h.example.com/plain/path",
            "https://h.example.com/plain/path",
            id="plain-url-unchanged",
        ),
        pytest.param(
            f"wss://ws.example.com/s?key={_HIDDEN}",
            f"wss://ws.example.com/s?key={MASK}",
            id="wss-like-https",
        ),
        pytest.param(
            f"WS://ws.example.com/s?k={_HIDDEN}",
            f"WS://ws.example.com/s?k={MASK}",
            id="ws-scheme-case-kept",
        ),
        pytest.param("wss://ws.example.com", "wss://ws.example.com", id="plain-wss"),
        pytest.param("git@github.com:acme/p.git", "git@github.com:acme/p.git", id="scp-form"),
        pytest.param(f"git://h.example.com/p.git?k={_HIDDEN}", MASK, id="other-scheme-query"),
        pytest.param(f"https://bot:{_HIDDEN}@h.example.com/p.git", MASK, id="credential-url"),
        pytest.param("https://h.example.com/${HOOK_KEY}/x", MASK, id="interpolation"),
        pytest.param(
            f"https://evil.example.com\\@good.example.com/x?k={_HIDDEN}", MASK, id="backslash-at"
        ),
        pytest.param(f"ws://h.example.com:bad/x?k={_HIDDEN}", MASK, id="ws-bad-port"),
        # WHATWG still reads these as https URLs with a user and password in them.
        pytest.param(f"https:\\\\bot:{_HIDDEN}@h.example.com/r.git", MASK, id="https-backslashes"),
        pytest.param(f"https:/bot:{_HIDDEN}@h.example.com/r.git", MASK, id="https-one-slash"),
        pytest.param(f"https:bot:{_HIDDEN}@h.example.com/r.git", MASK, id="https-no-slash"),
        pytest.param(f" https://bot:{_HIDDEN}@h.example.com/r.git", MASK, id="leading-space"),
        pytest.param(f"WSS:\\\\bot:{_HIDDEN}@h.example.com/r", MASK, id="wss-backslashes"),
        pytest.param(f"git://h.example.com/r\n{_HIDDEN}", MASK, id="line-break"),
    ],
)
def test_mask_url_view(url: str, view: str) -> None:
    assert mask_url(url) == view
    assert _HIDDEN not in mask_url(url)


# --- MCP tab ---------------------------------------------------------------------------


def _seed_mcp(project: Path, url: str = _MCP_URL, **extra: object) -> bytes:
    entry = {"type": "http", "url": url, **extra}
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"s": entry}}), "utf-8")
    return (project / ".mcp.json").read_bytes()


def _mcp_on_disk(project: Path) -> dict:
    return json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]


def test_mcp_read_masks_query_values_and_keeps_keys_visible(tmp_path: Path) -> None:
    _seed_mcp(tmp_path)
    shown, _h = mcp.read_project_servers(tmp_path)
    assert shown["s"]["url"] == _MCP_VIEW
    assert _HIDDEN not in json.dumps(shown)


def test_mcp_route_get_masks_the_url(write_config, tmp_path, projects_root) -> None:
    _seed_mcp(projects_root / "alpha")
    with _client(write_config, tmp_path) as c:
        resp = c.get("/api/config-write/mcp", params={"scope": "project", "project": "alpha"})
    assert resp.status_code == 200
    assert resp.json()["servers"]["s"]["url"] == _MCP_VIEW
    assert _HIDDEN not in resp.text


@pytest.mark.parametrize("writer", ["map", "entry"])
def test_mcp_unchanged_masked_url_keeps_the_stored_url(tmp_path: Path, writer: str) -> None:
    _seed_mcp(tmp_path, headers={"X": "h-4498"})
    shown, h = mcp.read_project_servers(tmp_path)
    if writer == "map":
        mcp.write_project_servers(tmp_path, shown, h)
    else:
        mcp.write_project_server_entry(tmp_path, "s", {**shown["s"], "timeout": 5000}, op="edit")
    stored = _mcp_on_disk(tmp_path)["s"]
    assert stored["url"] == _MCP_URL
    assert stored["headers"] == {"X": "h-4498"}


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda u: u.replace("mcp.example.com", "evil.example.com"), id="host"),
        pytest.param(lambda u: u.replace("/v1/mcp", "/v2/mcp"), id="path"),
        pytest.param(lambda u: u.replace("ref=", "tenant="), id="other-query-key"),
        pytest.param(lambda u: u + "&extra=1", id="added-query-param"),
        pytest.param(lambda u: u.replace("https://", "http://"), id="scheme"),
        pytest.param(lambda u: u.replace("mcp.example.com", "MCP.example.com"), id="host-case"),
        pytest.param(
            lambda u: u.replace("https://", "https://evil.example.com\\@"), id="backslash-at"
        ),
    ],
)
def test_mcp_changed_url_with_a_mask_is_refused(tmp_path: Path, edit) -> None:
    before = _seed_mcp(tmp_path)
    shown, h = mcp.read_project_servers(tmp_path)
    moved = {**shown["s"], "url": edit(shown["s"]["url"])}
    for write in (
        lambda: mcp.write_project_server_entry(tmp_path, "s", moved, op="edit"),
        lambda: mcp.write_project_servers(tmp_path, {"s": moved}, h),
    ):
        with pytest.raises(cw.InvalidCandidateError, match="Type the full url") as exc:
            write()
        _no_digits(str(exc.value))
    assert (tmp_path / ".mcp.json").read_bytes() == before


def test_mcp_masked_url_under_a_new_name_is_refused(tmp_path: Path) -> None:
    before = _seed_mcp(tmp_path)
    shown, h = mcp.read_project_servers(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="no stored url matches"):
        mcp.write_project_servers(tmp_path, {**shown, "copy": shown["s"]}, h)
    with pytest.raises(cw.InvalidCandidateError, match="no stored url matches"):
        mcp.write_project_server_entry(tmp_path, "copy", shown["s"], op="add")
    assert (tmp_path / ".mcp.json").read_bytes() == before


def test_mcp_restore_leaves_a_non_object_entry_and_refuses_a_non_object_stored() -> None:
    # A hand-edited non-object entry has no url to restore; a non-object stored entry has
    # no url to keep, so a partly masked url is refused.
    assert mcp._restore_masked_url("s", "plain", None) == "plain"
    with pytest.raises(cw.InvalidCandidateError, match="no stored url matches"):
        mcp._restore_masked_url("s", {"type": "http", "url": _MCP_VIEW}, ["not", "an", "entry"])


def test_mcp_whole_mask_still_restores_and_retyped_url_is_accepted(tmp_path: Path) -> None:
    # A credential url is still masked whole and restored by merge_redacted, as before.
    cred = f"https://bot:{_HIDDEN}@mcp.example.com/v1"
    _seed_mcp(tmp_path, url=cred)
    shown, h = mcp.read_project_servers(tmp_path)
    assert shown["s"]["url"] == MASK
    mcp.write_project_servers(tmp_path, shown, h)
    assert _mcp_on_disk(tmp_path)["s"]["url"] == cred
    # A fully retyped url is plain input, written as sent.
    fresh = "https://mcp.example.com/v1/mcp?key=new-1"
    mcp.write_project_server_entry(tmp_path, "s", {"type": "http", "url": fresh}, op="edit")
    assert _mcp_on_disk(tmp_path)["s"]["url"] == fresh


def test_mcp_path_only_mask_routes_off_the_cli(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    # A url with a masked path segment and no query would pass every CLI-routing check
    # except the mask itself; the CLI would store the literal mask. It goes direct.
    path_url = "https://mcp.example.com/hooks/T0123ABCD/mcp"
    entry = {"type": "http", "url": mask_url(path_url)}
    assert entry["url"] == f"https://mcp.example.com/hooks/{MASK}/mcp"
    assert mcp.contains_sentinel(entry)
    assert mcp_cli.entry_needs_direct_write(entry)
    alpha = projects_root / "alpha"
    _seed_mcp(alpha, url=path_url)
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_MCP_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path) as c:
        shown = c.get(
            "/api/config-write/mcp", params={"scope": "project", "project": "alpha"}
        ).json()["servers"]["s"]
        body = {"scope": "project", "project": "alpha", "confirm": "alpha", "op": "edit"}
        ok = c.post("/api/config-write/mcp/server", json={**body, "name": "s", "entry": shown})
        moved = {**shown, "url": shown["url"].replace("mcp.example.com", "evil.example.com")}
        bad = c.post("/api/config-write/mcp/server", json={**body, "name": "s", "entry": moved})
    assert ok.status_code == 200, ok.text
    assert _mcp_on_disk(alpha)["s"]["url"] == path_url
    assert bad.status_code == 422
    assert "Type the full url" in bad.json()["detail"]
    _no_digits(bad.json()["detail"])
    assert _mcp_on_disk(alpha)["s"]["url"] == path_url
    assert not argv_file.exists()  # neither save reached the CLI


# --- Plugins surface (read-only) ------------------------------------------------------


def _declared() -> dict:
    return {
        "q": {"source": {"source": "git", "url": _REPO_URL}},
        "plain": {"source": {"source": "git", "url": "https://git.example.com/p.git?ref=main"}},
        "gh": {"source": {"source": "github", "repo": "acme/plugins"}},
    }


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(data).encode("utf-8"))


def test_declared_marketplaces_mask_the_query_token(tmp_path: Path) -> None:
    _write_settings(cw.project_settings_path(tmp_path), {"extraKnownMarketplaces": _declared()})
    got = plugins.read_project_marketplaces(tmp_path)
    assert got["q"]["source"]["url"] == _REPO_VIEW
    assert got["plain"]["source"]["url"] == f"https://git.example.com/p.git?ref={MASK}"
    assert got["gh"] == _declared()["gh"]
    assert _HIDDEN not in json.dumps(got)


def test_route_declared_and_settings_mask_the_query_token_at_user_scope(
    write_config, tmp_path, projects_root
) -> None:
    # The TASK-474 repro: both GETs served the user-scope url with its token in clear.
    _write_settings(
        Path("~/.claude/settings.json").expanduser(), {"extraKnownMarketplaces": _declared()}
    )
    with _client(write_config, tmp_path) as c:
        declared = c.get("/api/config-write/marketplaces/declared?scope=user")
        settings = c.get("/api/config-write/settings?scope=user")
        effective = c.get("/api/config-write/settings/effective?project=alpha")
    for resp in (declared, settings, effective):
        assert resp.status_code == 200, resp.text
        assert _HIDDEN not in resp.text
    assert declared.json()["marketplaces"]["q"]["source"]["url"] == _REPO_VIEW
    assert settings.json()["settings"]["extraKnownMarketplaces"]["q"]["source"]["url"] == (
        _REPO_VIEW
    )


def test_route_marketplace_list_masks_the_query_token(write_config, tmp_path, monkeypatch) -> None:
    payload = [{"name": "q", "source": "git", "url": _REPO_URL}]
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", json.dumps(payload))
    with _client(write_config, tmp_path) as c:
        resp = c.get("/api/config-write/marketplaces?scope=user")
    assert resp.status_code == 200
    assert resp.json()["marketplaces"][0]["url"] == _REPO_VIEW
    assert _HIDDEN not in resp.text


def test_plugin_list_masks_an_mcp_server_query_token() -> None:
    entry = {"id": "p@m", "mcpServers": {"s": {"type": "http", "url": _MCP_URL}}}
    shown = plugins._redact_plugin_entry(entry)
    assert shown["mcpServers"]["s"]["url"] == _MCP_VIEW


def _market_add(c: TestClient, source: str):
    return c.post(
        "/api/config-write/marketplaces/action",
        json={
            "scope": "project",
            "project": "alpha",
            "confirm": "alpha",
            "op": "add",
            "source": source,
        },
    )


def test_marketplace_add_refuses_a_pasted_masked_url(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    # The list now shows `?token=********`; pasted into Add, the CLI would store the mask.
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    with _client(write_config, tmp_path) as c:
        resp = _market_add(c, _REPO_VIEW)
    assert resp.status_code == 422
    assert "Type the full url" in resp.json()["detail"]
    assert _REPO_VIEW not in resp.json()["detail"]
    assert not argv_file.exists()  # the CLI never ran


def test_marketplace_add_audit_line_masks_the_query_token(
    write_config, tmp_path, projects_root, monkeypatch
) -> None:
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CLAUDE_PLUGIN_STDOUT", "Successfully added marketplace: q")
    with _client(write_config, tmp_path) as c:
        resp = _market_add(c, _REPO_URL)
    assert resp.status_code == 200, resp.text
    # The CLI itself gets the real source; only the audit record is masked.
    assert json.loads(argv_file.read_text(encoding="utf-8"))["argv"][2] == _REPO_URL
    audit = (tmp_path / ".s" / "config_audit.log").read_text(encoding="utf-8")
    assert _HIDDEN not in audit
    assert _REPO_VIEW in audit


# --- Settings tab ------------------------------------------------------------------------


def _store(project: Path, data: dict) -> str:
    _write_settings(cw.project_settings_path(project), data)
    return cws.read_project_settings(project)[1]


def _settings_on_disk(project: Path) -> dict:
    return json.loads(cw.project_settings_path(project).read_text(encoding="utf-8"))


def _inline_market() -> dict:
    return {
        "inline": {
            "source": {
                "source": "settings",
                "name": "inline",
                "plugins": [
                    {"name": "a", "source": {"source": "url", "url": _REPO_URL}},
                    {"name": "b", "source": {"source": "url", "url": _REPO_URL + "b"}},
                ],
            }
        }
    }


@pytest.mark.parametrize("key", ["extraKnownMarketplaces", "additionalMarketplaces"])
def test_settings_unchanged_masked_url_keeps_the_stored_url(tmp_path: Path, key: str) -> None:
    stored = {
        **_declared(),
        "h": {"source": {"source": "url", "url": _REPO_URL, "headers": {"Cookie": "c-4498"}}},
    }
    h = _store(tmp_path, {key: stored})
    view = cws.read_project_settings(tmp_path)[0][key]
    assert view["q"]["source"]["url"] == _REPO_VIEW
    view["q"]["autoUpdate"] = True  # another field changes freely
    cws.write_project_settings(tmp_path, {key: view}, h)
    on_disk = _settings_on_disk(tmp_path)[key]
    assert on_disk["q"] == {**stored["q"], "autoUpdate": True}
    assert on_disk["h"] == stored["h"]
    assert on_disk["plain"] == stored["plain"]


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda u: u.replace("git.example.com", "evil.example.com"), id="host"),
        pytest.param(lambda u: u.replace("/q.git", "/r.git"), id="path"),
        pytest.param(lambda u: u.replace("token=", "sig="), id="other-query-key"),
        pytest.param(lambda u: u + "&ref=main", id="added-query-param"),
        pytest.param(
            lambda u: u.replace("https://", "https://evil.example.com\\@"), id="backslash-at"
        ),
    ],
)
def test_settings_changed_masked_url_is_refused(tmp_path: Path, edit) -> None:
    h = _store(tmp_path, {"extraKnownMarketplaces": _declared()})
    before = cw.project_settings_path(tmp_path).read_bytes()
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    view["q"]["source"]["url"] = edit(view["q"]["source"]["url"])
    with pytest.raises(cw.InvalidCandidateError, match="Type the full url") as exc:
        cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    _no_digits(str(exc.value).replace("extraKnownMarketplaces.q.source.url", ""))
    assert cw.project_settings_path(tmp_path).read_bytes() == before


def test_settings_masked_url_in_a_renamed_entry_is_refused(tmp_path: Path) -> None:
    h = _store(tmp_path, {"extraKnownMarketplaces": _declared()})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    with pytest.raises(cw.InvalidCandidateError, match="no stored url matches"):
        cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": {"q2": view["q"]}}, h)
    assert _settings_on_disk(tmp_path)["extraKnownMarketplaces"] == _declared()


def test_settings_list_of_masked_urls_round_trips(tmp_path: Path) -> None:
    h = _store(tmp_path, {"extraKnownMarketplaces": _inline_market()})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _settings_on_disk(tmp_path)["extraKnownMarketplaces"] == _inline_market()


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda p: list(reversed(p)), id="reorder"),
        pytest.param(lambda p: p[1:], id="delete-first"),
        pytest.param(lambda p: [*p, {"name": "c"}], id="append"),
        pytest.param(lambda p: [{**p[0], "name": "a2"}, p[1]], id="edit-masked-element"),
    ],
)
def test_settings_list_edit_around_a_masked_url_is_refused(tmp_path: Path, edit) -> None:
    # A list is bound by position, so a masked url's element must come back as it was read.
    h = _store(tmp_path, {"extraKnownMarketplaces": _inline_market()})
    view = cws.read_project_settings(tmp_path)[0]["extraKnownMarketplaces"]
    view["inline"]["source"]["plugins"] = edit(view["inline"]["source"]["plugins"])
    with pytest.raises(cw.InvalidCandidateError, match="Type the full url"):
        cws.write_project_settings(tmp_path, {"extraKnownMarketplaces": view}, h)
    assert _settings_on_disk(tmp_path)["extraKnownMarketplaces"] == _inline_market()


def test_settings_route_put_refuses_a_moved_masked_url(
    write_config, tmp_path, projects_root
) -> None:
    alpha = projects_root / "alpha"
    _store(alpha, {"extraKnownMarketplaces": _declared()})
    before = cw.project_settings_path(alpha).read_bytes()
    with _client(write_config, tmp_path) as c:
        r0 = c.get("/api/config-write/settings?scope=project&project=alpha").json()
        view = r0["settings"]
        view["extraKnownMarketplaces"]["q"]["source"]["url"] = _REPO_VIEW.replace(
            "git.example.com", "evil.example.com"
        )
        resp = c.put(
            "/api/config-write/settings",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": view,
                "hash": r0["hash"],
            },
        )
    assert resp.status_code == 422
    assert _HIDDEN not in resp.text
    assert cw.project_settings_path(alpha).read_bytes() == before


# --- subagent inline mcpServers --------------------------------------------------------


_AGENT = (
    "---\nname: my-agent\ndescription: d\nmcpServers:\n  - remote:\n      type: http\n"
    f"      url: {_MCP_URL}\n---\nbody\n"
)


def test_subagent_inline_server_url_is_masked_and_bound(tmp_path: Path) -> None:
    agents = sub.project_agents_dir(tmp_path)
    agents.mkdir(parents=True)
    path = agents / "my-agent.md"
    path.write_bytes(_AGENT.encode("utf-8"))
    doc = sub.read_project_agent(tmp_path, "my-agent")
    assert _HIDDEN not in doc["content"]
    assert doc["frontmatter"]["mcpServers"][0]["remote"]["url"] == _MCP_VIEW
    sub.write_project_agent(tmp_path, "my-agent", doc["content"], doc["hash"])
    assert path.read_bytes() == _AGENT.encode("utf-8")
    doc = sub.read_project_agent(tmp_path, "my-agent")
    moved = doc["content"].replace("mcp.example.com", "evil.example.com")
    with pytest.raises(cw.InvalidCandidateError, match="Type the full url"):
        sub.write_project_agent(tmp_path, "my-agent", moved, doc["hash"])
    assert path.read_bytes() == _AGENT.encode("utf-8")
