"""v0.3 ghost-environment reaper (spec §11). Fully offline: a mock transport stands
in for the Anthropic API and credentials come from temp files — nothing hits the
network or reads real creds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clauster import __main__ as cli
from clauster import environments
from clauster.environments import (
    BETA_HEADER,
    Credentials,
    CredentialsError,
    Environment,
    EnvironmentsAPIError,
    EnvironmentsClient,
    find_ghosts,
    load_credentials,
)

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


class MockTransport:
    """Records calls; returns queued (status, payload) responses. payload may be a
    dict/list (json-encoded) or raw bytes."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        status, payload = self.responses.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


def _env(id_, type_, directory=None):
    return Environment(id=id_, name=id_, config={"type": type_, "directory": directory})


# ----- credentials ------------------------------------------------------


def _write_creds(tmp_path, *, token="tok-abcdef", org="org-1", expires=None):
    cred = tmp_path / "credentials.json"
    oauth = {"accessToken": token}
    if expires is not None:
        oauth["expiresAt"] = expires
    cred.write_text(json.dumps({"claudeAiOauth": oauth}))
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"oauthAccount": {"organizationUuid": org}}))
    return cred, cj


def test_load_credentials_ok(tmp_path):
    cred, cj = _write_creds(tmp_path, token="secret-token-xyz", org="org-x")
    c = load_credentials(cred, cj)
    assert c.access_token == "secret-token-xyz" and c.organization_uuid == "org-x"
    assert "secret-token-xyz" not in c.masked_token()  # token masked for logging


def test_load_credentials_expired(tmp_path):
    cred, cj = _write_creds(tmp_path, expires=1000)
    with pytest.raises(CredentialsError, match="expired"):
        load_credentials(cred, cj, now_ms=2000)


def test_load_credentials_not_expired_when_future(tmp_path):
    cred, cj = _write_creds(tmp_path, expires=10_000)
    assert load_credentials(cred, cj, now_ms=5000).access_token  # still valid


def test_load_credentials_missing_token(tmp_path):
    cred = tmp_path / "c.json"
    cred.write_text('{"claudeAiOauth": {}}')
    cj = tmp_path / "cj.json"
    cj.write_text('{"oauthAccount": {"organizationUuid": "o"}}')
    with pytest.raises(CredentialsError, match="accessToken"):
        load_credentials(cred, cj)


def test_load_credentials_missing_org(tmp_path):
    cred = tmp_path / "c.json"
    cred.write_text('{"claudeAiOauth": {"accessToken": "t"}}')
    cj = tmp_path / "cj.json"
    cj.write_text("{}")
    with pytest.raises(CredentialsError, match="organizationUuid"):
        load_credentials(cred, cj)


def test_load_credentials_missing_file(tmp_path):
    with pytest.raises(CredentialsError):
        load_credentials(tmp_path / "nope.json", tmp_path / "nope2.json")


def test_load_credentials_bad_json(tmp_path):
    cred = tmp_path / "c.json"
    cred.write_text("{not json")
    cj = tmp_path / "cj.json"
    cj.write_text("{}")
    with pytest.raises(CredentialsError, match="not valid JSON"):
        load_credentials(cred, cj)


def test_load_credentials_bad_json_in_claude_json(tmp_path):
    # The org source (claude.json) being invalid JSON surfaces distinctly from the
    # credentials.json read failure (separate except-clause: JSONDecodeError on the
    # SECOND file, not the first).
    cred, cj = _write_creds(tmp_path)
    cj.write_text("{not json either")
    with pytest.raises(CredentialsError, match="not valid JSON"):
        load_credentials(cred, cj)


def test_load_credentials_non_utf8_claude_json(tmp_path):
    # Valid creds, but the claude.json (org source) isn't UTF-8: the read raises
    # UnicodeDecodeError (a ValueError) — it must surface as CredentialsError, not
    # bubble out as a raw decode error.
    cred, cj = _write_creds(tmp_path)
    cj.write_bytes(b"\xff\xfe\x00not utf-8")
    with pytest.raises(CredentialsError, match="could not read"):
        load_credentials(cred, cj)


def test_load_credentials_non_utf8_credentials_file(tmp_path):
    # Symmetric to the above: the FIRST read (credentials.json) being non-UTF-8
    # must also surface as CredentialsError rather than a raw UnicodeDecodeError.
    _, cj = _write_creds(tmp_path)
    bad_cred = tmp_path / "bad.json"
    bad_cred.write_bytes(b"\xff\xfe\x00not utf-8")
    with pytest.raises(CredentialsError, match="could not read"):
        load_credentials(bad_cred, cj)


