"""Session auth and dashboard-driven login-shepherd routes (#1156).

These routes back the browser's authentication surface: the login page and its
password submit, logout (which revokes every outstanding cookie by bumping the
session epoch), the Tier-B step-up re-auth, the lightweight login-status badge, and
the ``/api/login-shepherd/*`` flow that drives a ``claude auth login`` /
``setup-token`` from the dashboard.

The auth posture is unchanged by the move. The signing serializers, the password
hasher, the failed-login throttle, and the ``_cookie_secure`` decision all stay
built in ``create_app`` (the ``security_headers`` middleware and ``_authenticate`` /
``require_elevated`` closures still read them), and are published on ``app.state`` and
injected here through :mod:`clauster.dependencies` so no auth logic changes. ``/login``
stays public (``_is_public``) and ``/login`` + ``/logout`` stay in ``_UI_ONLY_ROUTES``
-- a byte-identical move keeps both. ``/logout`` mutates ``app.state.session_epoch``
directly through its ``Request`` because that scalar is written, not read. The
login-shepherd routes are opt-in (``login_shepherd.enabled``, plus the independent
``allow_setup_token`` for the higher-risk mode) and fail closed with an
invisible-surface 404 when disabled.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import auth, login_shepherd
from ..dependencies import (
    AuthenticateDep,
    ConfigDep,
    CookieSecureDep,
    ElevationSerializerDep,
    LoginSerializerDep,
    LoginShepherdDep,
    LoginStatusCacheDep,
    LoginThrottleDep,
    PasswordHasherDep,
    RenderDep,
)

if TYPE_CHECKING:
    from ..config import ClausterConfig

router = APIRouter()

# Session/elevation cookie names, the elevation unlock window, and the single-user actor.
# Mirror ``app._SESSION_COOKIE`` / ``_ELEVATION_COOKIE`` / ``_ELEVATION_MAX_AGE_SECONDS`` /
# ``_SESSION_USER``: the app.py copies are still read by ``_authenticate`` and
# ``require_elevated`` (which stay there), so both files name the same constants (#1156).
_SESSION_COOKIE = "clauster_session"
_ELEVATION_COOKIE = "clauster_elevation"
_ELEVATION_MAX_AGE_SECONDS = 600  # 10-minute unlock window; re-prove the password after
_SESSION_USER = "admin"  # single-user in v0.2; multi-user is v0.3


def _throttle_key(config: ClausterConfig, request: Request) -> tuple[str | None, bool]:
    """Return the login-throttle key and whether it is shared across users."""
    # Returns (key, shared). Behind a trusted reverse proxy every login shares the
    # proxy's socket IP, so a per-IP limiter becomes global (one attacker locks
    # everyone out). Key on the proxy-asserted user instead — but ONLY when the
    # X-Proxy-Auth HMAC validates that user (the same gate _authenticate uses).
    # The user_header alone is forgeable by any client that can reach a trusted
    # IP, so trusting it bare would let an attacker mint a fresh per-key login
    # budget per fabricated username and evade the limiter entirely. When no
    # HMAC-verified user is present, fall back to the shared proxy IP: mark it
    # shared=True so the per-key hard lock is skipped and only the global backoff
    # applies. In header-only forward-auth mode (#367, require_hmac=False) the
    # user_header is unsigned and therefore forgeable, so we NEVER key on it — the
    # `require_hmac` gate below makes a per-user key structurally unreachable in that
    # mode (verify_proxy_hmac would already fail with no secret, but the explicit gate
    # is defense-in-depth so a future change to the HMAC helper can't reopen the hole).
    rp = config.auth.reverse_proxy
    ip = auth.peer_ip(request)
    if rp.enabled and auth.peer_trusted(ip, rp.trusted_ips):
        remote_user = request.headers.get(rp.user_header)
        if (
            remote_user
            and rp.require_hmac
            and auth.verify_proxy_hmac(
                rp.shared_secret,
                request.headers.get(rp.shared_secret_header),
                remote_user,
                request.method,
                request.url.path,
                rp.hmac_window_seconds,
            )
        ):
            # Namespaced so a proxy user can't collide with a raw IP key. This
            # value only ever keys the rate limiter, never an HTTP response, so
            # semgrep's flask format-string-response rule is a false positive
            # on this non-route helper (bare nosemgrep: the line trips nothing
            # else, and the precise rule id overflows the line-length limit).
            return f"proxy-user:{remote_user}", False  # nosemgrep
        return ip, True  # shared proxy IP — global backoff only, no per-key lockout
    return ip, False


def _require_login_shepherd(config: ClausterConfig, mode: object = None) -> None:
    """Raise 404 unless the shepherd — and, for setup-token, its own opt-in — is enabled."""
    # Fail-closed invisible-surface gate, same shape as the reaper UI and
    # config-write: off by default, 404s (not 403) when disabled so a disabled
    # deployment exposes nothing about the feature's existence.
    #
    # `mode` is an optional second check (#846), mirroring config_write's
    # enabled/allow_user_scope pattern: `setup-token` mints a long-lived
    # CLAUDE_CODE_OAUTH_TOKEN the operator copies out of the browser, so it
    # requires BOTH the base `enabled` flag AND the independent
    # `allow_setup_token` opt-in. When `allow_setup_token` is off, a
    # `setup-token` request 404s with the SAME detail as the base gate —
    # invisible-surface, never a distinct 403 that would leak that the mode
    # exists but is disabled. Runs BEFORE the caller's own body/enum
    # validation (same ordering `config_write.require_capability` uses), so a
    # disabled mode 404s even alongside a malformed request. `login` and the
    # `code`/`status`/`cancel` routes (which call this with no `mode`) need
    # only the base gate.
    if not config.login_shepherd.enabled:
        raise HTTPException(status_code=404, detail="login shepherd is disabled")
    if mode == "setup-token" and not config.login_shepherd.allow_setup_token:
        raise HTTPException(status_code=404, detail="login shepherd is disabled")


# ----- session routes: login, logout, re-auth ------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
async def login_form(
    request: Request,
    config: ConfigDep,
    render: RenderDep,
    authenticate: AuthenticateDep,
) -> Response:
    """Render the login page, redirecting an already-authenticated caller to the dashboard."""
    if (await authenticate(request))[0]:
        return RedirectResponse(f"{config.root_path}/", status_code=303)
    return render(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    config: ConfigDep,
    render: RenderDep,
    throttle: LoginThrottleDep,
    hasher: PasswordHasherDep,
    serializer: LoginSerializerDep,
    cookie_secure: CookieSecureDep,
) -> Response:
    """Verify the submitted password under the login throttle and open a session."""
    throttle_key, throttle_shared = _throttle_key(config, request)
    allowed, retry_after = throttle.allowed(throttle_key, shared=throttle_shared)
    if not allowed:
        resp = render(
            request,
            "login.html",
            {"error": "Too many attempts — please try again later."},
            status_code=429,
        )
        resp.headers["Retry-After"] = str(max(1, int(retry_after) + 1))
        return resp
    form = await request.form()
    if auth.verify_password(hasher, config.auth.password_hash, str(form.get("password", ""))):
        throttle.reset(throttle_key)
        resp = RedirectResponse(f"{config.root_path}/", status_code=303)
        resp.set_cookie(
            _SESSION_COOKIE,
            auth.issue_session(serializer, _SESSION_USER, request.app.state.session_epoch),
            max_age=config.auth.session_max_age_seconds,
            httponly=True,
            # SameSite=Lax (deliberate UX trade-off): a top-level cross-site GET carries
            # the session so a bookmark / inbound link to the dashboard stays logged in.
            # NOT a CSRF hole — every state-changing request is an unsafe method and is
            # independently gated by the strict Origin allowlist (`_origin_allowed`); going
            # Strict would log the user out on every inbound navigation for no real gain.
            samesite="lax",
            secure=cookie_secure(request),
            path=config.root_path or "/",
        )
        return resp
    throttle.record_failure(throttle_key, shared=throttle_shared)
    return render(request, "login.html", {"error": "Incorrect password."}, status_code=401)


@router.post("/logout")
async def logout(request: Request, config: ConfigDep) -> Response:
    """Bump the session epoch so every issued cookie is revoked, then send back to login."""
    # Bump the server-side epoch so the cookie we just dropped — and any
    # copy of it elsewhere — is actually revoked, not merely cleared client
    # side. Single-user today, so this is "log out everywhere".
    # Floor the bump against the in-memory epoch so a transient read error
    # or corrupt session.epoch can never lower it (which would un-revoke).
    request.app.state.session_epoch = await asyncio.to_thread(
        auth.bump_epoch, config.state_dir, request.app.state.session_epoch
    )
    resp = RedirectResponse(f"{config.root_path}/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE, path=config.root_path or "/")
    # The epoch bump above already revokes any outstanding elevation token (#978);
    # clear its cookie too so a stale value doesn't linger in the browser.
    resp.delete_cookie(_ELEVATION_COOKIE, path=config.root_path or "/")
    return resp


@router.post("/api/reauth")
async def reauth(
    request: Request,
    config: ConfigDep,
    throttle: LoginThrottleDep,
    hasher: PasswordHasherDep,
    elevation_serializer: ElevationSerializerDep,
    cookie_secure: CookieSecureDep,
) -> Response:
    """Re-prove the operator password to unlock the Tier-B "Advanced" surface (#978).

    Step-up authentication: the caller is already logged in, but privileged
    config writes require a fresh password proof. On success, set a short-lived
    elevation cookie (``_ELEVATION_MAX_AGE_SECONDS``). Shares the login throttle
    so it can't be brute-forced, and — like login — verifies against a dummy
    hash when no password is set, so "no password configured" isn't a timing
    oracle and reauth simply never succeeds (Tier-B stays locked).
    """
    throttle_key, throttle_shared = _throttle_key(config, request)
    allowed, retry_after = throttle.allowed(throttle_key, shared=throttle_shared)
    if not allowed:
        resp = JSONResponse({"detail": "too many attempts"}, status_code=429)
        resp.headers["Retry-After"] = str(max(1, int(retry_after) + 1))
        return resp
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    password = str(body.get("password", "")) if isinstance(body, dict) else ""
    if auth.verify_password(hasher, config.auth.password_hash, password):
        throttle.reset(throttle_key)
        resp = JSONResponse({"elevated": True, "expires_in": _ELEVATION_MAX_AGE_SECONDS})
        resp.set_cookie(
            _ELEVATION_COOKIE,
            auth.issue_elevation(
                elevation_serializer, _SESSION_USER, request.app.state.session_epoch
            ),
            max_age=_ELEVATION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=cookie_secure(request),
            path=config.root_path or "/",
        )
        return resp
    throttle.record_failure(throttle_key, shared=throttle_shared)
    return JSONResponse({"detail": "incorrect password"}, status_code=401)


# ----- login status (the ops/environments/app-config routes are in routes/ops.py, #1156) -----
@router.get("/api/login-status")
async def api_login_status(login_status_cache: LoginStatusCacheDep) -> dict:
    """Return the cached claude-login state for the dashboard badge (#838).

    A deliberately lightweight companion to ``/healthz``: it returns ONLY the
    three login fields, read straight from the stale-while-revalidate cache
    (``read()`` returns immediately; the background thread does the actual
    ``claude auth status`` probe ≤ once per TTL). Unlike ``/healthz`` it never
    runs ``claude --version`` — so the badge's own poll can hit this every few
    seconds across many tabs without ever spawning a subprocess on the request
    path. ``/healthz`` keeps its login fields for external health consumers; this
    is an additional path for the badge, not a replacement. Auth-gated by the
    guard middleware like every other ``/api/*`` route.
    """
    login = login_status_cache.read()
    return {
        "claude_login_ok": login.logged_in,
        "claude_login_method": login.method,
        "claude_login_expires_at": login.expires_at_ms,
    }


# ----- login shepherd (#839): dashboard-driven `claude auth login` --------------
@router.post("/api/login-shepherd/start")
async def api_login_shepherd_start(
    body: dict, config: ConfigDep, shepherd: LoginShepherdDep
) -> dict:
    """Start a `claude` login or setup-token flow; 409 when one is already active."""
    mode = body.get("mode")
    _require_login_shepherd(config, mode)
    if mode not in ("login", "setup-token"):
        raise HTTPException(status_code=422, detail="mode must be 'login' or 'setup-token'")
    try:
        return await asyncio.to_thread(shepherd.start, mode)
    except login_shepherd.AlreadyActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except login_shepherd.LoginShepherdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/login-shepherd/code")
async def api_login_shepherd_code(
    body: dict, config: ConfigDep, shepherd: LoginShepherdDep
) -> dict:
    """Submit the operator's pasted OAuth code to the active login flow."""
    _require_login_shepherd(config)
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(status_code=422, detail="code must be a non-empty string")
    try:
        return await asyncio.to_thread(shepherd.submit_code, code.strip())
    except login_shepherd.NotActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/login-shepherd/status")
async def api_login_shepherd_status(config: ConfigDep, shepherd: LoginShepherdDep) -> dict:
    """Poll the active flow's outcome, reaping it once it reaches a terminal result."""
    # Poll the eventual outcome after a `pending: true` submit (a slow verification):
    # same shape — `pending: true` while still running, else the terminal result. 409
    # once the flow is gone — the client's cue to stop polling.
    _require_login_shepherd(config)
    try:
        return await asyncio.to_thread(shepherd.poll)
    except login_shepherd.NotActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/login-shepherd/state")
async def api_login_shepherd_state(config: ConfigDep, shepherd: LoginShepherdDep) -> dict:
    """Report whether a flow is open, so the UI can rehydrate after a page reload."""
    # Rehydration read (#1078): the dashboard's login state is per-page-load, so after a
    # reload the client no longer knows a flow is open and never renders Cancel — while
    # the server still refuses /start with 409. This lets the component recover that view
    # on init. Unlike /status it is a GET and never reaps: it cannot race the polling
    # client for a one-time setup-token result. `{"active": false}` when idle — a 200,
    # not a 409, because "no flow" is the expected answer here rather than an error.
    # Behind the same fail-closed gate as every other route in this group.
    #
    # Off-loaded like its siblings even though it only reads a dict: `state()` takes
    # `_flow_lock`, which `start()` holds across a subprocess spawn, so an inline call
    # could park the event loop behind that spawn.
    _require_login_shepherd(config)
    return await asyncio.to_thread(shepherd.state)


@router.post("/api/login-shepherd/cancel")
async def api_login_shepherd_cancel(config: ConfigDep, shepherd: LoginShepherdDep) -> dict:
    """Cancel the active login flow, if one is running."""
    _require_login_shepherd(config)
    await asyncio.to_thread(shepherd.cancel)
    return {"ok": True}
