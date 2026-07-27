"""Tests for the versioned `/api/v1` public surface + OpenAPI gating (#302).

Covers the DRY route-mirroring (`_mirror_v1_routes`), the public/internal split
(the v1 alias exists for the documented resource subset and nowhere else), the
OpenAPI docs/schema off-by-default + Bearer-gated-when-on posture, and the
named-token auth path (issue via the DB store, legacy `api_token_hash` folded
in, immediate revocation with no in-process cache to go stale).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clauster import auth
from clauster.app import _V1_PUBLIC_ROUTES, _mirror_v1_routes, create_app
from clauster.config import load_config
from clauster.runner import SessionRunner

ORIGIN = "http://testserver"  # TestClient's default origin


def _client(write_config, extra: str = "") -> TestClient:
    config = load_config(write_config(extra))
    return TestClient(create_app(config))


# ----- _mirror_v1_routes (unit) --------------------------------------------


def test_mirror_v1_routes_reuses_the_same_endpoint_object():
    app = FastAPI()

    @app.get("/api/thing")
    async def get_thing() -> dict:
        return {"ok": True}

    _mirror_v1_routes(app, frozenset({("GET", "/api/thing")}))
    v1_routes = [r for r in app.router.routes if getattr(r, "path", None) == "/api/v1/thing"]
    assert len(v1_routes) == 1
    assert v1_routes[0].endpoint is get_thing  # same callable, not a copy


def test_mirror_v1_routes_raises_when_a_target_is_missing():
    app = FastAPI()

    @app.get("/api/thing")
    async def get_thing() -> dict:  # pragma: no cover - never called
        return {"ok": True}

    with pytest.raises(RuntimeError, match=r"/api/nonexistent"):
        _mirror_v1_routes(app, frozenset({("GET", "/api/nonexistent")}))


def test_v1_public_routes_are_a_documented_fixed_set():
    # Guards against silent scope creep: any change to the public surface must
    # touch this test, not slip in unnoticed.
    assert _V1_PUBLIC_ROUTES == frozenset(
        {
            ("GET", "/api/projects"),
            ("GET", "/api/sessions"),
            ("GET", "/api/sessions/tracked"),
            ("GET", "/api/sessions/adoptable"),
            ("GET", "/api/instances"),
            ("POST", "/api/instances"),
            ("GET", "/api/instances/{instance_id}"),
            ("DELETE", "/api/instances/{instance_id}"),
            ("POST", "/api/instances/{instance_id}/resume"),
            ("GET", "/api/agents"),
            ("POST", "/api/agents"),
            ("DELETE", "/api/agents/{job_id}"),
            ("POST", "/api/agents/{job_id}/resume"),
        }
    )


# ----- /api/v1 aliasing (end-to-end) ----------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/projects",
        "/api/sessions",
        "/api/sessions/tracked",
        "/api/sessions/adoptable",
        "/api/instances",
        "/api/agents",
    ],
)
def test_v1_get_alias_returns_the_same_payload_as_the_internal_route(write_config, path):
    client = _client(write_config)
    internal = client.get(path)
    v1 = client.get("/api/v1" + path[len("/api") :])
    assert v1.status_code == internal.status_code == 200
    assert v1.json() == internal.json()


@pytest.mark.parametrize(
    "path",
    [
        # HTML-fragment / template routes — never versioned.
        "/api/projects/alpha/row",
        "/api/widget",
        # Per-session routes that stay internal-only per the resolved scope.
        "/api/instances/some-id/message",
        "/api/instances/some-id/permissions/req-1",
        "/api/instances/some-id/forget",
        "/api/instances/some-id/qr",
    ],
)
def test_v1_does_not_alias_internal_only_routes(write_config, path):
    client = _client(write_config)
    v1_path = "/api/v1" + path[len("/api") :]
    resp = client.get(v1_path)
    assert resp.status_code == 404


def test_v1_instance_delete_and_resume_are_aliased(write_config):
    client = _client(write_config)
    # No live instance exists, so both 404 the same way through either surface —
    # proving the v1 route reaches the identical handler, not just "some 404".
    # No Origin header on purpose: auth is off here, and the CSRF Origin gate now
    # runs on the auth-off path too — a *present* cross-site Origin would 403 before
    # the handler. An absent Origin (a non-browser client) passes, reaching the route.
    internal_delete = client.delete("/api/instances/ghost")
    v1_delete = client.delete("/api/v1/instances/ghost")
    assert v1_delete.status_code == internal_delete.status_code
    assert v1_delete.json() == internal_delete.json()

    internal_resume = client.post("/api/instances/ghost/resume")
    v1_resume = client.post("/api/v1/instances/ghost/resume")
    assert v1_resume.status_code == internal_resume.status_code
    assert v1_resume.json() == internal_resume.json()


def test_v1_instance_get_by_id_is_aliased(write_config):
    client = _client(write_config)
    internal = client.get("/api/instances/ghost")
    v1 = client.get("/api/v1/instances/ghost")
    assert v1.status_code == internal.status_code == 404
    assert v1.json() == internal.json()


def test_v1_agent_delete_and_resume_are_aliased(write_config):
    client = _client(write_config)
    # No Origin header: auth is off and the CSRF Origin gate now guards the auth-off
    # path, so a present cross-site Origin would 403 before the handler; an absent one
    # (non-browser client) passes through to the route, which is what this aliasing
    # check needs to reach.
    internal_delete = client.delete("/api/agents/ghost-job")
    v1_delete = client.delete("/api/v1/agents/ghost-job")
    assert v1_delete.status_code == internal_delete.status_code

    internal_resume = client.post("/api/agents/ghost-job/resume")
    v1_resume = client.post("/api/v1/agents/ghost-job/resume")
    assert v1_resume.status_code == internal_resume.status_code


# ----- OpenAPI docs / schema: off by default, Bearer-gated when on ---------


def test_openapi_off_by_default_returns_404(write_config):
    client = _client(write_config)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/redoc").status_code == 404  # never enabled, on purpose


def test_openapi_enabled_open_when_auth_disabled(write_config):
    # auth.enabled stays False (the default) — matches the rest of the app's
    # posture: nothing is gated when auth is off entirely.
    client = _client(write_config, "api:\n  openapi_enabled: true\n")
    assert client.get("/docs").status_code == 200
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/api/v1/projects" in resp.json()["paths"]


def test_openapi_enabled_401_without_bearer_when_auth_enabled(write_config):
    _raw, token_hash = auth.mint_token()
    client = _client(
        write_config,
        f"api:\n  openapi_enabled: true\nauth:\n  enabled: true\n  api_token_hash: {token_hash}\n",
    )
    assert client.get("/docs").status_code == 401
    assert client.get("/openapi.json").status_code == 401


def test_openapi_enabled_200_with_bearer_when_auth_enabled(write_config):
    raw, token_hash = auth.mint_token()
    client = _client(
        write_config,
        f"api:\n  openapi_enabled: true\nauth:\n  enabled: true\n  api_token_hash: {token_hash}\n",
    )
    headers = {"Authorization": f"Bearer {raw}"}
    assert client.get("/docs", headers=headers).status_code == 200
    resp = client.get("/openapi.json", headers=headers)
    assert resp.status_code == 200
    assert "/api/v1/agents" in resp.json()["paths"]


def test_openapi_disabled_404_even_when_auth_enabled_and_authed(write_config):
    raw, token_hash = auth.mint_token()
    client = _client(write_config, f"auth:\n  enabled: true\n  api_token_hash: {token_hash}\n")
    headers = {"Authorization": f"Bearer {raw}"}
    assert client.get("/openapi.json", headers=headers).status_code == 404


# ----- named tokens (DB-backed) at the HTTP layer ---------------------------


def _token_app(runner_config, *, legacy_hash: str | None = None):
    config, claude_json = runner_config
    config.auth.enabled = True
    config.auth.allowed_origins = [ORIGIN]
    if legacy_hash:
        config.auth.api_token_hash = legacy_hash
    runner = SessionRunner(config, claude_json=claude_json)
    return TestClient(create_app(config, runner=runner)), runner


def test_named_token_authenticates_v1_and_legacy_hash_still_works(runner_config):
    legacy_raw, legacy_hash = auth.mint_token()
    client, runner = _token_app(runner_config, legacy_hash=legacy_hash)
    try:
        raw, _record = runner.persistence.api_token_store().issue("ci")

        resp = client.get("/api/v1/instances", headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 200

        legacy_resp = client.get(
            "/api/v1/instances", headers={"Authorization": f"Bearer {legacy_raw}"}
        )
        assert legacy_resp.status_code == 200
    finally:
        runner.persistence.dispose()


def test_named_token_last_used_is_recorded_on_auth(runner_config):
    client, runner = _token_app(runner_config)
    try:
        store = runner.persistence.api_token_store()
        raw, _record = store.issue("ci")
        assert store.list_all()[0].last_used_at is None

        resp = client.get("/api/v1/instances", headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 200
        assert store.list_all()[0].last_used_at is not None
    finally:
        runner.persistence.dispose()


def test_revoked_named_token_denied_immediately(runner_config):
    # No in-process cache: revoking via the store must deny the very next request,
    # not just after a restart.
    client, runner = _token_app(runner_config)
    try:
        store = runner.persistence.api_token_store()
        raw, _record = store.issue("ci")
        headers = {"Authorization": f"Bearer {raw}"}
        assert client.get("/api/v1/instances", headers=headers).status_code == 200

        assert store.revoke("ci") is True
        assert client.get("/api/v1/instances", headers=headers).status_code == 401
    finally:
        runner.persistence.dispose()


def test_unknown_bearer_token_is_401(runner_config):
    client, runner = _token_app(runner_config)
    try:
        resp = client.get(
            "/api/v1/instances", headers={"Authorization": "Bearer clauster_pat_bogus"}
        )
        assert resp.status_code == 401
    finally:
        runner.persistence.dispose()