# ----- find_ghosts (pure) ----------------------------------------------


def test_find_ghosts_classification():
    envs = [
        _env("env_cloud", "cloud"),  # NEVER a ghost
        _env("env_live", "bridge", "/live"),  # has a live bridge -> keep
        _env("env_dead", "bridge", "/dead"),  # no live bridge -> ghost
        _env("env_nodir", "bridge", None),  # unattributable -> skip
        _env("env_other", "other", "/dead"),  # not a bridge -> skip
    ]
    ghosts = find_ghosts(envs, {"/live"})
    assert [g.id for g in ghosts] == ["env_dead"]


def test_find_ghosts_never_reaps_cloud_even_if_dir_dead():
    envs = [_env("env_default", "cloud", "/gone")]
    assert find_ghosts(envs, set()) == []


def test_find_ghosts_empty_live_set_reaps_all_bridges():
    envs = [_env("env_a", "bridge", "/a"), _env("env_b", "bridge", "/b")]
    assert {g.id for g in find_ghosts(envs, set())} == {"env_a", "env_b"}


# ----- client (mock transport) -----------------------------------------


def _client(responses):
    mt = MockTransport(responses)
    return EnvironmentsClient(Credentials("tokX", "org9"), transport=mt), mt


def test_client_list_paginates():
    client, mt = _client(
        [
            (
                200,
                {
                    "data": [{"id": "env_1", "config": {"type": "bridge", "directory": "/a"}}],
                    "next_page": "env_1",
                },
            ),
            (
                200,
                {
                    "data": [{"id": "env_2", "config": {"type": "cloud"}}],
                    "next_page": None,
                },
            ),
        ]
    )
    envs = client.list_environments()
    assert [e.id for e in envs] == ["env_1", "env_2"]
    assert "after_id=env_1" in mt.calls[1][1]  # the second page request paginated


def test_client_sends_required_headers():
    client, mt = _client([(200, {"data": [], "next_page": None})])
    client.list_environments()
    h = mt.calls[0][2]
    assert h["anthropic-beta"] == BETA_HEADER
    assert h["x-organization-uuid"] == "org9"
    assert h["Authorization"] == "Bearer tokX"


def test_client_archive_and_delete_paths():
    client, mt = _client([(200, b""), (204, b"")])
    client.archive_environment("env_x")
    client.delete_environment("env_y", force=True)
    assert mt.calls[0][0] == "POST" and mt.calls[0][1].endswith("/v1/environments/env_x/archive")
    assert mt.calls[1][0] == "DELETE" and "force=true" in mt.calls[1][1]


def test_client_maps_http_error():
    client, _ = _client([(409, b'{"error":"work in queue"}')])
    with pytest.raises(EnvironmentsAPIError) as ei:
        client.delete_environment("env_z")
    assert ei.value.status == 409


def test_client_list_pagination_cycle_guard():
    # A repeated next_page cursor must terminate, not loop forever (which would
    # also exhaust the mock into an IndexError on a 3rd request).
    def page(nxt):
        return {"data": [{"id": "env_1", "config": {"type": "bridge"}}], "next_page": nxt}

    client, mt = _client([(200, page("c1")), (200, page("c1"))])
    envs = client.list_environments()
    assert len(mt.calls) == 2  # stopped on the repeated cursor, no 3rd request
    assert len(envs) == 2


def test_client_list_pagination_hits_page_ceiling(monkeypatch, caplog):
    # An endpoint that hands back an ever-new cursor must terminate at the page
    # ceiling (not loop unbounded) and return the partial list it collected, with a
    # warning so the truncation isn't silent. Shrink the ceiling to keep it fast.
    monkeypatch.setattr(environments, "_MAX_LIST_PAGES", 3)

    counter = {"n": 0}

    def transport(method, url, headers, body):
        counter["n"] += 1
        payload = {
            "data": [{"id": f"env_{counter['n']}", "config": {"type": "bridge"}}],
            "next_page": f"cursor_{counter['n']}",  # always fresh -> never the cycle/None exit
        }
        return 200, json.dumps(payload).encode()

    client = EnvironmentsClient(Credentials("tokX", "org9"), transport=transport)
    with caplog.at_level("WARNING", logger="clauster.environments"):
        envs = client.list_environments()
    assert counter["n"] == 3  # stopped exactly at the ceiling
    assert len(envs) == 3  # returned the partial list collected so far
    assert any("ceiling" in r.message for r in caplog.records)  # surfaced, not silent


def test_client_request_non_json_body_raises():
    # A 2xx with a non-JSON body surfaces as an API error, not a bare JSONDecodeError.
    client, _ = _client([(200, b"<html>502 Bad Gateway</html>")])
    with pytest.raises(EnvironmentsAPIError, match="non-JSON"):
        client.list_environments()


def test_https_transport_rejects_non_https():
    with pytest.raises(EnvironmentsAPIError, match="non-https"):
        environments._https_transport("GET", "file:///etc/passwd", {}, None)


# ----- live_bridge_directories (fail-closed) ---------------------------


def test_live_dirs_from_sessions(monkeypatch):
    from clauster.models import WorkingSession

    s = WorkingSession(pid=1, cwd=Path("/x"), kind="interactive", started_at=1, local_uuid="u")
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda b: [s])
    # live_bridge_directories stores str(cwd); compare against the platform's own
    # rendering ("/x" on POSIX, "\\x" on Windows) rather than a hardcoded literal.
    assert str(Path("/x")) in environments.live_bridge_directories("claude")


def test_live_dirs_includes_live_pointer_projects(monkeypatch, tmp_path):
    from clauster.models import Project

    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda b: [])
    proj = Project(name="p", path=tmp_path / "p")
    monkeypatch.setattr("clauster.discovery.discover_projects", lambda root: [proj])
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: object())
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)
    dirs = environments.live_bridge_directories("claude", tmp_path)
    assert str(tmp_path / "p") in dirs


def test_live_dirs_skips_projects_without_a_live_pointer(monkeypatch, tmp_path):
    # Both non-live cases in one pass, so a dead pointer can't shield a ghost:
    #   * a project with NO pointer (None) → the `pointer is not None` guard short-circuits
    #   * a project with a pointer that is DEAD → `is_live(...)` returns False
    # Exercises BOTH sides of the `pointer is not None and is_live(pointer)` guard.
    from clauster.models import Project

    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda b: [])
    none_proj = Project(name="nopointer", path=tmp_path / "nopointer")
    dead_proj = Project(name="deadpointer", path=tmp_path / "deadpointer")
    monkeypatch.setattr(
        "clauster.discovery.discover_projects", lambda root: [none_proj, dead_proj]
    )
    # None for the first project, a (dead) pointer for the second.
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: None if path == none_proj.path else object(),
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: False)  # the pointer is dead
    dirs = environments.live_bridge_directories("claude", tmp_path)
    assert dirs == set()  # neither a missing nor a dead pointer counts as a live dir


def test_live_dirs_propagates_probe_failure(monkeypatch):
    # Critical: a failed liveness probe must RAISE, not return an empty set (which
    # would make every bridge look like a ghost and risk mass-reaping live ones).
    monkeypatch.setattr(
        "clauster.inspector.list_working_sessions",
        lambda b: (_ for _ in ()).throw(RuntimeError("agents --json broke")),
    )
    with pytest.raises(RuntimeError):
        environments.live_bridge_directories("claude")


# ----- CLI (everything mocked; no network) -----------------------------


def _cfg(write_config, tmp_path):
    return str(write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n"))


def _patch_creds(monkeypatch):
    monkeypatch.setattr(environments, "load_credentials", lambda **k: Credentials("t", "o"))


def test_cli_reap_dry_run(write_config, tmp_path, monkeypatch, capsys):
    _patch_creds(monkeypatch)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self):
            return [_env("env_dead", "bridge", "/dead")]

        def archive_environment(self, i):
            raise AssertionError("dry-run must not archive")

        def delete_environment(self, i, **k):
            raise AssertionError("dry-run must not delete")

    monkeypatch.setattr(environments, "EnvironmentsClient", FakeClient)
    monkeypatch.setattr(environments, "live_bridge_directories", lambda b, pr: set())
    rc = cli.main(["reap-environments", "-c", _cfg(write_config, tmp_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "dry-run" in err and "env_dead" in err


def test_cli_reap_archive(write_config, tmp_path, monkeypatch):
    _patch_creds(monkeypatch)
    archived = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self):
            return [_env("env_dead", "bridge", "/dead"), _env("env_default", "cloud")]

        def archive_environment(self, i):
            archived.append(i)

    monkeypatch.setattr(environments, "EnvironmentsClient", FakeClient)
    monkeypatch.setattr(environments, "live_bridge_directories", lambda b, pr: set())
    rc = cli.main(["reap-environments", "-c", _cfg(write_config, tmp_path), "--archive"])
    assert rc == 0
    assert archived == ["env_dead"]  # cloud env never touched


def test_cli_reap_aborts_when_live_probe_fails(write_config, tmp_path, monkeypatch):
    _patch_creds(monkeypatch)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self):
            return [_env("env_dead", "bridge", "/dead")]

        def archive_environment(self, i):
            raise AssertionError("must not reap when live set unknown")

    monkeypatch.setattr(environments, "EnvironmentsClient", FakeClient)
    monkeypatch.setattr(
        environments,
        "live_bridge_directories",
        lambda b, pr: (_ for _ in ()).throw(RuntimeError("probe down")),
    )
    rc = cli.main(["reap-environments", "-c", _cfg(write_config, tmp_path), "--archive"])
    assert rc == 2  # fail-closed: refuses to reap without a trustworthy live set


def test_cli_reap_bad_credentials_exit_2(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(
        environments,
        "load_credentials",
        lambda **k: (_ for _ in ()).throw(CredentialsError("no token")),
    )
    rc = cli.main(["reap-environments", "-c", _cfg(write_config, tmp_path)])
    assert rc == 2


def test_cli_reap_force_delete(write_config, tmp_path, monkeypatch):
    _patch_creds(monkeypatch)
    deleted = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self):
            return [_env("env_dead", "bridge", "/dead")]

        def delete_environment(self, i, force=False):
            deleted.append((i, force))

    monkeypatch.setattr(environments, "EnvironmentsClient", FakeClient)
    monkeypatch.setattr(environments, "live_bridge_directories", lambda b, pr: set())
    rc = cli.main(["reap-environments", "-c", _cfg(write_config, tmp_path), "--force-delete"])
    assert rc == 0 and deleted == [("env_dead", True)]


def test_cli_reap_no_ghosts(write_config, tmp_path, monkeypatch, capsys):
    _patch_creds(monkeypatch)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self):
            return [_env("env_live", "bridge", "/live")]

    monkeypatch.setattr(environments, "EnvironmentsClient", FakeClient)
    monkeypatch.setattr(environments, "live_bridge_directories", lambda b, pr: {"/live"})
    rc = cli.main(["reap-environments", "-c", _cfg(write_config, tmp_path)])
    assert rc == 0 and "0 ghost" in capsys.readouterr().err


def test_cli_reap_archive_failure_exit_1(write_config, tmp_path, monkeypatch):
    _patch_creds(monkeypatch)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self):
            return [_env("env_dead", "bridge", "/dead")]

        def archive_environment(self, i):
            raise environments.EnvironmentsAPIError(500, "boom")

    monkeypatch.setattr(environments, "EnvironmentsClient", FakeClient)
    monkeypatch.setattr(environments, "live_bridge_directories", lambda b, pr: set())
    rc = cli.main(["reap-environments", "-c", _cfg(write_config, tmp_path), "--archive"])
    assert rc == 1


def test_cli_reap_list_error_exit_2(write_config, tmp_path, monkeypatch):
    _patch_creds(monkeypatch)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def list_environments(self):
            raise environments.EnvironmentsAPIError(401, "unauthorized")

    monkeypatch.setattr(environments, "EnvironmentsClient", FakeClient)
    rc = cli.main(["reap-environments", "-c", _cfg(write_config, tmp_path)])
    assert rc == 2


def test_https_transport_forwards_valid_url_to_roundtrip(monkeypatch):
    # environments.py 155->157: a well-formed https URL passes the scheme guard and
    # reaches the (stubbed — never real network) round-trip with its parsed parts.
    seen: dict = {}

    def _fake_roundtrip(method, parts, headers, body, timeout):
        seen.update(method=method, netloc=parts.netloc, path=parts.path, query=parts.query)
        return (200, b"{}")

    monkeypatch.setattr(environments, "_https_roundtrip", _fake_roundtrip)
    status, body = environments._https_transport(
        "GET", "https://api.example.com/v1/envs?limit=1", {"h": "v"}, None
    )
    assert (status, body) == (200, b"{}")
    assert seen["method"] == "GET" and seen["netloc"] == "api.example.com"
    assert seen["path"] == "/v1/envs" and seen["query"] == "limit=1"
