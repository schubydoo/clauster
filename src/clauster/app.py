"""FastAPI application factory (spec §7)."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import segno
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from . import (
    __version__,
    atomicio,
    auth,
    claude_cli,
    claude_md,
    config_audit,
    config_editor,
    config_write,
    config_write_hooks,
    config_write_mcp,
    config_write_mcp_cli,
    config_write_permissions,
    config_write_plugins,
    config_write_settings,
    config_write_skills,
    config_write_subagents,
    config_writer,
    deps,
    environments,
    login_shepherd,
    login_status,
    logstream,
    ops,
    prometheus,
    pty_screen,
    setup_wizard,
    supervisor,
    usage,
)
from .claude_md import (
    ClaudeMdConflict,
    ClaudeMdError,
    ClaudeMdNotTrusted,
    ClaudeMdTooLarge,
    read_claude_md,
    write_claude_md,
)
from .claustrum_client import ClaustrumError
from .claustrum_daemon import ClaustrumDaemon
from .clone_jobs import CloneJob, CloneJobManager
from .config import BYPASS_DESKTOP_HINT, PERMISSION_LABELS, PERMISSION_MODES, ClausterConfig
from .db.stores import ApiTokenStore
from .discovery import (
    invalidate_discovery_cache,
    is_valid_project_name,
)
from .engine import ClausterEngine
from .hosted import HostedManager, HostedSessionError
from .models import (
    BackgroundJob,
    ClaudeMdDoc,
    InstanceStatus,
    Project,
    RemoteControlInstance,
    WorkingSession,
)
from .provisioning import (
    BlockedCloneHost,
    GitUnavailable,
    InvalidCloneUrl,
    InvalidProjectName,
    ProvisionError,
    TargetExists,
    clone_project,
    create_project,
    validate_clone_url,
)
from .redact import redact_for_disk, sanitize_line
from .runner import (
    AdoptionUnavailable,
    InstanceStillLive,
    InvalidSpawnOption,
    PermissionModeNotAllowed,
    SessionRunner,
    SpawnError,
    UnknownProject,
    _conpty_keeper_available,
)
from .trust import is_trusted

logger = logging.getLogger(__name__)


def _pty_supported() -> bool:
    """Whether Interactive Session (pty) can launch on this host, for the dashboard mode picker.

    POSIX always (`pty.openpty`); on Windows only when the ConPTY keeper's `pywinpty` (the
    `pty` extra) is installed — otherwise a `launch_mode: pty` request falls back to Server
    Mode, so the picker shouldn't offer it (#914). Mirrors the runner's launch-time gate.
    """
    return sys.platform != "win32" or _conpty_keeper_available()


_SESSION_COOKIE = "clauster_session"
# Step-up re-auth cookie for the privileged Tier-B "Advanced" config surface (#978):
# short-lived, distinct from the session cookie, and only ever consulted by the
# Tier-B config-write routes — never a general access credential.
_ELEVATION_COOKIE = "clauster_elevation"
_ELEVATION_MAX_AGE_SECONDS = 600  # 10-minute unlock window; re-prove the password after
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SESSION_USER = "admin"  # single-user in v0.2; multi-user is v0.3

# Result type for _spawn_or_http: both the create and resume routes await a
# SpawnOutcome (#778, #1145) — same exception mapping.
_SpawnT = TypeVar("_SpawnT")

# The OpenAPI docs UI + schema — off by default, gated like any other /api/...
# route when enabled (#302). Kept as a single set so the guard middleware and the
# app-factory wiring share one definition of "which paths are the docs surface".
_DOCS_PATHS = frozenset({"/docs", "/openapi.json"})

# The public, documented `/api/v1` resource subset (#302): projects list, session
# reads, instance spawn/stop/resume, agent spawn/stop/resume. Deliberately
# excludes every HTML-fragment/partial route (`/api/projects/{name}/row`,
# `/api/widget`, template endpoints) and the per-session `message` /
# `permissions` / `forget` / `qr` routes, which stay internal/unversioned only.
_V1_PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
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

# The web-UI surface (#806): the dashboard page, login/logout, and the exact
# "internal HTML-fragment / per-session interactive" route list #302 already
# named above (`/api/projects/{name}/row`, `/api/widget`, and the per-instance
# `message`/`permissions/{request_id}`/`forget`/`qr` routes) — never a superset.
# Every OTHER `/api/...` route (public or internal-but-JSON, e.g. `/api/doctor`,
# `/api/config`, `/api/environments/...`) stays reachable when `ui.enabled` is
# false: this list is deliberately narrow so "API-only" mode keeps the full JSON
# API working, only the browser-rendered surface goes away. `/static/*` is
# gated separately (a path prefix, not a single route) by `_ui_guard_matches`.
_UI_ONLY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/"),
        ("GET", "/login"),
        ("POST", "/login"),
        ("POST", "/logout"),
        ("GET", "/api/projects/{name}/row"),
        ("GET", "/api/widget"),
        ("POST", "/api/instances/{instance_id}/message"),
        ("POST", "/api/instances/{instance_id}/permissions/{request_id}"),
        ("POST", "/api/instances/{instance_id}/forget"),
        ("GET", "/api/instances/{instance_id}/qr"),
    }
)

_ROUTE_PARAM_RE = re.compile(r"\{[^{}]+\}")


def _compile_route_pattern(template: str) -> re.Pattern[str]:
    """Compile a FastAPI-style path template (``{name}``) into an anchored regex.

    Each ``{param}`` segment becomes a ``[^/]+`` match — enough to recognize the
    small, fixed :data:`_UI_ONLY_ROUTES` set against a live request path without
    pulling in Starlette's full route-matching machinery.
    """
    parts: list[str] = []
    last = 0
    for m in _ROUTE_PARAM_RE.finditer(template):
        parts.append(re.escape(template[last : m.start()]))
        parts.append(r"[^/]+")
        last = m.end()
    parts.append(re.escape(template[last:]))
    return re.compile("^" + "".join(parts) + "$")


_UI_ONLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, _compile_route_pattern(path)) for method, path in _UI_ONLY_ROUTES
)


def _is_ui_only_route(method: str, path: str) -> bool:
    """Whether ``(method, path)`` matches an entry in :data:`_UI_ONLY_ROUTES`.

    ``HEAD`` is normalized to ``GET`` before comparing so a ``HEAD`` to a
    GET-only entry (e.g. ``HEAD /``) gates exactly like the ``GET``. Without it
    the kill switch would let ``HEAD`` fall through to the router, which answers a
    ``HEAD`` on a GET-only route with a ``405`` — a confirmable, non-404 response
    that reveals the disabled surface still exists (and, for any route that DID
    accept ``HEAD``, would run its handler). Normalizing makes every UI route
    return a uniform ``404`` regardless of method.
    """
    normalized = "GET" if method == "HEAD" else method
    return any(normalized == m and pattern.match(path) for m, pattern in _UI_ONLY_PATTERNS)


def _ui_guard_matches(method: str, path: str) -> bool:
    """Whether a request hits the web-UI surface gated by ``ui.enabled`` (#806).

    True for ``/static/*`` (a mounted sub-app, not a single route) or any exact
    match in :data:`_UI_ONLY_ROUTES` — the dashboard page, login/logout, and the
    internal HTML-fragment / per-session interactive routes. ``HEAD`` is treated
    as ``GET`` for the route-set match (see :func:`_is_ui_only_route`), so a
    ``HEAD`` can't slip past a GET-only entry as a 405; the ``/static/`` prefix
    match already ignores the method. Everything else (the rest of the JSON API)
    is untouched.
    """
    return path.startswith("/static/") or _is_ui_only_route(method, path)


def _app_local_path(request: Request) -> str:
    """Return the request path with the configured ``root_path`` prefix stripped (#812).

    Both the auth ``guard`` and the ``ui_guard`` classify routes by comparing against
    app-local paths (``/login``, ``/api/…``, ``/static/…``) — the paths FastAPI's router
    matches. ``request.url.path`` is ``scope["path"]`` verbatim: under a reverse proxy
    that does NOT strip the mount prefix it still carries it (``/prefix/login``), which
    would misclassify a public/gated route and, for the UI kill switch, fail **open**.
    Stripping ``root_path`` makes classification correct regardless of whether the proxy
    strips the prefix. The supported prefix-stripping proxy already sends no prefix in
    the path, so the strip is a no-op there. The boundary check (exact match or a ``/``
    after the prefix) avoids stripping a coincidental prefix (``/prefixfoo``). A
    trailing slash on the configured ``root_path`` (``/prefix/``) is normalized off
    first, so it can't defeat the boundary check and leave the prefix un-stripped.
    """
    path = request.url.path
    root = request.scope.get("root_path", "").rstrip("/")
    if root and (path == root or path.startswith(root + "/")):
        return path[len(root) :] or "/"
    return path


def _warn_if_ui_off_locks_out_auth(config: ClausterConfig, api_token_store: ApiTokenStore) -> None:
    """Log a loud startup warning for a `ui.enabled=false` deployment nothing can reach (#806).

    With the web UI off there is no login page, so session-cookie (and password)
    auth is unreachable — only a Bearer token (the legacy ``auth.api_token_hash``
    or a named ``clauster api-token``) or a trusted reverse proxy can still
    authenticate. If ``auth.enabled`` is on and none of those is configured, no
    request could ever pass the guard: a self-inflicted lockout.

    Deliberately **warns, never refuses to start** — the stricter fail-closed
    choice would also brick a deployment that flips `ui.enabled` off before
    minting a token, and there is no way to fix that short of hand-editing the
    config back. This is a judgment call the PR body calls out explicitly for
    the maintainer to reconsider; today it only logs.

    Fail-open on a token-store read error (a DB hiccup): the check degrades to
    "assume no named tokens" rather than raising, since this is advisory only —
    a broken DB already surfaces via other startup/health checks.
    """
    if config.ui.enabled or not config.auth.enabled:
        return
    if config.auth.reverse_proxy.enabled or config.auth.api_token_hash:
        return
    try:
        has_named_token = bool(api_token_store.list_all())
    except OSError:
        has_named_token = False
    if has_named_token:
        return
    logger.warning(
        "clauster: WARNING — ui.enabled is false and auth.enabled is true, but no "
        "credential is configured: no auth.api_token_hash, no named `clauster api-token` "
        "token, and auth.reverse_proxy is off. With the web UI disabled there is no login "
        "page, so session-cookie/password auth is unreachable — nothing can currently "
        "authenticate to this deployment. Mint one with `clauster api-token issue` (or "
        "`clauster hash-token` for the legacy single-token field), or configure "
        "auth.reverse_proxy, before relying on this."
    )


def _mirror_v1_routes(app: FastAPI, public: frozenset[tuple[str, str]]) -> None:
    """Alias the public resource subset under ``/api/v1`` (#302), DRY.

    Must run AFTER every ``/api/...`` route in ``public`` is registered — it
    walks the routes already on ``app`` and, for each ``(method, path)`` match,
    re-registers the SAME ``endpoint`` callable (and its ``status_code`` /
    ``response_model``) under ``/api/v1/...``. No handler is copy-pasted, so the
    v1 alias can never drift from the internal route's behaviour.

    Fails loudly (``RuntimeError``) if any entry in ``public`` matches no
    registered route — a renamed/removed internal route must break the build,
    not silently vanish from the documented v1 surface.
    """
    found: set[tuple[str, str]] = set()
    for route in list(app.router.routes):
        if not isinstance(route, APIRoute):
            continue
        for method in (route.methods or set()) - {"HEAD"}:
            key = (method, route.path)
            if key not in public:
                continue
            found.add(key)
            app.add_api_route(
                "/api/v1" + route.path[len("/api") :],
                route.endpoint,
                methods=[method],
                status_code=route.status_code,
                response_model=route.response_model,
                name=f"v1_{route.name}",
                tags=["v1"],
            )
    missing = public - found
    if missing:
        raise RuntimeError(f"clauster: /api/v1 alias target(s) not found: {sorted(missing)}")


# Content-Security-Policy for every response (defence-in-depth; #428). The CSRF
# Origin gate already blocks cross-origin state changes, so this is a fallback
# layer, not the primary control.
#
# script-src is nonce-gated (#442): each request gets a fresh
# `secrets.token_urlsafe(16)` nonce (see the `security_headers` middleware), the
# inline <script> blocks carry `nonce="{{ csp_nonce }}"`, and the header lists
# `'nonce-<nonce>'` — so 'unsafe-inline' is dropped entirely. (CSP3: once a
# `nonce-…` source is present, browsers IGNORE 'unsafe-inline', so leaving it in
# would be dead config; dropping it is what blocks an injected inline <script>
# that lacks the per-request nonce.) The external alpine.csp.min.js is 'self'-allowed
# and needs no nonce.
#
# style-src is nonce-gated too (#533): the per-request nonce now also gates the
# inline <style> blocks (each carries `nonce="{{ csp_nonce }}"`), and every inline
# style="" *attribute* in the templates has been lifted into a class inside those
# nonce'd <style> blocks — a nonce does NOT cover style attributes, only <style>
# elements, so the attributes had to become classes, not nonce'd. With both done,
# 'unsafe-inline' is dropped from style-src. (Alpine's `:style` bindings must use
# the OBJECT form `{ prop: value }`, which sets individual `element.style`
# properties via CSSOM — CSP does not classify that as an inline style, so it
# needs no nonce and is unaffected. A STRING `:style` is applied via the style
# *attribute* and WOULD be blocked, so all dynamic styling uses the object form.)
#
# script-src no longer carries 'unsafe-eval' (#533): switching to the
# @alpinejs/csp build (alpine.csp.min.js) removes the `new Function()` evaluator
# so Alpine no longer needs eval. Every inline x-* directive that required an
# arrow function, nested property assignment, or .then() callback was moved into
# named methods on the dashboard() / projectRow() component objects, which execute
# in the nonce-gated <script> block — outside CSP's expression restriction.
#
# connect-src is just 'self': the live bridge-log + hosted-session streams open
# same-origin WebSockets, and every browser this app targets matches same-origin
# ws:/wss: under 'self'. A bare ws:/wss: scheme-source would instead permit a
# WebSocket to ANY host — an exfiltration channel under XSS — so the schemes are
# deliberately NOT listed.


def _csp_with_nonce(nonce: str | None) -> str:
    """Build the per-request Content-Security-Policy with nonce-gated script- and style-src.

    ``nonce`` is the per-request ``secrets.token_urlsafe(16)`` value generated in
    the ``security_headers`` middleware. When present, script-src lists
    ``'nonce-<nonce>'`` so the inline <script> blocks that carry the matching
    ``nonce="..."`` attribute execute, and style-src lists the same nonce so the
    inline <style> blocks (which also stamp ``nonce="..."``) apply.
    ``'unsafe-inline'`` is dropped entirely from both (#442 for script-src, #533
    for style-src): it is dead config once a nonce source is present, and dropping
    it is what blocks an injected inline <script>/<style> lacking the per-request
    nonce.

    Fail-closed: when ``nonce is None`` (a defensive degraded path that should not
    occur in normal request flow), both script-src and style-src still omit
    ``'unsafe-inline'`` — a degraded policy is *stricter*, never looser.
    ``'unsafe-eval'`` is dropped (#533): the CSP-friendly Alpine build does not use
    ``new Function()`` to evaluate directives.
    """
    nonce_src = f"'nonce-{nonce}' " if nonce else ""
    style_src = "style-src 'self'" + (f" 'nonce-{nonce}'" if nonce else "")
    return (
        "default-src 'self'; "
        f"script-src 'self' {nonce_src}; "
        f"{style_src}; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )


class LoginThrottle:
    """In-process failed-login limiter: a per-key hard lock + a global backoff fallback.

    The per-key window (``max_failures`` within ``window_seconds``) precisely limits a
    *distinguishable* client — a direct peer IP, or a reverse-proxy-asserted user. But
    behind a trusted reverse proxy that asserts no user, every login shares the proxy's
    socket IP, so a per-IP lock would lock **everyone** out (one attacker DoSing all
    users). For that shared-IP case the caller passes ``shared=True``: the per-key lock
    is skipped and only the **global backoff** applies — once shared-path failures exceed
    ``global_ceiling`` in the window, attempts must wait an exponentially-growing interval
    (surfaced as ``429`` + ``Retry-After``), degrading a flood to a delay rather than a
    blanket lockout a legitimate user can never get past. The two paths are independent: a
    shared-proxy flood never 429s a distinguishable direct client, and vice versa.

    In-process only: the counters reset on restart and are **not** shared across workers
    or replicas. For an internet-exposed deployment a fronting IdP/IAP (or the
    reverse-proxy auth) is the real control; this is brute-force friction, not an
    account-security boundary.
    """

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: int = 300,
        *,
        global_ceiling: int = 20,
        backoff_cap_seconds: float = 60.0,
    ) -> None:
        """Set the per-key threshold/window and the global-backoff ceiling/cap."""
        self._max = max_failures
        self._window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._global_ceiling = global_ceiling
        self._backoff_cap = backoff_cap_seconds

    def allowed(self, key: str | None, *, shared: bool = False) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)`` for a login attempt from ``key``.

        The two paths are independent: a ``shared`` proxy IP is governed only by the
        global backoff, a distinguishable client only by its per-key window — so a
        shared-proxy flood never spills over to 429 a direct client (or vice versa).
        """
        now = time.monotonic()
        if shared:
            # Global backoff: past the ceiling, require an exponentially-growing gap since
            # the last failure (capped), so a shared-proxy-IP flood can't lock everyone out
            # but is still throttled to a crawl.
            self._global = [t for t in self._global if now - t < self._window]
            over = len(self._global) - self._global_ceiling
            if over > 0 and self._global:
                backoff = min(self._backoff_cap, 2.0 ** min(over, 30))
                wait = backoff - (now - self._global[-1])
                if wait > 0:
                    return False, wait
            return True, 0.0
        # Per-key hard lock for a distinguishable client.
        if key:
            recent = [t for t in self._failures.get(key, []) if now - t < self._window]
            if recent:
                self._failures[key] = recent
            else:
                # Evict instead of leaving a permanent ``key: []`` — otherwise a
                # failed-login flood from many distinct IPs leaks one empty entry per IP.
                self._failures.pop(key, None)
            if len(recent) >= self._max:
                return False, float(self._window)
        return True, 0.0

    def record_failure(self, key: str | None, *, shared: bool = False) -> None:
        """Record one failed attempt — globally for a shared proxy IP, else per-key."""
        now = time.monotonic()
        if shared:
            self._global = [t for t in self._global if now - t < self._window]
            self._global.append(now)
        elif key:
            self._failures.setdefault(key, []).append(now)

    def reset(self, key: str | None) -> None:
        """Clear ``key``'s per-key failures (called on a successful login)."""
        if key is not None:
            self._failures.pop(key, None)


_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_DIR / "templates"
_STATIC_DIR = _PKG_DIR / "static"

# One year in seconds — the conventional far-future max-age for fingerprinted assets.
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


class _ImmutableStaticFiles(StaticFiles):
    """StaticFiles that marks assets cacheable-forever (#353).

    Safe because every linked asset is version-busted by the app version (templates
    append ``?v={{ asset_version }}``): a clauster upgrade changes ``__version__``, so
    the URL changes and the browser re-fetches rather than serving a stale bundle.
    Only successful file responses get the header — a 304/404 is left untouched.
    """

    async def get_response(self, path: str, scope: dict) -> Response:  # type: ignore[override]
        """Serve the file, tagging a 200 with the immutable Cache-Control header."""
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = _IMMUTABLE_CACHE
        return response


def _reap_ws_task(task: asyncio.Task) -> None:
    """Retrieve a finished WS helper task's outcome so the loop never warns about it."""
    if not task.cancelled() and task.exception() is not None:
        logger.debug("ws stream helper task ended with %r", task.exception())


def _unresolved_bridge(
    runner: SessionRunner, instance_id: str, not_found_detail: str
) -> HTTPException:
    """Build the error for a bridge reference that didn't resolve (#1099).

    ``409`` when an id **prefix** was ambiguous, ``404`` when nothing matched at all.
    They are genuinely different answers: "no such bridge" versus "several, say which" —
    and the operator can act on the second only if told the candidates. Mirrors the
    ``ambiguous`` reply ``session_status`` already returns on the MCP side.

    Introducing a 409 here regresses nothing: prefixes did not resolve before this
    change, so every request that can reach it used to 404. The caller passes the whole
    ``not_found_detail`` rather than a fragment so each route keeps its existing 404
    wording byte-for-byte — the sites were never consistent about quoting the id, and
    normalizing that here would be an unrelated visible change riding along.
    """
    candidates = runner.bridge_id_candidates(instance_id)
    if candidates:
        return HTTPException(
            status_code=409,
            detail=(
                f"ambiguous instance id {instance_id!r} — matches "
                f"{', '.join(candidates)}; use more characters"
            ),
        )
    return HTTPException(status_code=404, detail=not_found_detail)


# How often the /ws/pty-screen reader re-reads the keeper's screen sidecar. Matched to the
# keeper's _SCREEN_FLUSH_INTERVAL (0.25s) so the poll roughly tracks the publish cadence
# without busy-spinning; frames already seen are skipped by their monotonic ``seq``.
_SCREEN_POLL_INTERVAL = 0.25


async def stream_until_disconnect(
    websocket: WebSocket, stream: Callable[[], Awaitable[None]]
) -> None:
    """Run a send-only WebSocket ``stream`` until it finishes or the client goes away.

    A send-only handler never awaits ``receive()``, so it cannot observe the
    client's disconnect (or the server's shutdown close): blocked on its idle event
    source, it becomes a ghost ASGI task that uvicorn's graceful shutdown waits on
    forever — and its subscription leaks. Race the stream against a receive loop:
    the first ``websocket.disconnect`` cancels the stream; anything else the client
    sends is ignored. Errors from the stream itself (e.g. send-after-close) still
    propagate to the caller.
    """
    stream_task = asyncio.ensure_future(stream())
    recv_task = asyncio.ensure_future(websocket.receive())
    try:
        while True:
            done, _ = await asyncio.wait(
                {stream_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stream_task in done:
                stream_task.result()  # propagate the stream's exception, if any
                return
            if recv_task.result()["type"] == "websocket.disconnect":
                return
            recv_task = asyncio.ensure_future(websocket.receive())
    finally:
        for task in (stream_task, recv_task):
            task.cancel()
            # Reap via callback instead of awaiting here: an await inside this
            # finally re-receives the handler's own in-flight cancellation (the
            # test client / server re-delivers it until its scope exits) and turns
            # a clean close into a cancelled ASGI task.
            task.add_done_callback(_reap_ws_task)


def create_app(config: ClausterConfig, runner: SessionRunner | None = None) -> FastAPI:
    """Build and wire the FastAPI app (routes, middleware, static, bridge poll loop)."""
    runner = runner or SessionRunner(config)
    # Shared read facade (#775): built over the app's own runner so routes and the
    # headless CLI drive one code path. Injected runner ⇒ engine.dispose() is a no-op
    # (the app owns the runner's lifecycle via the poll loop / lifespan).
    engine = ClausterEngine(config, runner=runner)
    # Point the cross-process config/CLAUDE.md write lock at a state-dir directory (not the
    # project dir) BEFORE any request can write, so the CLAUDE.md editor and the config-write
    # path share one flock without littering project dirs with a `CLAUDE.md.lock` (follow-up to
    # #915). Configured here in prod ⇒ the warn-once "unconfigured" path is test-only misuse.
    atomicio.configure_lock_dir(Path(config.state_dir).expanduser() / "locks")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runner.start_poll_loop()  # rediscover running bridges + begin polling
        if config.claustrum.enabled:
            daemon = ClaustrumDaemon(config)
            app.state.claustrum_daemon = daemon
            try:
                await daemon.ensure()  # connect-or-spawn the hosted-channel daemon
                # CL-6: reattach hosted sessions that kept running on the daemon
                # while we were down. Best-effort — a reattach failure is recorded
                # per-session, never blocks startup. Skip if the daemon came up
                # without a live client (nothing to reattach through).
                if daemon.client is not None:
                    await app.state.hosted.reattach_all(daemon.client)
            except ClaustrumError as exc:
                # Fail-closed: the daemon's health carries the error and hosted
                # spawns are refused, but bridges (and startup) are unaffected.
                logger.warning("claustrum daemon unavailable at startup: %s", exc)
        try:
            yield
        finally:
            await app.state.hosted.aclose()  # detach (not stop); sessions survive the restart
            # Login shepherd (#839): reap any in-flight `claude auth login` subprocess so an
            # abandoned (or mid-flow-at-shutdown) login can't outlive the app. `cancel()` is a
            # safe no-op when nothing is active; it's sync and can block on terminate/kill
            # waits, so run it off the event loop. Always set in create_app, so no None guard.
            await asyncio.to_thread(app.state.login_shepherd.cancel)
            daemon = getattr(app.state, "claustrum_daemon", None)
            if daemon is not None:
                await daemon.aclose()  # drop our connection; leave the daemon running
            await runner.shutdown()  # cancel poll task; leave bridges running (survive re-exec)
            runner.persistence.dispose()  # close the DB engine's connection pool

    # OpenAPI docs (#302): off by default (explicit, not the FastAPI implicit
    # default) — `/docs` + `/openapi.json` simply aren't registered as routes
    # unless `api.openapi_enabled` is set. `redoc_url` is always None: the docs
    # surface is one UI (`/docs`), not two undocumented ones. When enabled, both
    # paths are still gated by the `guard` middleware below like any other
    # `/api/...` route.
    _docs_url = "/docs" if config.api.openapi_enabled else None
    _openapi_url = "/openapi.json" if config.api.openapi_enabled else None
    app = FastAPI(
        title="Clauster",
        version=__version__,
        root_path=config.root_path,
        lifespan=lifespan,
        docs_url=_docs_url,
        redoc_url=None,
        openapi_url=_openapi_url,
    )
    app.state.config = config
    app.state.runner = runner
    app.state.claustrum_daemon = None  # set by lifespan when claustrum.enabled
    # #838: login-status cache. `/healthz` reads it synchronously but non-blocking —
    # the `claude auth status` subprocess runs at most once per TTL on a background
    # thread (never on the request path), so the dashboard's 4s poll never stalls on
    # a slow probe and multiple tabs don't spawn overlapping subprocesses.
    app.state.login_status_cache = login_status.LoginStatusCache(
        config.claude.binary, runner.claude_json
    )
    # In-app restart (#483): the entry point (``_run``) sets ``uvicorn_server`` to the
    # live server so ``POST /api/restart`` can request a graceful shutdown; left None
    # under TestClient / non-uvicorn hosts (the endpoint 503s rather than half-restart).
    # ``restart_requested`` is read by ``_run`` after shutdown to decide whether to re-exec.
    app.state.uvicorn_server = None
    app.state.restart_requested = False

    # Hosted-channel sessions (CL-4); always present. The store (CL-6) persists them
    # so a clauster restart can reattach the survivors via lifespan reattach_all.
    # Reuse the runner's persistence container so the process shares one engine and
    # one migration run (#362) — the store keeps the same load()/save() contract.
    def _on_hosted_permission_needed(process_id: str, subtype: str) -> None:
        """Fire the #432 `permission-needed` webhook when a hosted prompt parks.

        Called inline on the hosted stream pump (the event loop). Forwards only the
        session process id and the request subtype — never the prompt body, which can
        carry a tool path/argument; the subtype is redacted defensively. Routes through
        the runner's emitter so it stays fire-and-forget and fail-open (default OFF).
        """
        runner.emit_event(
            "permission-needed",
            {
                "event_type": "permission-needed",
                "process_id": process_id,
                "subtype": sanitize_line(subtype) if subtype else None,
            },
        )
        # Notification channel (#541): the "come look" signal. Carries only the redacted
        # subtype, never the prompt body. Fail-closed + fire-and-forget like the webhook.
        clean = sanitize_line(subtype) if subtype else None
        runner.notify_app_event(
            "permission-needed",
            "clauster: permission needed",
            f"A Direct Session parked a tool-permission prompt ({clean})."
            if clean
            else "A Direct Session parked a tool-permission prompt.",
        )

    app.state.hosted = HostedManager(
        runner.persistence.hosted_state_store(),
        on_permission_needed=_on_hosted_permission_needed,
    )
    # Named public-API bearer tokens (#302): the CLI (`clauster api-token ...`)
    # owns issue/list/rotate/revoke; the running app only ever reads it, on the
    # request hot path via `_authenticate` below.
    api_token_store = runner.persistence.api_token_store()
    _warn_if_ui_off_locks_out_auth(config, api_token_store)
    # Let the poll loop's `agents --json` cross-check recognize our own hosted
    # sessions (claustrum channel) so it never mislabels them EXTERNAL/unmanaged (#592).
    runner.set_hosted_provider(app.state.hosted.list_instances)
    clone_jobs = CloneJobManager()
    app.state.clone_jobs = clone_jobs
    # Login shepherd (#839): single-flight manager for a dashboard-driven `claude
    # auth login` / `claude setup-token`. Constructed unconditionally (cheap, no
    # subprocess yet) — the config gate gets enforced per-request by the routes.
    app.state.login_shepherd = login_shepherd.LoginShepherd(config.claude.binary)
    # Drop a finished clone job after this grace so a client that disconnected
    # mid-clone can reconnect and still read the terminal frame.
    _CLONE_JOB_TTL = 60.0
    # Hold strong refs to in-flight clone tasks so they aren't GC'd mid-run.
    _clone_tasks: set[asyncio.Task] = set()
    templates = Jinja2Blocks(directory=str(_TEMPLATES_DIR))
    # Version-bust the vendored asset URLs so the immutable cache below is safe across
    # upgrades (templates link them as `...?v={{ asset_version }}`).
    templates.env.globals["asset_version"] = __version__
    # Per-vendor "don't autofill" attributes for NON-credential inputs (#1036) — shared with the
    # setup wizard's separate template env (see setup_wizard.NO_AUTOFILL). Baked into the markup so
    # Alpine `x-for` row clones inherit it; password fields deliberately omit it (login autofill).
    templates.env.globals["NO_AUTOFILL"] = setup_wizard.NO_AUTOFILL

    def _render(
        request: Request,
        name: str,
        context: dict | None = None,
        **kwargs,
    ) -> Response:
        """Render a template with the per-request CSP nonce injected (#442).

        Every HTML render must carry ``csp_nonce`` so its inline <script> blocks
        stamp ``nonce="{{ csp_nonce }}"`` and survive the nonce-gated script-src.
        The nonce is *not* a Jinja global — that would freeze one value
        process-wide (a security bug); it is pulled per request from
        ``request.state.csp_nonce`` (set by the ``security_headers`` middleware).
        ``**kwargs`` forwards extras like ``status_code`` to ``TemplateResponse``.
        """
        ctx = dict(context or {})
        ctx["csp_nonce"] = getattr(request.state, "csp_nonce", None)
        return templates.TemplateResponse(request, name, ctx, **kwargs)

    # Compress responses over the threshold — the ~665KB uncompressed Tabler/Alpine
    # bundle and the JSON poll responses both shrink ~4-5x for remote/proxied clients
    # that don't compress at the proxy (invisible on LAN). Below it, the gzip overhead
    # isn't worth it.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", _ImmutableStaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        """Render a friendly HTML 404 for browser navigation; keep JSON for the API.

        An unmatched route otherwise dead-ends a stale/mistyped URL on a bare
        ``{"detail": "Not Found"}`` body with no way back. Only browser GETs to
        non-API paths get the page; ``/api`` + ``/ws`` and JSON clients keep the
        machine-readable error, so the API contract (and its tests) stay intact.
        """
        wants_html = "text/html" in request.headers.get("accept", "")
        # Classify against the app-local path (root_path stripped, #812) — the same path
        # FastAPI routes on — so a prefix-mounted deployment still treats /api + /ws as
        # machine-readable even behind a non-prefix-stripping proxy. Match the bare prefix
        # too: exactly /api or /ws (no trailing slash) is still an API/transport path and
        # must stay JSON, not the HTML page.
        path = _app_local_path(request)
        is_api = path in ("/api", "/ws") or path.startswith(("/api/", "/ws/"))
        if exc.status_code == 404 and wants_html and not is_api:
            return _render(request, "404.html", {}, status_code=404)
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    # ----- auth context (v0.2 foundation, D12/D13) ------------------------
    _root = config.root_path
    _signing_secret = auth.load_or_create_secret(config.state_dir)
    _serializer = auth.make_serializer(_signing_secret)
    # Step-up elevation (#978): same secret, distinct salt — an elevation token can
    # never be presented as a session cookie or vice versa (see make_elevation_serializer).
    _elevation_serializer = auth.make_elevation_serializer(_signing_secret)
    _hasher = auth.make_hasher()
    _allowed_origins = auth.build_allowed_origins(config)
    _throttle = LoginThrottle()
    # Session epoch: cookies embed it at issue; logout bumps it so every
    # outstanding cookie (incl. a captured one) is revoked. Persisted, so a
    # restart doesn't silently un-revoke. Cached on app.state — single uvicorn
    # worker, so the in-memory value is authoritative.
    app.state.session_epoch = auth.read_epoch(config.state_dir)

    async def _authenticate(scope) -> tuple[str | None, bool, bool]:
        """Return (user, via_proxy, via_token) for the request/connection.

        Works for both Request and WebSocket (both expose
        .headers/.cookies/.client/.url). ``via_proxy`` and ``via_token`` mark
        non-cookie credentials that carry no ambient browser state, so the CSRF
        Origin gate exempts them (a captured Origin can't ride them cross-site).
        """
        rp = config.auth.reverse_proxy
        if rp.enabled and auth.peer_trusted(auth.peer_ip(scope), rp.trusted_ips):
            remote_user = scope.headers.get(rp.user_header)
            if rp.require_hmac:
                sig = scope.headers.get(rp.shared_secret_header)
                method = getattr(scope, "method", "GET")  # WS handshake => GET
                if auth.verify_proxy_hmac(
                    rp.shared_secret,
                    sig,
                    remote_user,
                    method,
                    scope.url.path,
                    rp.hmac_window_seconds,
                ):
                    return remote_user, True, False
            elif remote_user:
                # Forward-auth (header-only) mode (#367): a trusted forward-auth proxy
                # (Authelia/authentik/Caddy/Traefik/oauth2-proxy) asserts the user but
                # signs no HMAC. We already proved the peer is in `trusted_ips`, so a
                # present user_header authenticates. via_proxy=True keeps the CSRF Origin
                # exemption (no ambient cookie a cross-site page could ride). The header is
                # only as trustworthy as the proxy's must-strip-inbound discipline — see the
                # require_hmac config doc and docs/networking.md.
                return remote_user, True, False
        # API token (#360, extended #302): an Authorization: Bearer credential,
        # hashed at rest. A token is one more enforced-auth METHOD behind the same
        # auth.enabled master switch — never a bypass of it (the guard still gates
        # on enabled). Two sources, checked cheapest-first:
        #   1. the legacy single `config.auth.api_token_hash` (in-memory, no DB —
        #      kept working forever for backward compat, #302);
        #   2. a named token from the `api_tokens` table (`clauster api-token
        #      issue/rotate`), looked up by exact hash match off-loop so a
        #      revoked/rotated token stops authenticating immediately — no
        #      in-process cache to go stale.
        presented = auth.parse_bearer(scope.headers.get("authorization"))
        if presented:
            if auth.verify_token(presented, config.auth.api_token_hash):
                return _SESSION_USER, False, True
            presented_hash = auth.hash_token(presented)
            if await asyncio.to_thread(api_token_store.is_active_hash, presented_hash):
                await asyncio.to_thread(api_token_store.touch_last_used, presented_hash)
                return _SESSION_USER, False, True
        user = auth.read_session(
            _serializer,
            scope.cookies.get(_SESSION_COOKIE),
            config.auth.session_max_age_seconds,
            current_epoch=app.state.session_epoch,
        )
        return (user, False, False) if user else (None, False, False)

    def _origin_allowed(request: Request) -> bool:
        # Origin only: Referer is spoofable/suppressible (Referrer-Policy, downgrades)
        # so it's not trusted for CSRF. Modern browsers always send Origin on a
        # state-changing fetch/XHR/form POST; its absence => reject.
        origin = request.headers.get("origin")
        if origin is None:
            return False
        return auth.normalize_origin(origin) in _allowed_origins

    def _cookie_secure(request: Request) -> bool:
        mode = config.auth.cookie_secure
        if mode != "auto":
            return mode == "always"
        if request.url.scheme == "https":
            return True
        rp = config.auth.reverse_proxy
        if rp.enabled and auth.peer_trusted(auth.peer_ip(request), rp.trusted_ips):
            return request.headers.get("x-forwarded-proto", "").lower() == "https"
        return False

    def _throttle_key(request: Request) -> tuple[str | None, bool]:
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

    def _is_public(path: str) -> bool:
        return path == "/healthz" or path == "/login" or path.startswith("/static/")

    def _metrics_token_ok(request: Request) -> bool:
        """Whether the request carries the configured `/metrics` scrape token (#352).

        The token is stored as a SHA-256 hash at rest (parity with the API token,
        #473); ``auth.verify_token`` fails closed when no hash is configured and
        constant-time-compares the presented bearer's hash, so a non-ASCII bearer
        yields a clean denial rather than a 500.
        """
        presented = auth.parse_bearer(request.headers.get("authorization"))
        return auth.verify_token(presented, config.observability.metrics_token_hash)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not config.auth.enabled:
            # Auth off (the shipped loopback default) still enforces the CSRF Origin gate
            # on unsafe methods, so a cross-site page the operator visits can't drive the
            # tokenless loopback API — a confused-deputy attack on a loopback-only service.
            # There are NO credentials on this path, so a legitimate non-browser client
            # (CLI/curl/script) sends no Origin and MUST still be allowed: reject ONLY a
            # *present* Origin that isn't allowlisted, never an absent one. Browsers always
            # attach Origin to a cross-origin state-changing fetch/XHR/form-POST and JS can't
            # suppress it, so "Origin absent" is a same-origin or non-browser request, never
            # the cross-site attack, while "Origin present, not allowlisted" is exactly it.
            # (DNS-rebinding hardening via TrustedHostMiddleware is a follow-up: it can't be
            # pinned here without risking legitimate LAN host/IP access to the dashboard.)
            if (
                request.method in _UNSAFE_METHODS
                and request.headers.get("origin") is not None
                and not _origin_allowed(request)
            ):
                return JSONResponse({"detail": "origin check failed"}, status_code=403)
            return await call_next(request)
        user, via_proxy, via_token = await _authenticate(request)
        # CSRF: an unsafe method needs a trusted Origin — unless the credential is
        # non-cookie. A proxy HMAC is already bound to method+path; a Bearer token
        # carries no ambient cookie a cross-site page could ride, and a browser
        # fetch can't set Authorization cross-origin without a preflight we never
        # CORS-allow. Both are exempt; cookie/session requests still need Origin.
        if (
            request.method in _UNSAFE_METHODS
            and not via_proxy
            and not via_token
            and not _origin_allowed(request)
        ):
            return JSONResponse({"detail": "origin check failed"}, status_code=403)
        # Classify against the app-local path (root_path stripped) so route matching is
        # correct even behind a non-prefix-stripping proxy (#812) — a no-op under the
        # supported prefix-stripping proxy.
        path = _app_local_path(request)
        if _is_public(path):
            return await call_next(request)
        if path in _DOCS_PATHS:
            # OpenAPI docs (#302): disabled means the route was never registered
            # (docs_url/openapi_url=None), so let the request fall through to the
            # router's own 404 instead of the login redirect every other HTML path
            # gets below. Enabled means gate exactly like the JSON API — a 401,
            # not a browser redirect (the docs UI is for API clients).
            if not config.api.openapi_enabled:
                return await call_next(request)
            if user is None:
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            return await call_next(request)
        if user is None:
            # A valid scrape token grants /metrics access without a session (Prometheus
            # can't log in). Strictly additive: only /metrics, only on an exact match.
            if path == "/metrics" and _metrics_token_ok(request):
                return await call_next(request)
            if path.startswith("/api/"):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            return RedirectResponse(f"{_root}/login", status_code=303)
        return await call_next(request)

    # Registered AFTER `guard` on purpose (#806): added second, so it is the
    # OUTER of the two and its check runs BEFORE `guard`'s auth logic — the
    # web-UI kill switch must 404 the dashboard surface regardless of
    # `auth.enabled` (including when auth is off entirely, where `guard` itself
    # returns immediately and never reaches this check). Still INNER of
    # `security_headers` below (added third/last), so the standard security
    # headers land on this 404 too, same as every other response.
    @app.middleware("http")
    async def ui_guard(request: Request, call_next):
        # Match on the app-local path (root_path stripped) so the kill switch can't fail
        # OPEN behind a non-prefix-stripping proxy (#812).
        if not config.ui.enabled and _ui_guard_matches(request.method, _app_local_path(request)):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await call_next(request)

    # Registered AFTER `guard` (and `ui_guard`) on purpose: Starlette runs the
    # last-added http middleware OUTERMOST, so this wraps both and stamps the
    # headers even on their early 401/403/404/redirect responses (not just
    # route responses).
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Stamp defence-in-depth security headers on every response (#428).

        Runs for all responses — including the auth guard's 401/403/redirect —
        so the headers are present even on rejected requests. The CSRF Origin
        gate is still the primary control; these are a belt-and-suspenders layer.
        HSTS is emitted only when the connection is HTTPS (reusing the same
        ``_cookie_secure`` detection the session cookie uses), so a plain-HTTP
        LAN deployment never pins a browser to a scheme it can't serve.

        The per-request CSP nonce is generated *before* ``call_next`` so the
        template render inside it can read ``request.state.csp_nonce`` and stamp
        the matching ``nonce="..."`` on its inline <script> blocks; the header is
        then built from the same value afterwards (#442). A fresh
        ``secrets.token_urlsafe(16)`` per request — never a process-wide constant
        — so a leaked nonce can't be replayed against a later response.
        """
        request.state.csp_nonce = secrets.token_urlsafe(16)
        response = await call_next(request)
        headers = response.headers
        # setdefault: never clobber a header a downstream response set on purpose.
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        # same-origin, NOT no-referrer: under `no-referrer` a spec-compliant browser
        # serializes the Origin header of a same-origin <form> POST *navigation* to the
        # literal "null" (Fetch: a non-GET request from a no-referrer document gets a
        # null origin). The CSRF gate (_origin_allowed) then rejects that as not-in-
        # allowlist and 403s the native login/logout forms — the only non-fetch POSTs;
        # the Alpine API uses cors fetch, which always carries the real Origin. On newer
        # Chrome this is deterministic (login/logout simply break). `same-origin` keeps
        # the real Origin on same-origin navigations while still suppressing the referrer
        # cross-origin, preserving the privacy intent of #428. (See #454.) Safe only
        # while no secret travels in a same-origin URL — clauster credentials are all
        # cookie/header-borne (session cookie, Bearer token, proxy HMAC), so a same-origin
        # Referer carries no secret; revisit this if a token/session ever rides a URL.
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault(
            "Content-Security-Policy",
            _csp_with_nonce(getattr(request.state, "csp_nonce", None)),
        )
        if _cookie_secure(request):
            # No includeSubDomains: it would pin every sibling subdomain of the
            # serving host to HTTPS for a year, bricking a plain-HTTP service on a
            # shared parent domain. Scope the policy to clauster's own host only.
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if (await _authenticate(request))[0]:
            return RedirectResponse(f"{_root}/", status_code=303)
        return _render(request, "login.html", {"error": None})

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        throttle_key, throttle_shared = _throttle_key(request)
        allowed, retry_after = _throttle.allowed(throttle_key, shared=throttle_shared)
        if not allowed:
            resp = _render(
                request,
                "login.html",
                {"error": "Too many attempts — please try again later."},
                status_code=429,
            )
            resp.headers["Retry-After"] = str(max(1, int(retry_after) + 1))
            return resp
        form = await request.form()
        if auth.verify_password(_hasher, config.auth.password_hash, str(form.get("password", ""))):
            _throttle.reset(throttle_key)
            resp = RedirectResponse(f"{_root}/", status_code=303)
            resp.set_cookie(
                _SESSION_COOKIE,
                auth.issue_session(_serializer, _SESSION_USER, app.state.session_epoch),
                max_age=config.auth.session_max_age_seconds,
                httponly=True,
                # SameSite=Lax (deliberate UX trade-off): a top-level cross-site GET carries
                # the session so a bookmark / inbound link to the dashboard stays logged in.
                # NOT a CSRF hole — every state-changing request is an unsafe method and is
                # independently gated by the strict Origin allowlist (`_origin_allowed`); going
                # Strict would log the user out on every inbound navigation for no real gain.
                samesite="lax",
                secure=_cookie_secure(request),
                path=_root or "/",
            )
            return resp
        _throttle.record_failure(throttle_key, shared=throttle_shared)
        return _render(request, "login.html", {"error": "Incorrect password."}, status_code=401)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        # Bump the server-side epoch so the cookie we just dropped — and any
        # copy of it elsewhere — is actually revoked, not merely cleared client
        # side. Single-user today, so this is "log out everywhere".
        # Floor the bump against the in-memory epoch so a transient read error
        # or corrupt session.epoch can never lower it (which would un-revoke).
        app.state.session_epoch = await asyncio.to_thread(
            auth.bump_epoch, config.state_dir, app.state.session_epoch
        )
        resp = RedirectResponse(f"{_root}/login", status_code=303)
        resp.delete_cookie(_SESSION_COOKIE, path=_root or "/")
        # The epoch bump above already revokes any outstanding elevation token (#978);
        # clear its cookie too so a stale value doesn't linger in the browser.
        resp.delete_cookie(_ELEVATION_COOKIE, path=_root or "/")
        return resp

    def require_elevated(request: Request) -> None:
        """Fail-closed step-up gate for the privileged Tier-B config surface (#978).

        Raises ``403 {"detail": "reauth_required"}`` unless the request carries a
        valid, unexpired, non-revoked elevation cookie — the caller must have
        re-proved the operator password via ``POST /api/reauth`` within the unlock
        window. Consulted only by Tier-B config-write routes, and always *after*
        the capability/scope gate, so a disabled surface stays a 404 (invisible)
        rather than advertising itself with a 403.
        """
        elevated = auth.read_elevation(
            _elevation_serializer,
            request.cookies.get(_ELEVATION_COOKIE),
            _ELEVATION_MAX_AGE_SECONDS,
            current_epoch=app.state.session_epoch,
        )
        if elevated is None:
            raise HTTPException(status_code=403, detail="reauth_required")

    @app.post("/api/reauth")
    async def reauth(request: Request) -> Response:
        """Re-prove the operator password to unlock the Tier-B "Advanced" surface (#978).

        Step-up authentication: the caller is already logged in, but privileged
        config writes require a fresh password proof. On success, set a short-lived
        elevation cookie (``_ELEVATION_MAX_AGE_SECONDS``). Shares the login throttle
        so it can't be brute-forced, and — like login — verifies against a dummy
        hash when no password is set, so "no password configured" isn't a timing
        oracle and reauth simply never succeeds (Tier-B stays locked).
        """
        throttle_key, throttle_shared = _throttle_key(request)
        allowed, retry_after = _throttle.allowed(throttle_key, shared=throttle_shared)
        if not allowed:
            resp = JSONResponse({"detail": "too many attempts"}, status_code=429)
            resp.headers["Retry-After"] = str(max(1, int(retry_after) + 1))
            return resp
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        password = str(body.get("password", "")) if isinstance(body, dict) else ""
        if auth.verify_password(_hasher, config.auth.password_hash, password):
            _throttle.reset(throttle_key)
            resp = JSONResponse({"elevated": True, "expires_in": _ELEVATION_MAX_AGE_SECONDS})
            resp.set_cookie(
                _ELEVATION_COOKIE,
                auth.issue_elevation(
                    _elevation_serializer, _SESSION_USER, app.state.session_epoch
                ),
                max_age=_ELEVATION_MAX_AGE_SECONDS,
                httponly=True,
                samesite="lax",
                secure=_cookie_secure(request),
                path=_root or "/",
            )
            return resp
        _throttle.record_failure(throttle_key, shared=throttle_shared)
        return JSONResponse({"detail": "incorrect password"}, status_code=401)

    async def list_projects() -> list[Project]:
        # Shared facade (#775): the CLI and this route go through the same
        # discover-then-stamp-bypass path, so the two can't drift.
        return await asyncio.to_thread(engine.list_projects)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        # Unauthenticated callers get only liveness when auth is enabled — don't
        # leak claude version / running count on a public reverse-proxy deploy.
        if config.auth.enabled and (await _authenticate(request))[0] is None:
            return {"status": "ok"}
        try:
            version = await asyncio.to_thread(claude_cli.claude_version, config.claude.binary)
            claude_ok = True
        except Exception as exc:
            # Stay non-throwing and fail closed (claude_ok=False surfaces in the response),
            # but leave a diagnostic line — the one probe in this handler that was silent.
            logger.warning("healthz claude probe failed: %s", exc)
            version = None
            claude_ok = False
        # #838: claude_ok only confirms the binary is invokable, not that the account
        # is authenticated — an expired/absent login lets a bridge spawn and then hang
        # at "Starting" with no upfront signal. `claude auth status --json` is the
        # mechanism-agnostic signal (OAuth / apiKeyHelper / API key / env token all
        # reflected in loggedIn). Read is served from a stale-while-revalidate cache:
        # non-blocking (the subprocess never runs on this request path) and the probe
        # runs at most once per TTL, single-flight. A cold start returns a neutral
        # "unknown" (claude_login_ok=True) so the UI never cries wolf before probing.
        # Only the non-PII loggedIn + authMethod are surfaced (never email/org/token).
        login = app.state.login_status_cache.read()
        result: dict[str, object] = {
            "status": "ok",
            "version": __version__,
            "claude_ok": claude_ok,
            "claude_version": version,
            "claude_login_ok": login.logged_in,
            "claude_login_method": login.method,
            "claude_login_expires_at": login.expires_at_ms,
            "instances_running": runner.running_count(),
        }
        if config.claustrum.enabled:
            daemon = getattr(app.state, "claustrum_daemon", None)
            result["claustrum"] = (
                await daemon.probe() if daemon is not None else {"enabled": True, "running": False}
            )
        return result

    @app.get("/api/login-status")
    async def api_login_status() -> dict:
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
        login = app.state.login_status_cache.read()
        return {
            "claude_login_ok": login.logged_in,
            "claude_login_method": login.method,
            "claude_login_expires_at": login.expires_at_ms,
        }

    def _cached_bridge_samples() -> list[tuple[str, float, int]]:
        """(project, cpu, rss) for each bridge in the server-side metrics cache (#354).

        Reads the runner's snapshot (refreshed off the request path by the metrics
        task), so the scrape does no per-request sampling — O(1), consistent with the
        per-project / batch endpoints. Empty when metrics are disabled (cache stays bare).
        """
        return [
            (project, float(s.get("cpu_percent", 0.0)), int(s.get("rss_bytes", 0)))
            for project, s in runner.metrics_snapshots().items()
        ]

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        # Behind the auth guard unless observability.metrics_token_hash is set (then a
        # valid scrape token grants access without a session — see the guard).
        if not config.observability.prometheus_enabled:
            raise HTTPException(status_code=404, detail="metrics endpoint is disabled")
        projects = await list_projects()
        hosted_live = sum(
            1
            for inst in app.state.hosted.list_instances()
            if inst.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING)
        )
        claustrum_up: bool | None = None
        if config.claustrum.enabled:
            daemon = getattr(app.state, "claustrum_daemon", None)
            claustrum_up = daemon is not None and bool(daemon.status().get("running"))
        body = prometheus.render_metrics(
            version=__version__,
            instances=runner.list_instances(),
            project_count=len(projects),
            bridge_samples=_cached_bridge_samples(),
            crash_counts=runner.crash_counts(),
            hosted_sessions=hosted_live,
            claustrum_up=claustrum_up,
        )
        return Response(content=body, media_type=prometheus.CONTENT_TYPE)

    @app.get("/api/projects")
    async def api_projects() -> list[Project]:
        return await list_projects()

    @app.get("/api/doctor")
    async def api_doctor() -> dict:
        """System-readiness checks for the dashboard preflight panel.

        Surfaces the same diagnostics as the ``clauster doctor`` CLI (claude binary +
        version, login, projects_root, state_dir, git, auth sanity, workspace trust,
        source freshness) as JSON, so the browser can show a ✓/⚠/✗ checklist up front
        instead of letting a precondition fail silently at spawn time. The CLI's
        listen-port probe is omitted here (``check_port=False``): this server holds the
        port, so it would always false-warn "already in use", and the port isn't a
        bridge prerequisite anyway.

        Read-only and auth-gated by the guard middleware. Runs off the event loop:
        doctor does blocking subprocess probes (``claude --version``, ``git``). It
        re-reads the config from ``source_path`` (same as the CLI), so it also reflects
        on-disk edits made since boot; an env-only deploy with no config file surfaces a
        single ``config`` FAIL, which is the honest result.
        """
        src = config.source_path
        # check_port=False: this server holds config.port, so the availability probe
        # would always warn "already in use (Clauster already running?)" — a false
        # positive in the dashboard (the port isn't a bridge prerequisite either).
        checks, ok = await asyncio.to_thread(
            ops.run_doctor, str(src) if src is not None else None, check_port=False
        )
        return {
            "ok": ok,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        }

    @app.get("/api/projects/preflight")
    async def api_projects_preflight() -> dict:
        """Batch per-project preflight for first paint — ONE discovery scan, not N.

        First paint needs every project's readiness pill; fetching them one-by-one
        re-ran ``list_projects()`` per project (O(N²) discovery on load). This returns
        the same ``{ok, checks}`` shape keyed by project name from a single scan, so
        the dashboard fires one request instead of N. Declared before the
        ``{name}/preflight`` route so the literal path wins the match. Read-only,
        auth-gated. The per-project route stays for the fragment-inserted-row path.

        Each project's checks (including the #837 MCP-approval check, which reads
        ``.mcp.json`` + ``~/.claude.json``) run in a worker thread — real file I/O,
        so it must not block the event loop.
        """
        result: dict[str, dict] = {}
        for proj in await list_projects():
            checks = await asyncio.to_thread(
                ops.project_preflight_checks, proj, runner.claude_json
            )
            result[proj.name] = {
                "ok": all(c.status != ops.FAIL for c in checks),
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail} for c in checks
                ],
            }
        return result

    @app.get("/api/projects/sortmeta")
    async def api_projects_sortmeta() -> dict:
        """Batch per-project sort keys (last-used + cost) for the Projects sort control.

        Returns ``{name: {last_used: iso|null, cost_usd: float|null}}`` for every
        discovered project, read from the session-history rollup. Powers the
        dashboard's opt-in sort dropdown (name / last-used / cost); the client sorts
        client-side, so this is advisory and read-only. Declared before the
        ``{name}/…`` routes so the literal path wins the match. ``sortmeta_for_all``
        reads every project in one session (two grouped queries, not the old per-project
        N+1) and already degrades to empty on a DB error, so a sort never crashes the
        dashboard; the try/except is just an outer net for an engine/IO fault. Invalid
        project names are dropped before use.
        """
        names = [p.name for p in await list_projects() if is_valid_project_name(p.name)]

        def _collect() -> dict[str, dict]:
            store = runner.persistence.session_history_store()
            meta = store.sortmeta_for_all(names)
            out: dict[str, dict] = {}
            for name in names:
                last_used, cost_usd = meta.get(name, (None, None))
                out[name] = {
                    "last_used": last_used.isoformat() if last_used else None,
                    "cost_usd": cost_usd,
                }
            return out

        # Catch only infra (DB engine / IO): a programming bug should surface as a 500
        # (the client falls back to name order on any non-OK response), not be masked as
        # a silently-empty sort.
        try:
            return await asyncio.to_thread(_collect)
        except (OSError, SQLAlchemyError) as exc:
            logger.warning("projects sortmeta read failed, degrading to empty: %s", exc)
            return {}

    @app.get("/api/projects/{name}/preflight")
    async def api_project_preflight(name: str) -> dict:
        """Per-project spawn-readiness checks (the system-wide panel is ``/api/doctor``).

        Reports the preconditions specific to *this* project's bridge launch —
        workspace trust, whether it's a git repo (worktree mode), and whether its
        committed ``.mcp.json`` has servers still awaiting approval (#837) — as the
        same ``{name, status, detail}`` shape the doctor panel consumes. Derived from
        the discovered project (so trust/git match the card); read-only and
        auth-gated. Runs off the event loop: the MCP-approval check does real file
        I/O (``.mcp.json`` + ``~/.claude.json``).
        """
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        proj = next((p for p in await list_projects() if p.name == name), None)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"project {name!r} not found")
        checks = await asyncio.to_thread(ops.project_preflight_checks, proj, runner.claude_json)
        return {
            "project": name,
            "ok": all(c.status != ops.FAIL for c in checks),
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        }

    @app.get("/api/projects/{name}/row", response_class=HTMLResponse)
    async def api_project_row(request: Request, name: str) -> Response:
        """Render one project's row for reactive insertion (no full-page reload).

        Same Jinja partial the dashboard grid loops over — one source of truth.
        ``idx=0`` so a freshly created project is never hidden by the Projects
        search/cap (it renders within the first page).
        """
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        proj = next((p for p in await list_projects() if p.name == name), None)
        if proj is None:
            raise HTTPException(status_code=404, detail=f"project {name!r} not found")
        return _render(
            request,
            "_project_row.html",
            {
                "p": proj,
                "idx": 0,
                "pty_supported": _pty_supported(),
                # Same canonical label map the dashboard grid loop passes through (#685);
                # the standalone row render must carry it too so the launch <select>'s
                # <option> text and the bypass hint render identically here.
                "permission_labels": PERMISSION_LABELS,
                "bypass_desktop_hint": BYPASS_DESKTOP_HINT,
                # Gate the #837 "Resolve in Server approvals" link on config-write being
                # enabled (its target panel + /api/config-write/* routes 404 when off).
                # The full-page render passes this via _dashboard_context(); the fragment
                # route must pass it too, else an undefined Jinja var reads falsy and the
                # link would WRONGLY vanish on dynamically-inserted rows when it IS on.
                "config_write_enabled": config.config_write.enabled,
            },
        )

    @app.get("/api/projects/{name}/usage")
    async def api_project_usage(name: str) -> dict:
        # Read-only cost/token rollup for the dashboard badge. We validate the name
        # for path-component safety but deliberately skip a discovery scan: an
        # unknown-but-safe name simply has no transcripts and rolls up to zero.
        # Transcripts can be huge, so the parse runs off the event loop.
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        # The rollup walks on-disk transcripts; a broken directory or unreadable
        # file (OSError) must surface as a defined "couldn't read" status, not a
        # bare 500. The badge is advisory, so a read failure degrades to 503. Log
        # the full error server-side (it can carry an absolute on-disk path) but
        # return only the static prefix so the path never leaks to the browser.
        try:
            rollup = await asyncio.to_thread(
                usage.aggregate_project_usage_cached,
                config.projects_root / name,
                project_name=name,
            )
        except OSError as exc:
            logger.warning("usage read failed for %r: %s", name, exc)
            raise HTTPException(
                status_code=503, detail="could not read usage transcripts"
            ) from exc
        tot = rollup.totals
        return {
            "project": name,
            "transcripts": rollup.transcript_count,
            "messages": tot.messages,
            "total_tokens": tot.total_tokens,
            # Per-category split so the badge can show a cache-excluded total
            # (token_total_includes_cache) and a breakdown tooltip.
            "token_breakdown": {
                "input": tot.input,
                "output": tot.output,
                "cache_creation": tot.cache_creation,
                "cache_read": tot.cache_read,
            },
            "cost_usd": round(rollup.cost_usd(), 4),
            "approximate": True,  # hand-maintained price table; counts exact, $ ballpark
            "unpriced_models": rollup.unpriced_models(),
            "by_model": {
                model: {
                    "total_tokens": t.total_tokens,
                    "cost_usd": round(usage.cost_usd(model, t) or 0.0, 4),
                }
                for model, t in sorted(rollup.by_model.items())
            },
        }

    def _live_session_uuids(project_path: Path, name: str) -> set[str]:
        """Session ids of currently-running sessions writing into this project's dir.

        Bridge/agent sessions come from the runner's reconcile snapshot (keyed by
        sanitized cwd); hosted (claustrum) sessions run no ``agents --json`` session,
        so their captured session uuid is folded in by project name, status-filtered
        to RUNNING/STARTING (``_instances`` is not pruned on session end). Both are
        in-memory reads that never touch disk, so this liveness join can't fail a
        transcript listing or tail — at worst a just-started/just-stopped session
        flips a poll late. Shared by the list (#614 Part 1) and tail (#614 Part 2)
        routes so they agree on exactly which sessions are live.
        """
        live_uuids = runner.live_session_uuids(project_path)
        live_uuids |= {
            inst.claude_session_uuid
            for inst in app.state.hosted.list_instances()
            if inst.project == name
            and inst.claude_session_uuid
            and inst.status in (InstanceStatus.RUNNING, InstanceStatus.STARTING)
        }
        return live_uuids

    @app.get("/api/projects/{name}/transcripts")
    async def api_project_transcripts(name: str) -> dict:
        """List a project's session transcripts for the read-only viewer (issue #431, #614).

        Returns ``{project, sessions: [{session, mtime, turn_count, live}]}``,
        live-first then newest-first (by mtime). ``session`` is the transcript filename
        stem (the per-session uuid); ``live`` is True when that session id maps to a
        currently-running bridge/agent or hosted session (#614). Mirrors
        :func:`api_project_usage`: the name is validated for path-component safety (422),
        the on-disk walk runs off the event loop, and a broken directory or unreadable
        file (``OSError``) degrades to a defined 503 — never a bare 500 and never echoing
        the on-disk path to the browser. We deliberately skip a discovery scan (an
        unknown-but-safe name simply has no transcripts and lists empty).

        The live set is computed before the off-thread walk from in-memory snapshots
        (the runner's ``agents --json`` reconcile join + the hosted registry); both are
        plain reads and never touch disk, so the liveness cross-reference can't fail the
        listing — at worst a just-started/just-stopped session badges a poll late.
        """
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")

        project_path = config.projects_root / name
        # In-memory liveness join (see _live_session_uuids): never touches disk, so
        # it can't fail the listing — at worst a session badges a poll late.
        live_uuids = _live_session_uuids(project_path, name)

        def _list() -> list[dict]:
            out: list[dict] = []
            for path in usage.transcript_paths_for(project_path):
                try:
                    mtime = path.stat().st_mtime
                    # turn_count AND the resume-picker fields (#303) come from a cached
                    # per-file summary (#1035): an unchanged transcript skips the full
                    # re-parse, so reopening the selector is near-instant. Every derived
                    # turn is redaction-safe (sanitize_line in _line_to_turn).
                    summary = usage.read_transcript_summary(path)
                except FileNotFoundError:
                    # A session removed mid-walk (racing cleanup) is skipped, not fatal.
                    continue
                # Timestamps bound the conversation for the picker's "when · duration"
                # display ("" when the record has none); first_prompt labels it.
                out.append(
                    {
                        "session": path.stem,
                        "mtime": mtime,
                        "turn_count": summary.turn_count,
                        "live": path.stem in live_uuids,
                        "first_prompt": summary.first_prompt,
                        "first_ts": summary.first_ts,
                        "last_ts": summary.last_ts,
                    }
                )
            # Live sessions first (a glance at what's running now), then newest-first
            # within each group so a stable, predictable order survives every poll.
            out.sort(key=lambda s: (not s["live"], -s["mtime"]))
            return out

        # An OSError walking transcripts must surface as a defined "couldn't read"
        # 503, not a 500. Log the full error server-side (it can carry an absolute
        # on-disk path) but return only the static prefix so the path never leaks.
        try:
            sessions = await asyncio.to_thread(_list)
        except OSError as exc:
            logger.warning("transcript list failed for %r: %s", name, exc)
            raise HTTPException(status_code=503, detail="could not read transcripts") from exc
        return {"project": name, "sessions": sessions}

    @app.get("/api/projects/{name}/transcripts/{session}")
    async def api_project_transcript_session(
        name: str,
        session: str,
        cursor: int = 0,
        limit: int = 50,
        order: str = "desc",
        q: str = "",
    ) -> dict:
        """Return one page of a session's turns, ordered + optionally filtered (#431, #612).

        Turns are ``{role, content, model, timestamp}`` already redacted by
        :func:`usage.read_transcript_turns` (every rendered field passes through
        ``redact.sanitize_line`` before it leaves the reader). ``cursor`` is an
        opaque offset into the ordered turn list and ``next_cursor`` is the offset to
        fetch the following page (``None`` at the end).

        ``order`` (``desc`` default, newest-first; ``asc`` flips to oldest-first) and
        the optional ``q=`` substring filter (#612) are applied *before* paging, so a
        toggle/search re-fetches from cursor 0 and pagination still terminates without
        double-rendering. The search matches against the **redacted** ``content`` (the
        text that already left the reader) so it can never be used to confirm a
        redacted secret, and it is case-insensitive. ``total`` reflects the count
        *after* filtering, so the modal's turn count and the load-more terminator
        track the visible set.

        BOTH ``name`` and ``session`` are validated path-safe: the name via the
        project-name regex (422), and ``session`` via
        :func:`usage.resolve_session_transcript`, which fails closed against ``..`` /
        separators and confirms the resolved file sits strictly inside the project's
        transcript dir (a bad/unknown session → 404, never a directory escape). An
        unreadable transcript (``OSError``) degrades to a defined 503 with no path in
        the body — never a bare 500.
        """
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        # Clamp paging args defensively: a negative cursor/limit or an absurd limit
        # must not let a client over-read or index from the tail.
        cursor = max(cursor, 0)
        limit = max(1, min(limit, 500))
        # Normalize the sort: anything that isn't an explicit "asc" stays newest-first
        # (the historical default), so a typo'd order never silently reverses the view.
        ascending = order == "asc"
        # The filter matches the already-redacted content, case-insensitively. An empty
        # (or whitespace-only) term means "no filter" — the full ordered list pages.
        needle = q.strip().casefold()

        def _read() -> dict:
            project_path = config.projects_root / name
            path = usage.resolve_session_transcript(project_path, session)
            if path is None:
                # Fail closed: unsafe or unknown session is a 404, never a path escape.
                raise HTTPException(status_code=404, detail="transcript not found")
            turns = usage.read_transcript_turns(path)
            if not ascending:
                turns.reverse()  # newest-first page order (default)
            if needle:
                # Match against the redacted content only — searching the text that
                # already left the reader means a query can't confirm a masked secret.
                turns = [t for t in turns if needle in (t.get("content") or "").casefold()]
            page = turns[cursor : cursor + limit]
            end = cursor + limit
            next_cursor = end if end < len(turns) else None
            return {
                "project": name,
                "session": session,
                "turns": page,
                "next_cursor": next_cursor,
                "total": len(turns),
            }

        try:
            return await asyncio.to_thread(_read)
        except HTTPException:
            # Re-raise the in-thread 404 unchanged (don't fold it into the 503 below).
            raise
        except (FileNotFoundError, OSError) as exc:
            logger.warning("transcript read failed for %r/%r: %s", name, session, exc)
            raise HTTPException(status_code=503, detail="could not read transcript") from exc

    @app.get("/api/projects/{name}/transcripts/{session}/tail")
    async def api_project_transcript_tail(name: str, session: str, offset: int = 0) -> dict:
        """Return a live session's transcript turns appended since byte ``offset`` (#614 Part 2).

        The front-end opens a session, then — *only while it is live* — polls this
        endpoint on a timer to follow new turns as the agent works (the maintainer's
        decided mechanism: poll the ``.jsonl`` from a known byte offset, not the
        bridge-log WebSocket). Returns
        ``{project, session, turns, offset, reset, live}``:

        - ``turns`` — the renderable turns appended after ``offset``, in **file
          order** (oldest-first append order) so the client appends them to the
          bottom of the tail. Each is already redacted by
          :func:`usage.read_transcript_turns_from_offset` (shared
          ``redact.sanitize_line`` path) — never raw.
        - ``offset`` — the byte position to poll from next. It only advances past
          **complete** lines, so a half-written final record is reparsed next poll
          rather than surfaced as a corrupt/empty turn.
        - ``reset`` — ``True`` when the file shrank below the requested offset
          (rotated/truncated): the read restarts from 0 and the client replaces
          its tail buffer instead of appending.
        - ``live`` — whether the session still maps to a running bridge/agent/hosted
          session. The client stops polling once this is ``False`` (the session
          ended), after draining this final delta.

        Same fail-closed boundary as the paged reader: ``name`` is project-name
        validated (422); ``session`` is resolved strictly inside the project's
        transcript dir via :func:`usage.resolve_session_transcript` (unsafe/unknown
        → 404, never a directory escape); an unreadable transcript (``OSError``)
        degrades to a defined 503 with no on-disk path in the body — never a bare
        500. Read-only throughout: it never writes or mutates the transcript.
        """
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        # Clamp a negative offset to 0 defensively (the reader clamps too, but keep
        # the wire contract clean); a client can't seek to a negative position.
        start = max(offset, 0)
        project_path = config.projects_root / name
        # In-memory liveness join (never disk) — computed before the off-thread read.
        live = session in _live_session_uuids(project_path, name)

        def _tail() -> dict:
            path = usage.resolve_session_transcript(project_path, session)
            if path is None:
                # Fail closed: unsafe or unknown session is a 404, never a path escape.
                raise HTTPException(status_code=404, detail="transcript not found")
            turns, new_offset, reset = usage.read_transcript_turns_from_offset(path, start)
            return {
                "project": name,
                "session": session,
                "turns": turns,
                "offset": new_offset,
                "reset": reset,
            }

        try:
            body = await asyncio.to_thread(_tail)
        except HTTPException:
            raise
        except (FileNotFoundError, OSError) as exc:
            logger.warning("transcript tail failed for %r/%r: %s", name, session, exc)
            raise HTTPException(status_code=503, detail="could not read transcript") from exc
        body["live"] = live
        return body

    @app.get("/api/projects/{name}/metrics")
    async def api_project_metrics(name: str) -> dict:
        # Live CPU/memory/disk for a project's running bridge (dashboard badge). Served
        # from the server-side snapshot the runner's metrics task refreshes every
        # metrics.poll_seconds (#354), so the read is O(1) with no per-request thread —
        # request cost no longer scales with the running-bridge count. A project with no
        # current sample (off, just-started, or stopped) reports {running: false}.
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")
        if not config.metrics.enabled:
            return {"running": False}
        sample = runner.metrics_snapshot(name)
        if sample is None:
            return {"running": False}
        return {"running": True, **sample}

    @app.get("/api/metrics")
    async def api_metrics_batch() -> dict:
        # Batch counterpart to the per-project endpoint (#354): one O(1) read of every
        # running bridge's cached sample, so a dashboard can refresh all badges in a
        # single request instead of one per bridge.
        if not config.metrics.enabled:
            return {}
        return {
            name: {"running": True, **sample}
            for name, sample in runner.metrics_snapshots().items()
        }

    # --- ghost-environment reaper (spec §11), dashboard surface ----------------
    # Destructive first-party API, so: opt-in config gate, fail-closed live set,
    # and every action re-derives the ghost set server-side (see _gather_ghosts).
    def _gather_ghosts() -> tuple[environments.EnvironmentsClient, list, set, list]:
        """Sync: creds → list envs → live set (fail-closed) → ghosts.

        Mirrors the CLI's safety rails. Raises HTTPException on any failure so the
        route never proceeds on partial information.
        """
        try:
            creds = environments.load_credentials(now_ms=int(time.time() * 1000))
        except environments.CredentialsError as exc:
            raise HTTPException(status_code=503, detail=f"credentials unavailable: {exc}") from exc
        client = environments.EnvironmentsClient(creds)
        try:
            envs = client.list_environments()
        except environments.EnvironmentsAPIError as exc:
            raise HTTPException(status_code=502, detail=f"environments API error: {exc}") from exc
        # SAFETY: never reap without a trustworthy live set — an incomplete one could
        # see a live bridge as a ghost. Fail closed on ANY liveness-probe failure.
        try:
            live = environments.live_bridge_directories(config.claude.binary, config.projects_root)
        except Exception as exc:  # noqa: BLE001 — fail closed: any liveness-probe failure must not let a reap proceed
            raise HTTPException(
                status_code=503,
                detail=f"refusing to reap — could not determine live bridges: {exc}",
            ) from exc
        # projects_root scopes the classification: this instance may only reap
        # environments inside its own tree (#1100).
        ghosts = environments.find_ghosts(envs, live, projects_root=config.projects_root)
        return client, envs, live, ghosts

    @app.get("/api/environments/ghosts")
    async def api_environment_ghosts() -> dict:
        if not config.reaper.ui_enabled:
            raise HTTPException(status_code=404, detail="reaper UI is disabled")

        def _work() -> dict:
            _client, envs, live, ghosts = _gather_ghosts()
            return {
                "enabled": True,
                "total": len(envs),
                "live_dirs": len(live),
                "ghosts": [
                    {"id": g.id, "directory": g.config.directory, "name": g.name} for g in ghosts
                ],
            }

        return await asyncio.to_thread(_work)

    @app.post("/api/environments/reap")
    async def api_environment_reap(body: dict) -> dict:
        if not config.reaper.ui_enabled:
            raise HTTPException(status_code=404, detail="reaper UI is disabled")
        action = body.get("action")
        if action not in ("archive", "delete"):
            raise HTTPException(status_code=422, detail="action must be 'archive' or 'delete'")
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise HTTPException(status_code=422, detail="ids must be a non-empty list of strings")
        # Typed-confirm gate; the irreversible force-delete demands the stricter token.
        expected = "DELETE" if action == "delete" else "archive"
        if body.get("confirm") != expected:
            raise HTTPException(status_code=400, detail=f"confirmation text must be {expected!r}")

        def _work() -> dict:
            client, _envs, _live, ghosts = _gather_ghosts()
            # Re-derive server-side: only ever act on ids that are CURRENTLY ghosts.
            # Anything else (a now-live bridge, the cloud Default, an unknown or stale
            # id) is left untouched and reported back as skipped — the client cannot
            # widen the blast radius beyond the freshly-computed ghost set.
            requested = set(ids)
            ghost_ids = {g.id for g in ghosts}
            reaped: list[str] = []
            errors: dict[str, str] = {}
            for g in ghosts:
                if g.id not in requested:
                    continue
                try:
                    if action == "delete":
                        client.delete_environment(g.id, force=True)
                    else:
                        client.archive_environment(g.id)
                    reaped.append(g.id)
                except environments.EnvironmentsAPIError as exc:
                    errors[g.id] = str(exc)
            return {
                "action": action,
                "reaped": reaped,
                "skipped": sorted(requested - ghost_ids),
                "errors": errors,
            }

        return await asyncio.to_thread(_work)

    # ----- login shepherd (#839): dashboard-driven `claude auth login` --------------

    def _require_login_shepherd(mode: object = None) -> None:
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

    @app.post("/api/login-shepherd/start")
    async def api_login_shepherd_start(body: dict) -> dict:
        mode = body.get("mode")
        _require_login_shepherd(mode)
        if mode not in ("login", "setup-token"):
            raise HTTPException(status_code=422, detail="mode must be 'login' or 'setup-token'")
        try:
            return await asyncio.to_thread(app.state.login_shepherd.start, mode)
        except login_shepherd.AlreadyActiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except login_shepherd.LoginShepherdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/login-shepherd/code")
    async def api_login_shepherd_code(body: dict) -> dict:
        _require_login_shepherd()
        code = body.get("code")
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(status_code=422, detail="code must be a non-empty string")
        try:
            return await asyncio.to_thread(app.state.login_shepherd.submit_code, code.strip())
        except login_shepherd.NotActiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/login-shepherd/status")
    async def api_login_shepherd_status() -> dict:
        # Poll the eventual outcome after a `pending: true` submit (a slow verification).
        # Returns the same shape: `pending: true` while still running, or the terminal
        # result (which also reaps the completed flow). 409 once the flow is gone —
        # the client's cue to stop polling.
        _require_login_shepherd()
        try:
            return await asyncio.to_thread(app.state.login_shepherd.poll)
        except login_shepherd.NotActiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/login-shepherd/state")
    async def api_login_shepherd_state() -> dict:
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
        _require_login_shepherd()
        return await asyncio.to_thread(app.state.login_shepherd.state)

    @app.post("/api/login-shepherd/cancel")
    async def api_login_shepherd_cancel() -> dict:
        _require_login_shepherd()
        await asyncio.to_thread(app.state.login_shepherd.cancel)
        return {"ok": True}

    @app.get("/api/config-write/status")
    async def api_config_write_status() -> dict:
        # Foundation surface for the code-executing config-write trust tier (#347/#687).
        # NO concrete config-mutation endpoint exists yet — the children (#688-#691)
        # attach their writers behind this same gate. The capability gate fail-closes:
        # when config_write.enabled is off this 404s (the surface is invisible, same as
        # the reaper), so a disabled deployment exposes nothing. The body reflects only
        # the two opt-in flags, never any config content.
        config_write.require_capability(config, "project")
        return config_write.capability_status(config)

    def _resolve_cw_project(name: object, *, require_exists: bool = False) -> Path:
        # Project-scope config-write: validate-before-I/O path containment. A bad
        # name or an escaping path is a 400 (PathEscapeError), never a write. The
        # name is also the type-the-name confirm token (server-re-derived below).
        # ``require_exists`` is set on the WRITE path only: a contained-but-absent
        # project dir would make the atomic writer's ``mkstemp(dir=path.parent)``
        # raise ``FileNotFoundError`` (an OSError outside the ConfigWriteError guard)
        # → an unhandled 500. Surface it as a clean 404 instead. The READ path leaves
        # it False so a missing dir still reads as an empty server map (harmless).
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=422, detail="body must include a 'project' string")
        try:
            project_dir = config_write.resolve_project_dir(config.projects_root, name)
        except config_write.PathEscapeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if require_exists and not project_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"project directory not found: {name!r}")
        return project_dir

    def _map_config_write_error(exc: config_write.ConfigWriteError) -> HTTPException:
        # Map the Foundation's typed write failures to their fail-closed HTTP codes.
        # InvalidCandidate ⇒ 422 (bad shape), Stale ⇒ 409 (external edit). Everything
        # else — including PathEscapeError, which the route catches earlier as a 400
        # before the writer is even reached — falls through to a 400.
        if isinstance(exc, config_write.InvalidCandidateError):
            return HTTPException(status_code=422, detail=str(exc))
        if isinstance(exc, config_write.StaleConfigWriteError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, config_write_mcp.ServerExistsError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, config_write_mcp.ServerNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, config_write_subagents.AgentNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, config_write_subagents.ReadOnlyAgentError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, config_write_skills.ScriptConfirmRequiredError):
            # A skill upload included non-SKILL.md files without echoing the extra
            # script-body confirm token — a distinct 400 gate on top of the ordinary
            # type-the-name confirm (see config_write_skills' module docstring).
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, config_write_plugins.PluginNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, config_write_plugins.MarketplaceNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    def _config_write_watch(project_dir: Path) -> list[Path]:
        """Return the config files a `claude mcp`/`claude plugin` write could touch.

        A comprehensive candidate set across scopes for the #958 P6 before/after audit
        fingerprint — an unchanged file simply never appears in the diff, so watching a
        superset is harmless and avoids per-scope path guesswork.
        """
        home = runner.claude_json.parent
        return [
            runner.claude_json,
            home / ".claude" / "settings.json",
            home / ".claude" / "plugins" / "known_marketplaces.json",
            project_dir / ".claude" / "settings.json",
            project_dir / ".claude" / "settings.local.json",
            project_dir / ".mcp.json",
        ]

    async def _audit_config_write(
        *, work: Callable[[], None], watch: list[Path], **fields: Any
    ) -> None:
        """Record a committed config-write's audit line + its file/argv side effects (#958 P6).

        Runs ``work`` off-thread, then records the base audit line enriched with (a) which
        watched files it changed — path + sha256 + size, never contents — and (b) the redacted
        ``claude …`` argv any spawned CLI ran.
        Lets :class:`~config_write.ConfigWriteError` propagate (the caller maps it) and records
        ONLY on success. The argv is captured via :data:`config_write.cli_argv_sink`, which
        propagates into the worker thread; the audit append itself is best-effort and never
        fails the already-committed write.

        Best-effort fingerprint, not a transactional attribution: the snapshots bracket the
        write but are not inside its file lock, and ``watch`` is a cross-scope superset, so
        under (rare, single-operator) concurrent writes the diff can attribute another
        request's change. It's a forensic hint of where a change landed — the base line's
        surface/scope/target/action names the operation exactly.
        """
        before = await asyncio.to_thread(config_audit.file_fingerprints, watch)
        sink: list[list[str]] = []
        token = config_write.cli_argv_sink.set(sink)
        try:
            await asyncio.to_thread(work)
        finally:
            config_write.cli_argv_sink.reset(token)
        after = await asyncio.to_thread(config_audit.file_fingerprints, watch)
        extra: dict[str, Any] = {"files": config_audit.diff_fingerprints(before, after)}
        if sink:
            extra["argv"] = sink
        await config_audit.arecord(config.state_dir, extra=extra, **fields)

    @app.get("/api/config-write/mcp")
    async def api_config_write_mcp_read(scope: str = "project", project: str = "") -> dict:
        # Read the (structurally redacted) MCP server map for a surface. Gated exactly
        # like the status route: 404 when config-write is off, and 404 for user scope
        # when allow_user_scope is off — the surface is invisible, never 403. Capability
        # gate FIRST, before the scope-enum check, so a disabled surface 404s for ANY
        # request (a bogus scope included) instead of leaking existence via a differing
        # 422 — the #819/#768 invisible-surface invariant.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            try:
                servers = await asyncio.to_thread(
                    config_write_mcp.read_user_servers, runner.claude_json
                )
            except config_write.ConfigWriteError as exc:
                # A corrupt/non-object/non-UTF-8 ~/.claude.json raises InvalidCandidateError
                # from _load_json_obj — same as the project read below; map it to a clean 422
                # rather than letting it escape as an unhandled 500.
                raise _map_config_write_error(exc) from exc
            return {"scope": "user", "servers": servers, "hash": None}
        if scope == "local":
            project_dir = _resolve_cw_project(project)
            try:
                servers = await asyncio.to_thread(
                    config_write_mcp.read_project_local_servers, runner.claude_json, project_dir
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {"scope": "local", "project": project, "servers": servers, "hash": None}
        project_dir = _resolve_cw_project(project)
        try:
            servers, file_hash = await asyncio.to_thread(
                config_write_mcp.read_project_servers, project_dir
            )
        except config_write.ConfigWriteError as exc:
            # A corrupt/non-object on-disk .mcp.json raises InvalidCandidateError from
            # _load_json_obj. Map it through the same helper as the PUT route so a
            # hand-edited or partially-written file is reported as a clean 422, never
            # an unhandled 500.
            raise _map_config_write_error(exc) from exc
        return {"scope": "project", "project": project, "servers": servers, "hash": file_hash}

    async def _put_config_write(
        body: dict,
        payload_key: str,
        write_user_fn: Callable[..., None],
        write_project_fn: Callable[[Path, dict, str | None], None],
        write_local_fn: Callable[..., None],
        *,
        surface: str,
        get_user_path: Callable[[], Path],
        user_fn_has_hash: bool = True,
        local_fn_has_hash: bool = True,
        get_local_target: Callable[[], Path] | None = None,
    ) -> dict:
        """Shared Foundation pipeline for the three PUT /api/config-write/* routes.

        Order: capability (404, FIRST — invisible-surface #819/#768) → scope-enum (422)
        → confirm (400, FIRST semantic gate) →
        payload shape check (422) → path resolve/contain → stale-hash guard (409) →
        atomic write. Any step aborts before the write.

        ``user_fn_has_hash=False`` is only correct for writers that own their own
        hash/locking mechanism (currently: MCP user scope via ``write_user_servers``).
        ``local_fn_has_hash=False`` is the same shape for the local-scope twin (MCP
        local scope via ``write_project_local_servers``, which nests into
        ``~/.claude.json`` rather than a separate hashable file) — when set,
        ``get_local_target`` supplies the extra positional argument (the
        ``~/.claude.json`` path) the writer needs ahead of ``project_dir``. Every other
        surface should leave both hash flags at the default ``True`` so the stale-hash
        guard is enforced. Any ``"hash"`` key the client sends is intentionally not
        forwarded when the relevant flag is ``False``.
        """
        scope = body.get("scope", "project")
        # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s
        # for ANY request (a bogus scope included), never a differing 422 (#819/#768).
        config_write.require_capability(config, scope)
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            config_write.require_confirm("user", None, body.get("confirm"))
            payload = body.get(payload_key)
            if not isinstance(payload, dict):
                raise HTTPException(
                    status_code=422, detail=f"body must include a '{payload_key}' object"
                )
            user_path = get_user_path()
            if user_fn_has_hash:
                expected: str | None = body.get("hash")
                if expected is not None and not isinstance(expected, str):
                    raise HTTPException(
                        status_code=422, detail="'hash' must be a string when present"
                    )
                try:
                    await asyncio.to_thread(write_user_fn, user_path, payload, expected)
                except config_write.ConfigWriteError as exc:
                    raise _map_config_write_error(exc) from exc
            else:
                # writer owns its own hash/locking (e.g. MCP); "hash" from body is
                # intentionally not forwarded — see user_fn_has_hash docstring above.
                try:
                    await asyncio.to_thread(write_user_fn, user_path, payload)
                except config_write.ConfigWriteError as exc:
                    raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface=surface,
                scope="user",
                target=str(user_path),
                action="update",
                actor=_SESSION_USER,
                keys=sorted(payload),
            )
            return {"scope": "user", "ok": True}
        if scope == "local":
            project = body.get("project")
            config_write.require_confirm("local", project, body.get("confirm"))
            payload = body.get(payload_key)
            if not isinstance(payload, dict):
                raise HTTPException(
                    status_code=422, detail=f"body must include a '{payload_key}' object"
                )
            project_dir = _resolve_cw_project(project, require_exists=True)
            if local_fn_has_hash:
                expected = body.get("hash")
                if expected is not None and not isinstance(expected, str):
                    raise HTTPException(
                        status_code=422, detail="'hash' must be a string when present"
                    )
                try:
                    await asyncio.to_thread(write_local_fn, project_dir, payload, expected)
                except config_write.ConfigWriteError as exc:
                    raise _map_config_write_error(exc) from exc
            else:
                # writer owns its own hash/locking (MCP local scope, nested into
                # ~/.claude.json); "hash" from body is intentionally not forwarded.
                if get_local_target is None:  # pragma: no cover - wiring bug, not user-reachable
                    raise HTTPException(status_code=500, detail="local scope writer misconfigured")
                local_target = get_local_target()
                try:
                    await asyncio.to_thread(write_local_fn, local_target, project_dir, payload)
                except config_write.ConfigWriteError as exc:
                    raise _map_config_write_error(exc) from exc
            # The written file is the project dir's settings file (hash-guarded surfaces) or
            # the ~/.claude.json the MCP local writer nests into; `surface` disambiguates.
            await config_audit.arecord(
                config.state_dir,
                surface=surface,
                scope="local",
                target=str(project_dir if local_fn_has_hash else local_target),
                action="update",
                actor=_SESSION_USER,
                keys=sorted(payload),
            )
            return {"scope": "local", "project": project, "ok": True}
        project = body.get("project")
        config_write.require_confirm("project", project, body.get("confirm"))
        payload = body.get(payload_key)
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=422, detail=f"body must include a '{payload_key}' object"
            )
        project_dir = _resolve_cw_project(project, require_exists=True)
        expected = body.get("hash")
        if expected is not None and not isinstance(expected, str):
            raise HTTPException(status_code=422, detail="'hash' must be a string when present")
        try:
            await asyncio.to_thread(write_project_fn, project_dir, payload, expected)
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface=surface,
            scope="project",
            target=str(project_dir),
            action="update",
            actor=_SESSION_USER,
            keys=sorted(payload),
        )
        return {"scope": "project", "project": project, "ok": True}

    @app.put("/api/config-write/mcp")
    async def api_config_write_mcp_write(body: dict) -> dict:
        return await _put_config_write(
            body,
            "servers",
            surface="mcp",
            write_user_fn=config_write_mcp.write_user_servers,
            write_project_fn=config_write_mcp.write_project_servers,
            write_local_fn=config_write_mcp.write_project_local_servers,
            get_user_path=lambda: runner.claude_json,
            user_fn_has_hash=False,
            local_fn_has_hash=False,
            get_local_target=lambda: runner.claude_json,
        )

    @app.post("/api/config-write/mcp/server")
    async def api_config_write_mcp_server(body: dict) -> dict:
        # CLI-driven add/remove/edit (#769) over the same Foundation gate the PUT
        # (whole-map) route uses. Order mirrors the Foundation docstring exactly:
        # capability (404, FIRST — a disabled surface 404s for ANY request, a bogus
        # scope included, so it never leaks existence via a differing 422; #819/#768)
        # -> scope shape (422) -> confirm (400, FIRST semantic gate, so it fires even
        # against a garbled op/name/entry) -> op/name/entry shape (422) -> path resolve
        # (400/404) -> the CLI/direct-write dispatch itself (409 already-exists, 404
        # not-found, or 400 for any other CLI failure).
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )

        project = body.get("project")
        config_write.require_confirm(
            scope,
            None if scope == "user" else project,
            body.get("confirm"),  # type: ignore[arg-type]
        )

        op = body.get("op")
        if op not in ("add", "remove", "edit"):
            raise HTTPException(status_code=422, detail="op must be 'add', 'remove', or 'edit'")
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(
                status_code=422, detail="body must include a non-empty 'name' string"
            )

        entry = None
        if op in ("add", "edit"):
            entry = body.get("entry")
            if not isinstance(entry, dict):
                raise HTTPException(
                    status_code=422, detail="body must include an 'entry' object for add/edit"
                )
            try:
                config_write.validate_candidate(
                    {name: entry}, config_write_mcp.validate_mcp_servers
                )
            except config_write.InvalidCandidateError as exc:
                raise _map_config_write_error(exc) from exc

        client_secret = body.get("client_secret")
        if client_secret is not None and not isinstance(client_secret, str):
            raise HTTPException(
                status_code=422, detail="'client_secret' must be a string when present"
            )
        # An OAuth client-secret is only deliverable through the CLI (which passes it via
        # MCP_CLIENT_SECRET in the child env). An entry that must bypass the CLI — inline
        # env/headers, or a url carrying a query/userinfo/fragment — takes the direct
        # writer, which has nowhere to put it. Refuse rather than write the entry and
        # silently drop the secret: the operator would believe it was stored and only
        # discover otherwise when the server fails to authenticate.
        if client_secret is not None and entry is not None:
            if config_write_mcp_cli.entry_needs_direct_write(entry):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "'client_secret' cannot be stored for this entry: it carries a "
                        "value that must be kept off the CLI's argv (inline env/headers, "
                        "or a url with a query string, userinfo, or fragment), so it is "
                        "written directly to the config file, which has no way to deliver "
                        "the secret. Put the credential in the entry's 'env' or 'headers' "
                        "instead."
                    ),
                )

        if scope == "user":
            cli_cwd = runner.claude_json.parent
        else:
            cli_cwd = _resolve_cw_project(project, require_exists=True)
        binary = config.claude.binary

        def _direct_write(target_entry: dict, target_op: str) -> None:
            # The #766 direct (non-spawning) writers, one per scope. Used for any entry
            # that must never reach the CLI's argv, and as the edit-rollback restore.
            if scope == "user":
                config_write_mcp.write_user_server_entry(
                    runner.claude_json, name, target_entry, op=target_op
                )
            elif scope == "local":
                config_write_mcp.write_project_local_server_entry(
                    runner.claude_json, cli_cwd, name, target_entry, op=target_op
                )
            else:
                config_write_mcp.write_project_server_entry(
                    cli_cwd, name, target_entry, op=target_op
                )

        def _snapshot_prior() -> dict | None:
            # UNREDACTED single-entry read for the edit-rollback (in-memory, same request,
            # never serialized to a response/log — see config_write_mcp.snapshot_server_entry).
            return config_write_mcp.snapshot_server_entry(
                scope,  # type: ignore[arg-type]
                name,
                claude_json=runner.claude_json,
                project_dir=cli_cwd,
            )

        def _work() -> None:
            if op == "remove":
                config_write_mcp_cli.cli_remove_server(binary, cli_cwd, name, scope)  # type: ignore[arg-type]
                return
            # add / edit always carry an `entry` (validated above); narrow it here so the
            # writers see a concrete dict (defensive — the op-gate guarantees it is set).
            if entry is None:  # pragma: no cover - add/edit always populate `entry` above
                raise RuntimeError("internal: add/edit reached _work with no entry")
            # An entry carrying an inline env/headers value (or a secret-shaped url) can
            # never reach the CLI's argv — err toward the direct #766 writer (same file
            # state, no subprocess). See entry_needs_direct_write.
            if config_write_mcp_cli.entry_needs_direct_write(entry):
                _direct_write(entry, op)
                return
            if op == "add":
                config_write_mcp_cli.cli_add_server(
                    binary,
                    cli_cwd,
                    name,
                    entry,
                    scope,
                    client_secret=client_secret,  # type: ignore[arg-type]
                )
            else:
                # Capture the prior definition BEFORE cli_edit_server runs the remove, so
                # a re-add failure can restore it verbatim via the direct writer (a prior
                # secret is thus never re-exposed on argv). op="edit" overwrites in place.
                prior = _snapshot_prior()

                def _restore() -> bool:
                    # Return whether a prior actually existed and was restored, so
                    # cli_edit_server reports "restored" only when that is true.
                    if prior is None:
                        return False
                    _direct_write(prior, "edit")
                    return True

                config_write_mcp_cli.cli_edit_server(
                    binary,
                    cli_cwd,
                    name,
                    entry,
                    scope,
                    client_secret=client_secret,  # type: ignore[arg-type]
                    restore=_restore,
                )

        # Run the mutation (direct OR CLI-driven) and record the base audit line enriched
        # with which files it changed + the redacted `claude mcp` argv it ran (#958 P6).
        try:
            await _audit_config_write(
                work=_work,
                watch=_config_write_watch(cli_cwd),
                surface="mcp",
                scope=scope,  # type: ignore[arg-type]
                target=name,
                action=op,
                actor=_SESSION_USER,
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        result = {"scope": scope, "name": name, "op": op, "ok": True}
        if scope != "user":
            result["project"] = project
        return result

    @app.get("/api/config-write/mcp/approvals")
    async def api_config_write_mcp_approvals_read(project: str = "") -> dict:
        # Project `.mcp.json` server approvals (#769) are inherently project-scope
        # only — local/user-scope servers carry no approval step, only a committed
        # .mcp.json server does — so this reads/writes at "project" scope alone,
        # gated exactly like the other config-write surfaces (404 when disabled).
        config_write.require_capability(config, "project")
        project_dir = _resolve_cw_project(project)
        approvals = await asyncio.to_thread(
            config_write_mcp.read_project_approvals, runner.claude_json, project_dir
        )
        return {"project": project, **approvals}

    @app.put("/api/config-write/mcp/approvals")
    async def api_config_write_mcp_approvals_write(body: dict) -> dict:
        config_write.require_capability(config, "project")
        project = body.get("project")
        config_write.require_confirm("project", project, body.get("confirm"))
        enabled = body.get("enabled")
        disabled = body.get("disabled")
        if not isinstance(enabled, list) or not isinstance(disabled, list):
            raise HTTPException(
                status_code=422, detail="body must include 'enabled' and 'disabled' lists"
            )
        project_dir = _resolve_cw_project(project, require_exists=True)
        try:
            await _audit_config_write(
                work=lambda: config_write_mcp.write_project_approvals(
                    runner.claude_json, project_dir, enabled, disabled
                ),
                watch=_config_write_watch(project_dir),
                surface="mcp-approvals",
                scope="project",
                target=str(runner.claude_json),
                action="update",
                actor=_SESSION_USER,
                keys=sorted(set(enabled) | set(disabled)),
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {"project": project, "ok": True}

    @app.post("/api/config-write/mcp/reset-project-choices")
    async def api_config_write_mcp_reset_project_choices(body: dict) -> dict:
        # The one enable/disable-adjacent operation with a real CLI verb (#769) —
        # `claude mcp reset-project-choices` clears both approval lists for the
        # project at `cli_cwd`. Gated + confirmed like the approvals routes above.
        config_write.require_capability(config, "project")
        project = body.get("project")
        config_write.require_confirm("project", project, body.get("confirm"))
        project_dir = _resolve_cw_project(project, require_exists=True)
        try:
            await _audit_config_write(
                work=lambda: config_write_mcp_cli.cli_reset_project_choices(
                    config.claude.binary, project_dir
                ),
                watch=_config_write_watch(project_dir),
                surface="mcp-approvals",
                scope="project",
                target=str(runner.claude_json),
                action="reset",
                actor=_SESSION_USER,
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {"project": project, "ok": True}

    def _user_settings_json() -> Path:
        # User-scope permission rules live in ~/.claude/settings.json (the settings
        # file), NOT ~/.claude.json. Derive it the same way the runner does internally
        # (beside the claude.json whose trusted-dirs we honor) so the two never diverge.
        #
        # The user-scope surface needs a runner to resolve that path. If none is wired
        # (create_app's runner is None — test harnesses / CLI tooling that skip the
        # SessionRunner coercion), fail CLOSED with the same 404-invisible shape
        # require_capability uses for a disabled user scope, rather than letting
        # runner.claude_json raise an AttributeError that escapes as an unhandled 500.
        active_runner = app.state.runner
        if active_runner is None:
            raise HTTPException(status_code=404, detail="config-write user scope is unavailable")
        return active_runner.claude_json.parent / ".claude" / "settings.json"

    @app.get("/api/config-write/permissions")
    async def api_config_write_permissions_read(scope: str = "project", project: str = "") -> dict:
        # Read the permission-rules block for a surface. Gated exactly like the MCP/status
        # routes: 404 when config-write is off, and 404 for user scope when allow_user_scope
        # is off — the surface is invisible, never 403. A corrupt/non-object on-disk
        # settings.json raises InvalidCandidateError from _load_json_obj; map it through the
        # same helper as the PUT route so a hand-edited file is a clean 422, never a 500.
        # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s
        # for ANY request (a bogus scope included), never a differing 422 (#819/#768).
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            try:
                permissions, file_hash = await asyncio.to_thread(
                    config_write_permissions.read_user_permissions, _user_settings_json()
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {"scope": "user", "permissions": permissions, "hash": file_hash}
        if scope == "local":
            project_dir = _resolve_cw_project(project)
            try:
                permissions, file_hash = await asyncio.to_thread(
                    config_write_permissions.read_project_local_permissions, project_dir
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {
                "scope": "local",
                "project": project,
                "permissions": permissions,
                "hash": file_hash,
            }
        project_dir = _resolve_cw_project(project)
        try:
            permissions, file_hash = await asyncio.to_thread(
                config_write_permissions.read_project_permissions, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {
            "scope": "project",
            "project": project,
            "permissions": permissions,
            "hash": file_hash,
        }

    @app.put("/api/config-write/permissions")
    async def api_config_write_permissions_write(body: dict) -> dict:
        # bypassPermissions can never be set here: the validator rejects it as a
        # defaultMode (422), keeping it behind the footgun gate.
        return await _put_config_write(
            body,
            "permissions",
            surface="permissions",
            write_user_fn=config_write_permissions.write_user_permissions,
            write_project_fn=config_write_permissions.write_project_permissions,
            write_local_fn=config_write_permissions.write_project_local_permissions,
            get_user_path=_user_settings_json,
        )

    @app.get("/api/config-write/hooks")
    async def api_config_write_hooks_read(scope: str = "project", project: str = "") -> dict:
        # Read the hooks block for a surface. Gated exactly like the permissions/MCP/status
        # routes: 404 when config-write is off, and 404 for user scope when allow_user_scope
        # is off — the surface is invisible, never 403. A corrupt/non-object on-disk
        # settings.json raises InvalidCandidateError from _load_json_obj; map it through the
        # same helper as the PUT route so a hand-edited file is a clean 422, never a 500.
        # READ never runs a command — it only reflects the stored (inert) hook structure.
        # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s
        # for ANY request (a bogus scope included), never a differing 422 (#819/#768).
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            try:
                hooks, file_hash = await asyncio.to_thread(
                    config_write_hooks.read_user_hooks, _user_settings_json()
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {"scope": "user", "hooks": hooks, "hash": file_hash}
        if scope == "local":
            project_dir = _resolve_cw_project(project)
            try:
                hooks, file_hash = await asyncio.to_thread(
                    config_write_hooks.read_project_local_hooks, project_dir
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {"scope": "local", "project": project, "hooks": hooks, "hash": file_hash}
        project_dir = _resolve_cw_project(project)
        try:
            hooks, file_hash = await asyncio.to_thread(
                config_write_hooks.read_project_hooks, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {"scope": "project", "project": project, "hooks": hooks, "hash": file_hash}

    @app.put("/api/config-write/hooks")
    async def api_config_write_hooks_write(body: dict) -> dict:
        # SECURITY: hooks are shell commands claude runs on lifecycle events. The
        # structural validator NEVER resolves, spawns, or shell-parses a command
        # string; it is stored as inert data and only runs inside a real claude
        # process. The off-by-default gate + validate-never-execute invariant are
        # what prevent a browser write from reaching host RCE.
        return await _put_config_write(
            body,
            "hooks",
            surface="hooks",
            write_user_fn=config_write_hooks.write_user_hooks,
            write_project_fn=config_write_hooks.write_project_hooks,
            write_local_fn=config_write_hooks.write_project_local_hooks,
            get_user_path=_user_settings_json,
        )

    @app.get("/api/config-write/claude-md")
    async def api_config_write_claude_md_read(scope: str = "project", project: str = "") -> dict:
        # Read CLAUDE.md for a surface. Gated exactly like the permissions/hooks/MCP
        # routes: 404 when config-write is off, and 404 for user scope when
        # allow_user_scope is off — the surface is invisible, never 403. Content-tier:
        # the returned text is RAW, never redacted (#768 threat-model decision) — this
        # is the one config-write read route that deliberately skips secret masking.
        #
        # The capability gate runs FIRST, BEFORE the scope-enum check: a disabled
        # surface must 404 for ANY request (including a bogus scope), or a differing
        # 422 would leak that the endpoint exists. A bogus scope never matches "user",
        # so require_capability only trips the base `enabled` flag; when enabled, the
        # enum check below then rejects it as a 422 (the surface is reachable, so the
        # shape error is safe to reveal).
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            try:
                content, file_hash, exists = await asyncio.to_thread(
                    claude_md.read_user_claude_md, runner.claude_json
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {"scope": "user", "content": content, "hash": file_hash, "exists": exists}
        if scope == "local":
            project_dir = _resolve_cw_project(project)
            try:
                content, file_hash, exists = await asyncio.to_thread(
                    claude_md.read_project_local_claude_md, project_dir
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {
                "scope": "local",
                "project": project,
                "content": content,
                "hash": file_hash,
                "exists": exists,
            }
        project_dir = _resolve_cw_project(project)
        try:
            content, file_hash, exists = await asyncio.to_thread(
                claude_md.read_project_claude_md, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {
            "scope": "project",
            "project": project,
            "content": content,
            "hash": file_hash,
            "exists": exists,
        }

    @app.put("/api/config-write/claude-md")
    async def api_config_write_claude_md_write(body: dict) -> dict:
        # CLAUDE.md is prompt-injection CONTENT, not executable config (#768 threat
        # model): the Foundation gate + type-the-name confirm still apply, but there
        # is no structural shape to validate beyond "a string under the size cap" and
        # no redaction on write (nothing here is ever assembled from a secret sentinel).
        # The payload is a single `content` string, not a named JSON subtree, so this
        # route can't reuse `_put_config_write` (which assumes a dict payload) — the
        # gate order is identical though: capability -> confirm -> shape -> path
        # resolve/contain -> stale-hash guard (inside the writer) -> atomic write.
        #
        # Capability gate FIRST, before the scope-enum check, so a disabled surface
        # 404s for ANY request (a bogus scope included) instead of leaking existence
        # via a differing 422 — same invisible-surface invariant as the GET route.
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        project = body.get("project") if scope != "user" else None
        config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=422, detail="body must include a 'content' string")
        expected: str | None = body.get("hash")
        if expected is not None and not isinstance(expected, str):
            raise HTTPException(status_code=422, detail="'hash' must be a string when present")
        if scope == "user":
            try:
                await asyncio.to_thread(
                    claude_md.write_user_claude_md, runner.claude_json, content, expected
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface="claude-md",
                scope="user",
                target=str(runner.claude_json.parent / ".claude" / claude_md.FILENAME),
                action="update",
                actor=_SESSION_USER,
            )
            return {"scope": "user", "ok": True}
        project_dir = _resolve_cw_project(project, require_exists=True)
        write_fn = (
            claude_md.write_project_local_claude_md
            if scope == "local"
            else claude_md.write_project_claude_md
        )
        try:
            await asyncio.to_thread(write_fn, project_dir, content, expected)
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="claude-md",
            scope=scope,
            target=str(project_dir),
            action="update",
            actor=_SESSION_USER,
        )
        return {"scope": scope, "project": project, "ok": True}

    @app.get("/api/config-write/subagents")
    async def api_config_write_subagents_list(scope: str = "project", project: str = "") -> dict:
        # Subagents have exactly two scopes (user/project) — unlike the JSON-subtree
        # surfaces and CLAUDE.md, there is no genuine local-scope directory Claude
        # Code itself reads (see the config_write_subagents module docstring).
        # Capability gate FIRST, before the scope-enum check, so a disabled surface
        # 404s for ANY request (a bogus scope included) — the #819/#768 ordering.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user"):
            raise HTTPException(status_code=422, detail="scope must be 'project' or 'user'")
        if scope == "user":
            agents = await asyncio.to_thread(
                config_write_subagents.list_user_agents, runner.claude_json
            )
            return {"scope": "user", "agents": agents}
        project_dir = _resolve_cw_project(project)
        agents = await asyncio.to_thread(config_write_subagents.list_project_agents, project_dir)
        return {"scope": "project", "project": project, "agents": agents}

    @app.get("/api/config-write/subagents/{name}")
    async def api_config_write_subagent_get(
        name: str, scope: str = "project", project: str = ""
    ) -> dict:
        # Read one subagent's detail doc. A built-in name returns a synthetic,
        # non-editable, 200-shaped doc (it really exists in Claude Code, just not as
        # a file) — never a 404. A missing real file raises AgentNotFoundError,
        # mapped to 404 below. `content` is raw/unredacted (the write round trip);
        # `frontmatter` is a derived, structurally redacted display field.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user"):
            raise HTTPException(status_code=422, detail="scope must be 'project' or 'user'")
        if scope == "user":
            try:
                doc = await asyncio.to_thread(
                    config_write_subagents.read_user_agent, runner.claude_json, name
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {"scope": "user", **doc}
        project_dir = _resolve_cw_project(project)
        try:
            doc = await asyncio.to_thread(
                config_write_subagents.read_project_agent, project_dir, name
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {"scope": "project", "project": project, **doc}

    @app.put("/api/config-write/subagents/{name}")
    async def api_config_write_subagent_put(name: str, body: dict) -> dict:
        # SECURITY: a subagent's frontmatter can carry `hooks`/`mcpServers`/`tools` —
        # each validated the same fail-closed, validate-never-execute way as the
        # dedicated surfaces (hooks reuses config_write_hooks.validate_hooks wholesale,
        # including its plugin-marker rejection). A name colliding with a Claude Code
        # built-in, or an existing on-disk file already detected as plugin-owned, is
        # refused (403) before the candidate content is even validated.
        #
        # Gate order (the #819/#768 fix): capability -> scope-enum 422 -> confirm 400
        # -> payload shape 422 -> path-contain/read-only guard (403, inside the
        # writer) -> stale-hash guard (409, inside the writer) -> atomic write.
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)
        if scope not in ("project", "user"):
            raise HTTPException(status_code=422, detail="scope must be 'project' or 'user'")
        project = body.get("project") if scope != "user" else None
        config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=422, detail="body must include a 'content' string")
        expected: str | None = body.get("hash")
        if expected is not None and not isinstance(expected, str):
            raise HTTPException(status_code=422, detail="'hash' must be a string when present")
        if scope == "user":
            try:
                await asyncio.to_thread(
                    config_write_subagents.write_user_agent,
                    runner.claude_json,
                    name,
                    content,
                    expected,
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface="subagents",
                scope="user",
                target=name,
                action="update",
                actor=_SESSION_USER,
            )
            return {"scope": "user", "name": name, "ok": True}
        project_dir = _resolve_cw_project(project, require_exists=True)
        try:
            await asyncio.to_thread(
                config_write_subagents.write_project_agent, project_dir, name, content, expected
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="subagents",
            scope="project",
            target=name,
            action="update",
            actor=_SESSION_USER,
        )
        return {"scope": "project", "project": project, "name": name, "ok": True}

    @app.delete("/api/config-write/subagents/{name}")
    async def api_config_write_subagent_delete(
        name: str, scope: str = "project", project: str = "", confirm: str = ""
    ) -> dict:
        # Same fail-closed gate order as the PUT route (capability -> scope-enum ->
        # confirm -> read-only/path guards inside the deleter). A built-in or
        # plugin-owned name is refused (403); a genuinely absent ordinary name
        # deletes as a no-op (`deleted: false`), never an error.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user"):
            raise HTTPException(status_code=422, detail="scope must be 'project' or 'user'")
        proj = project if scope != "user" else None
        config_write.require_confirm(scope, proj, confirm)  # type: ignore[arg-type]
        if scope == "user":
            try:
                existed = await asyncio.to_thread(
                    config_write_subagents.delete_user_agent, runner.claude_json, name
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface="subagents",
                scope="user",
                target=name,
                action="delete",
                actor=_SESSION_USER,
                extra={"removed": existed},
            )
            return {"scope": "user", "name": name, "deleted": existed}
        project_dir = _resolve_cw_project(proj, require_exists=True)
        try:
            existed = await asyncio.to_thread(
                config_write_subagents.delete_project_agent, project_dir, name
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="subagents",
            scope="project",
            target=name,
            action="delete",
            actor=_SESSION_USER,
            extra={"removed": existed},
        )
        return {"scope": "project", "project": proj, "name": name, "deleted": existed}

    def _user_claude_json_guarded() -> Path:
        # Same fail-closed guard as _user_settings_json(): the user-scope skills
        # directory (~/.claude/skills/) needs a runner to resolve ~/.claude.json;
        # without one, fail closed with the 404-invisible shape rather than let a
        # None runner raise an unhandled 500.
        active_runner = app.state.runner
        if active_runner is None:
            raise HTTPException(status_code=404, detail="config-write user scope is unavailable")
        return active_runner.claude_json

    @app.get("/api/config-write/skills")
    async def api_config_write_skills_list(scope: str = "project", project: str = "") -> dict:
        # Skill DIRECTORY ops are User/Project scope ONLY -- Claude Code has no
        # "local" skills directory (config_write_skills' module docstring). Capability
        # gate FIRST, before the scope-enum check (#819 ordering fix): a disabled
        # surface must 404 for ANY request, a bogus scope included.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project' or 'user' for skills"
            )
        # NOTE: config_write_skills.list_{user,project}_skills() never raises
        # ConfigWriteError -- a skill whose SKILL.md fails structural validation is
        # still listed with a "frontmatter_error" field (see its docstring), so
        # there is no error-mapping try/except needed here (unlike the file-read
        # and write routes below, which DO propagate typed failures).
        if scope == "user":
            skills = await asyncio.to_thread(
                config_write_skills.list_user_skills, _user_claude_json_guarded()
            )
            return {"scope": "user", "skills": skills}
        project_dir = _resolve_cw_project(project)
        skills = await asyncio.to_thread(config_write_skills.list_project_skills, project_dir)
        return {"scope": "project", "project": project, "skills": skills}

    @app.get("/api/config-write/skills/file")
    async def api_config_write_skills_file_read(
        scope: str = "project",
        project: str = "",
        name: str = "",
        relative: str = config_write_skills.SKILL_FILENAME,
    ) -> dict:
        # View a single file inside a skill directory (SKILL.md by default). Content
        # is REDACTED (config_write_skills' module docstring -- the #813 INFO-1 gap
        # this surface deliberately closes, unlike the CLAUDE.md content-tier route).
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project' or 'user' for skills"
            )
        if not name:
            raise HTTPException(status_code=422, detail="'name' is required")
        if scope == "user":
            try:
                content, file_hash, exists = await asyncio.to_thread(
                    config_write_skills.read_user_skill_file,
                    _user_claude_json_guarded(),
                    name,
                    relative,
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {
                "scope": "user",
                "name": name,
                "relative": relative,
                "content": content,
                "hash": file_hash,
                "exists": exists,
            }
        project_dir = _resolve_cw_project(project)
        try:
            content, file_hash, exists = await asyncio.to_thread(
                config_write_skills.read_project_skill_file, project_dir, name, relative
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {
            "scope": "project",
            "project": project,
            "name": name,
            "relative": relative,
            "content": content,
            "hash": file_hash,
            "exists": exists,
        }

    @app.put("/api/config-write/skills")
    async def api_config_write_skills_write(body: dict) -> dict:
        # SECURITY: a skill's supporting files (scripts/*) are uploaded, OPAQUE
        # content -- never parsed/resolved/executed here, only shape-checked
        # (config_write_skills.validate_script_body). Any file besides SKILL.md
        # requires the caller to echo config_write_skills.SCRIPT_CONFIRM_TOKEN back
        # in "confirm_scripts" -- a SECOND, distinct confirm on top of the ordinary
        # type-the-name gate, required only when script bodies are actually present.
        #
        # Capability gate FIRST, before the scope-enum check (#819 ordering fix).
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)
        if scope not in ("project", "user"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project' or 'user' for skills"
            )
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=422, detail="body must include a 'name' string")
        project = body.get("project") if scope != "user" else None
        config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
        files = body.get("files")
        if not isinstance(files, dict):
            raise HTTPException(status_code=422, detail="body must include a 'files' object")
        expected: str | None = body.get("hash")
        if expected is not None and not isinstance(expected, str):
            raise HTTPException(status_code=422, detail="'hash' must be a string when present")
        confirm_scripts = body.get("confirm_scripts")
        if scope == "user":
            try:
                await asyncio.to_thread(
                    config_write_skills.write_user_skill,
                    _user_claude_json_guarded(),
                    name,
                    files,
                    expected_hash=expected,
                    confirm_scripts=confirm_scripts,
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface="skills",
                scope="user",
                target=name,
                action="update",
                actor=_SESSION_USER,
                keys=sorted(files),
            )
            return {"scope": "user", "name": name, "ok": True}
        project_dir = _resolve_cw_project(project, require_exists=True)
        try:
            await asyncio.to_thread(
                config_write_skills.write_project_skill,
                project_dir,
                name,
                files,
                expected_hash=expected,
                confirm_scripts=confirm_scripts,
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="skills",
            scope="project",
            target=name,
            action="update",
            actor=_SESSION_USER,
            keys=sorted(files),
        )
        return {"scope": "project", "project": project, "name": name, "ok": True}

    @app.post("/api/config-write/skills/delete")
    async def api_config_write_skills_delete(body: dict) -> dict:
        # Deletion needs the same type-the-name confirm as a write: irreversible, no
        # undo store. Capability gate FIRST, before the scope-enum check.
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)
        if scope not in ("project", "user"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project' or 'user' for skills"
            )
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=422, detail="body must include a 'name' string")
        project = body.get("project") if scope != "user" else None
        config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
        if scope == "user":
            try:
                existed = await asyncio.to_thread(
                    config_write_skills.delete_user_skill, _user_claude_json_guarded(), name
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface="skills",
                scope="user",
                target=name,
                action="delete",
                actor=_SESSION_USER,
                extra={"removed": existed},
            )
            return {"scope": "user", "name": name, "existed": existed}
        project_dir = _resolve_cw_project(project, require_exists=True)
        try:
            existed = await asyncio.to_thread(
                config_write_skills.delete_project_skill, project_dir, name
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="skills",
            scope="project",
            target=name,
            action="delete",
            actor=_SESSION_USER,
            extra={"removed": existed},
        )
        return {"scope": "project", "project": project, "name": name, "existed": existed}

    @app.get("/api/config-write/skills/overrides")
    async def api_config_write_skills_overrides_read(
        scope: str = "project", project: str = ""
    ) -> dict:
        # skillOverrides is an ordinary settings.json key, so -- unlike the directory
        # ops above -- it gets all three scopes (user/project/local), exactly like
        # config_write_hooks' `hooks` key. Capability gate FIRST (#819 ordering fix).
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            try:
                overrides, file_hash = await asyncio.to_thread(
                    config_write_skills.read_user_skill_overrides, _user_settings_json()
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {"scope": "user", "overrides": overrides, "hash": file_hash}
        if scope == "local":
            project_dir = _resolve_cw_project(project)
            try:
                overrides, file_hash = await asyncio.to_thread(
                    config_write_skills.read_project_local_skill_overrides, project_dir
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {
                "scope": "local",
                "project": project,
                "overrides": overrides,
                "hash": file_hash,
            }
        project_dir = _resolve_cw_project(project)
        try:
            overrides, file_hash = await asyncio.to_thread(
                config_write_skills.read_project_skill_overrides, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {"scope": "project", "project": project, "overrides": overrides, "hash": file_hash}

    @app.put("/api/config-write/skills/overrides")
    async def api_config_write_skills_overrides_write(body: dict) -> dict:
        # skillOverrides is inert visibility state (on/name-only/user-invocable-only/
        # off) -- never executed, unlike the skill directory writer above. Gate order
        # mirrors the CLAUDE.md/settings routes (capability -> scope-enum 422 ->
        # confirm 400 -> payload shape 422 -> path resolve/contain -> stale-hash guard
        # (inside the writer) -> atomic write) -- the #819 fix, not the older
        # _put_config_write helper's order (scope-enum before capability).
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        project = body.get("project") if scope != "user" else None
        config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
        payload = body.get("overrides")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="body must include an 'overrides' object")
        expected: str | None = body.get("hash")
        if expected is not None and not isinstance(expected, str):
            raise HTTPException(status_code=422, detail="'hash' must be a string when present")
        if scope == "user":
            try:
                await asyncio.to_thread(
                    config_write_skills.write_user_skill_overrides,
                    _user_settings_json(),
                    payload,
                    expected,
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface="skill-overrides",
                scope="user",
                target=str(_user_settings_json()),
                action="update",
                actor=_SESSION_USER,
                keys=sorted(payload),
            )
            return {"scope": "user", "ok": True}
        if scope == "local":
            project_dir = _resolve_cw_project(project, require_exists=True)
            try:
                await asyncio.to_thread(
                    config_write_skills.write_project_local_skill_overrides,
                    project_dir,
                    payload,
                    expected,
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface="skill-overrides",
                scope="local",
                target=str(project_dir),
                action="update",
                actor=_SESSION_USER,
                keys=sorted(payload),
            )
            return {"scope": "local", "project": project, "ok": True}
        project_dir = _resolve_cw_project(project, require_exists=True)
        try:
            await asyncio.to_thread(
                config_write_skills.write_project_skill_overrides, project_dir, payload, expected
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="skill-overrides",
            scope="project",
            target=str(project_dir),
            action="update",
            actor=_SESSION_USER,
            keys=sorted(payload),
        )
        return {"scope": "project", "project": project, "ok": True}

    @app.get("/api/config-write/settings")
    async def api_config_write_settings_read(scope: str = "project", project: str = "") -> dict:
        # Generic settings.json editor (#772): env/model/misc keys not owned by a
        # dedicated surface (permissions/hooks/plugin+MCP-enable stay on their own
        # routes). Gated exactly like the other config-write reads: 404 when
        # config-write is off, 404 for user scope when allow_user_scope is off.
        #
        # Capability gate FIRST, before the scope-enum check (the #819/#768
        # ordering fix): a disabled surface must 404 for ANY request, a bogus
        # scope included, rather than leak existence via a differing 422.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            try:
                settings_view, file_hash = await asyncio.to_thread(
                    config_write_settings.read_user_settings, _user_settings_json()
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {"scope": "user", "settings": settings_view, "hash": file_hash}
        if scope == "local":
            project_dir = _resolve_cw_project(project)
            try:
                settings_view, file_hash = await asyncio.to_thread(
                    config_write_settings.read_project_local_settings, project_dir
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            return {
                "scope": "local",
                "project": project,
                "settings": settings_view,
                "hash": file_hash,
            }
        project_dir = _resolve_cw_project(project)
        try:
            settings_view, file_hash = await asyncio.to_thread(
                config_write_settings.read_project_settings, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {
            "scope": "project",
            "project": project,
            "settings": settings_view,
            "hash": file_hash,
        }

    @app.put("/api/config-write/settings")
    async def api_config_write_settings_write(body: dict) -> dict:
        # SECURITY: `env` is where operators keep secrets (#822 lesson) -- the
        # read path masks every env value unconditionally; a write that resends
        # the mask sentinel keeps the stored value (config_write.merge_redacted),
        # so this route never assembles a live secret from a client echo. See
        # config_write_settings' module docstring for the full redaction decision.
        #
        # Gate order mirrors the CLAUDE.md route (capability -> scope-enum 422 ->
        # confirm 400 -> payload shape 422 -> path resolve/contain -> stale-hash
        # guard (inside the writer) -> atomic write) -- the #819/#768 fix, not the
        # older `_put_config_write` helper's order (scope-enum before capability).
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        project = body.get("project") if scope != "user" else None
        config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]
        payload = body.get("settings")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="body must include a 'settings' object")
        expected: str | None = body.get("hash")
        if expected is not None and not isinstance(expected, str):
            raise HTTPException(status_code=422, detail="'hash' must be a string when present")
        if scope == "user":
            try:
                await asyncio.to_thread(
                    config_write_settings.write_user_settings,
                    _user_settings_json(),
                    payload,
                    expected,
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
            await config_audit.arecord(
                config.state_dir,
                surface="settings",
                scope="user",
                target=str(_user_settings_json()),
                action="update",
                actor=_SESSION_USER,
                keys=sorted(payload),
            )
            return {"scope": "user", "ok": True}
        project_dir = _resolve_cw_project(project, require_exists=True)
        write_fn = (
            config_write_settings.write_project_local_settings
            if scope == "local"
            else config_write_settings.write_project_settings
        )
        try:
            await asyncio.to_thread(write_fn, project_dir, payload, expected)
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        await config_audit.arecord(
            config.state_dir,
            surface="settings",
            scope=scope,
            target=str(project_dir),
            action="update",
            actor=_SESSION_USER,
            keys=sorted(payload),
        )
        return {"scope": scope, "project": project, "ok": True}

    @app.get("/api/config-write/settings/effective")
    async def api_config_write_settings_effective(project: str = "") -> dict:
        # Scope-merge provenance (#772, the novel part): per-key effective value
        # + which scope layer supplied it, across every scope clauster manages.
        # Gated on "project" scope -- project/local are inherently per-project,
        # so a project is always required for this view. The user layer is
        # folded into the merge only when allow_user_scope is ALSO on; when it's
        # off, ~/.claude/settings.json is never read for this route either --
        # the user-scope surface stays invisible for every read, this one
        # included, not just the dedicated GET/PUT above.
        config_write.require_capability(config, "project")
        project_dir = _resolve_cw_project(project)
        try:
            project_misc, _p_hash = await asyncio.to_thread(
                config_write_settings.read_project_settings, project_dir
            )
            local_misc, _l_hash = await asyncio.to_thread(
                config_write_settings.read_project_local_settings, project_dir
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        user_misc: dict[str, Any] | None = None
        if config.config_write.allow_user_scope:
            try:
                user_misc, _u_hash = await asyncio.to_thread(
                    config_write_settings.read_user_settings, _user_settings_json()
                )
            except config_write.ConfigWriteError as exc:
                raise _map_config_write_error(exc) from exc
        effective = config_write_settings._compute_effective_settings(
            user_misc=user_misc, project_misc=project_misc, local_misc=local_misc
        )
        return {"project": project, "effective": effective}

    def _plugin_cli_cwd(scope: str, project: str) -> Path:
        # Resolve the directory `claude plugin ...` should be spawned from for a
        # given scope. User scope has no project — an arbitrary safe directory the
        # CLI ignores (same choice config_write_mcp_cli makes for MCP user-scope
        # calls). Project/local scope MUST exist on disk (require_exists=True):
        # several verbs' output genuinely depends on this cwd (plugin `list`'s
        # per-entry `enabled` field, marketplace declarations visible from it) --
        # see config_write_plugins' module docstring's live-verified findings.
        if scope == "user":
            active_runner = app.state.runner
            if active_runner is None:
                raise HTTPException(
                    status_code=404, detail="config-write user scope is unavailable"
                )
            return active_runner.claude_json.parent
        return _resolve_cw_project(project, require_exists=True)

    @app.get("/api/config-write/plugins")
    async def api_config_write_plugins_list(scope: str = "project", project: str = "") -> dict:
        # Installed plugins (#771) -- CLI-only (`claude plugin list --json`): cache
        # path / install timestamp / cwd-dependent `enabled` state have no
        # settings.json equivalent to read directly. Capability gate FIRST (#819).
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        cwd = _plugin_cli_cwd(scope, project)
        try:
            plugins = await asyncio.to_thread(
                config_write_plugins.cli_list_plugins, config.claude.binary, cwd
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        # Omit `project` for user scope (where it is a meaningless "") to match the
        # sibling routes (/plugins/enabled, /marketplaces/declared, the action POSTs).
        result: dict[str, Any] = {"scope": scope, "plugins": plugins}
        if scope != "user":
            result["project"] = project
        return result

    @app.get("/api/config-write/plugins/enabled")
    async def api_config_write_plugins_enabled(scope: str = "project", project: str = "") -> dict:
        # Direct (non-spawning) read of the enable/disable map -- mirrors the MCP
        # surface's "file read for display" doctrine; no secret ever lives here.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            enabled = await asyncio.to_thread(
                config_write_plugins.read_user_enabled_plugins, _user_settings_json()
            )
            return {"scope": "user", "enabled": enabled}
        project_dir = _resolve_cw_project(project)
        read_fn = (
            config_write_plugins.read_project_local_enabled_plugins
            if scope == "local"
            else config_write_plugins.read_project_enabled_plugins
        )
        enabled = await asyncio.to_thread(read_fn, project_dir)
        return {"scope": scope, "project": project, "enabled": enabled}

    @app.get("/api/config-write/plugins/{plugin_id}")
    async def api_config_write_plugin_details(
        plugin_id: str, scope: str = "project", project: str = ""
    ) -> dict:
        # `claude plugin details <id>` -- CLI-only (component inventory + token
        # cost projection, not stored in settings.json). Capability gate FIRST.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        try:
            config_write.validate_candidate(plugin_id, config_write_plugins.validate_plugin_id)
        except config_write.InvalidCandidateError as exc:
            raise _map_config_write_error(exc) from exc
        cwd = _plugin_cli_cwd(scope, project)
        try:
            details = await asyncio.to_thread(
                config_write_plugins.cli_plugin_details, config.claude.binary, cwd, plugin_id
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        return {"scope": scope, "project": project, "plugin": plugin_id, "details": details}

    @app.post("/api/config-write/plugins/action")
    async def api_config_write_plugins_action(body: dict) -> dict:
        # Plugin enable/disable/install/uninstall/update (#771), the highest
        # blast-radius config-write child: `install` pulls new executable code
        # onto the host, so it carries a SECOND, stronger confirm on top of the
        # ordinary scope confirm -- see config_write_plugins.require_install_confirm.
        # Gate order (the #819/#768 fix, extended with the install-specific
        # confirm): capability -> scope-enum 422 -> base scope confirm 400 ->
        # op/plugin-id shape 422 -> [install only] plugin-id confirm 400 ->
        # path resolve/contain (400/404) -> the CLI dispatch itself (404
        # not-found, or 400 for any other CLI failure).
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        project = body.get("project") if scope != "user" else None
        config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]

        op = body.get("op")
        if op not in ("enable", "disable", "install", "uninstall", "update"):
            raise HTTPException(
                status_code=422,
                detail="op must be 'enable', 'disable', 'install', 'uninstall', or 'update'",
            )
        plugin_id = body.get("plugin")
        if not isinstance(plugin_id, str) or not plugin_id:
            raise HTTPException(
                status_code=422, detail="body must include a non-empty 'plugin' string"
            )
        try:
            config_write.validate_candidate(plugin_id, config_write_plugins.validate_plugin_id)
        except config_write.InvalidCandidateError as exc:
            raise _map_config_write_error(exc) from exc

        if op == "install":
            # The STRONG per-install confirm: the operator retypes the exact
            # plugin id being introduced, not just the project/scope name.
            config_write_plugins.require_install_confirm(plugin_id, body.get("confirm_plugin"))

        keep_data = body.get("keep_data", False)
        if not isinstance(keep_data, bool):
            raise HTTPException(status_code=422, detail="'keep_data' must be a boolean")
        prune = body.get("prune", False)
        if not isinstance(prune, bool):
            raise HTTPException(status_code=422, detail="'prune' must be a boolean")

        cwd = _plugin_cli_cwd(scope, project or "")
        binary = config.claude.binary

        def _work() -> None:
            if op == "enable":
                config_write_plugins.cli_enable_plugin(binary, cwd, plugin_id, scope)  # type: ignore[arg-type]
            elif op == "disable":
                config_write_plugins.cli_disable_plugin(binary, cwd, plugin_id, scope)  # type: ignore[arg-type]
            elif op == "install":
                config_write_plugins.cli_install_plugin(binary, cwd, plugin_id, scope)  # type: ignore[arg-type]
            elif op == "uninstall":
                config_write_plugins.cli_uninstall_plugin(
                    binary,
                    cwd,
                    plugin_id,
                    scope,  # type: ignore[arg-type]
                    keep_data=keep_data,
                    prune=prune,
                )
            else:
                config_write_plugins.cli_update_plugin(binary, cwd, plugin_id, scope)  # type: ignore[arg-type]

        # Audit right after the mutation commits, BEFORE the gitignore housekeeping — a
        # failure of that step must not drop the committed change from the trail (#958 P6).
        # Records the changed files + the redacted `claude plugin` argv it ran.
        try:
            await _audit_config_write(
                work=_work,
                watch=_config_write_watch(cwd),
                surface="plugins",
                scope=scope,  # type: ignore[arg-type]
                target=plugin_id,
                action=op,  # type: ignore[arg-type]
                actor=_SESSION_USER,
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        if scope == "local":
            # The CLI writes settings.local.json directly (never through clauster's
            # own writer), so clauster must gitignore it itself here -- the
            # gitignore-on-create hard requirement (#766) still applies even when
            # the file is CLI-written rather than clauster-written.
            await asyncio.to_thread(
                config_write.ensure_gitignored,
                cwd,
                ".claude/settings.local.json",
                ignore_backup_sibling=True,
            )
        result = {"scope": scope, "plugin": plugin_id, "op": op, "ok": True}
        if scope != "user":
            result["project"] = project
        return result

    @app.get("/api/config-write/marketplaces")
    async def api_config_write_marketplaces_list(
        scope: str = "project", project: str = ""
    ) -> dict:
        # `claude plugin marketplace list --json` (#771) -- a single merged pool,
        # confirmed cwd-independent live, but still gated/routed through the
        # ordinary scope plumbing like every other route here.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        cwd = _plugin_cli_cwd(scope, project)
        try:
            marketplaces = await asyncio.to_thread(
                config_write_plugins.cli_list_marketplaces, config.claude.binary, cwd
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        # Omit `project` for user scope (a meaningless "") to match the sibling routes.
        result: dict[str, Any] = {"scope": scope, "marketplaces": marketplaces}
        if scope != "user":
            result["project"] = project
        return result

    @app.get("/api/config-write/marketplaces/declared")
    async def api_config_write_marketplaces_declared(
        scope: str = "project", project: str = ""
    ) -> dict:
        # Direct (non-spawning) read of the PER-SCOPE `extraKnownMarketplaces`
        # declaration -- the one thing the CLI's merged list view cannot tell you
        # (which scope declared a given marketplace), needed to know where a
        # remove/add would land.
        config_write.require_capability(config, scope)  # type: ignore[arg-type]
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        if scope == "user":
            declared = await asyncio.to_thread(
                config_write_plugins.read_user_marketplaces, _user_settings_json()
            )
            return {"scope": "user", "marketplaces": declared}
        project_dir = _resolve_cw_project(project)
        read_fn = (
            config_write_plugins.read_project_local_marketplaces
            if scope == "local"
            else config_write_plugins.read_project_marketplaces
        )
        declared = await asyncio.to_thread(read_fn, project_dir)
        return {"scope": scope, "project": project, "marketplaces": declared}

    @app.post("/api/config-write/marketplaces/action")
    async def api_config_write_marketplaces_action(body: dict) -> dict:
        # Marketplace add/remove/update (#771). `add`/`remove` are scoped
        # (--scope always explicit, never omitted -- omitting it on `remove`
        # would let the CLI reach into every scope, see config_write_plugins'
        # module docstring); `update` takes no --scope but is still routed
        # through the same scope/project/confirm plumbing for a stable cwd.
        scope = body.get("scope", "project")
        config_write.require_capability(config, scope)
        if scope not in ("project", "user", "local"):
            raise HTTPException(
                status_code=422, detail="scope must be 'project', 'user', or 'local'"
            )
        project = body.get("project") if scope != "user" else None
        config_write.require_confirm(scope, project, body.get("confirm"))  # type: ignore[arg-type]

        op = body.get("op")
        if op not in ("add", "remove", "update"):
            raise HTTPException(status_code=422, detail="op must be 'add', 'remove', or 'update'")

        name_raw = body.get("name")
        source_raw = body.get("source")
        name: str | None = None
        source: str | None = None
        if op == "add":
            if not isinstance(source_raw, str) or not source_raw:
                raise HTTPException(
                    status_code=422, detail="body must include a non-empty 'source' string"
                )
            source = source_raw
            try:
                config_write.validate_candidate(
                    source, config_write_plugins.validate_marketplace_source
                )
            except config_write.InvalidCandidateError as exc:
                raise _map_config_write_error(exc) from exc
        elif op == "remove":
            if not isinstance(name_raw, str) or not name_raw:
                raise HTTPException(
                    status_code=422, detail="body must include a non-empty 'name' string"
                )
            name = name_raw
            try:
                config_write.validate_candidate(
                    name, config_write_plugins.validate_marketplace_name
                )
            except config_write.InvalidCandidateError as exc:
                raise _map_config_write_error(exc) from exc
        elif name_raw is not None:
            if not isinstance(name_raw, str) or not name_raw:
                raise HTTPException(
                    status_code=422, detail="'name' must be a non-empty string when present"
                )
            name = name_raw
            try:
                config_write.validate_candidate(
                    name, config_write_plugins.validate_marketplace_name
                )
            except config_write.InvalidCandidateError as exc:
                raise _map_config_write_error(exc) from exc

        cwd = _plugin_cli_cwd(scope, project or "")
        binary = config.claude.binary

        def _work() -> None:
            if op == "add":
                config_write_plugins.cli_marketplace_add(binary, cwd, source, scope)  # type: ignore[arg-type]
            elif op == "remove":
                config_write_plugins.cli_marketplace_remove(binary, cwd, name, scope)  # type: ignore[arg-type]
            else:
                config_write_plugins.cli_marketplace_update(binary, cwd, name)

        # Audit right after the mutation commits, BEFORE the gitignore housekeeping — a
        # failure of that step must not drop the committed change from the trail (#958 P6).
        # Records the changed files + the redacted `claude plugin marketplace` argv it ran.
        try:
            await _audit_config_write(
                work=_work,
                watch=_config_write_watch(cwd),
                surface="marketplaces",
                scope=scope,  # type: ignore[arg-type]
                target=(name or source or ""),
                action=op,  # type: ignore[arg-type]
                actor=_SESSION_USER,
            )
        except config_write.ConfigWriteError as exc:
            raise _map_config_write_error(exc) from exc
        if scope == "local" and op in ("add", "remove"):
            # Only add/remove actually touch the scope's settings file (`update`
            # merely refreshes a git checkout, writing no settings key) -- see the
            # plugins/action route's identical comment for why this is needed at
            # all: the CLI writes settings.local.json directly, bypassing
            # clauster's own gitignore-on-create writer path.
            await asyncio.to_thread(
                config_write.ensure_gitignored,
                cwd,
                ".claude/settings.local.json",
                ignore_backup_sibling=True,
            )
        result: dict[str, Any] = {"scope": scope, "op": op, "ok": True}
        if name is not None:
            result["name"] = name
        if source is not None:
            result["source"] = source
        if scope != "user":
            result["project"] = project
        return result

    async def _project_by_name(name: str) -> Project:
        for proj in await list_projects():
            if proj.name == name:
                return proj
        raise HTTPException(status_code=500, detail=f"project {name!r} missing after provisioning")

    @app.post("/api/projects", status_code=201)
    async def api_create_project(body: dict) -> Project:
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=422, detail="body must include a 'name' string")
        git_init = bool(body.get("git_init", False))
        try:
            await asyncio.to_thread(create_project, config.projects_root, name, git_init=git_init)
        except InvalidProjectName as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TargetExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except GitUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProvisionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The new directory may not bump projects_root's mtime at the cache's
        # resolution; drop the cache so the project is visible to _project_by_name now.
        invalidate_discovery_cache()
        return await _project_by_name(name)

    async def _run_clone(job: CloneJob, name: str, url: str, shallow: bool) -> None:
        """Clone in a worker thread, streaming progress into the job's queue."""
        loop = asyncio.get_running_loop()

        def _forward(line: str) -> None:
            loop.call_soon_threadsafe(clone_jobs.push_progress, job, line)

        def _register_proc(proc: subprocess.Popen[bytes]) -> None:
            # Hand the worker's git process to the job so a cancel request can terminate
            # it. Hop onto the loop: the job registry is mutated event-loop-only (#573).
            loop.call_soon_threadsafe(job.register_terminate, proc.terminate)

        try:
            target = await asyncio.to_thread(
                clone_project,
                config.projects_root,
                name,
                url,
                cfg=config.clone,
                shallow=shallow,
                progress_cb=_forward,
                on_proc=_register_proc,
            )
        except ProvisionError as exc:
            # A cancel terminates git → non-zero exit → CloneFailed here; report it as a
            # clean `cancelled`, not an error (the worker already tore down the temp dir).
            if job.cancel_requested:
                clone_jobs.cancel(job)
            else:
                clone_jobs.finish(job, error=str(exc))
        except Exception as exc:  # defensive: never leave a job stuck "running"
            if job.cancel_requested:
                clone_jobs.cancel(job)
            else:
                clone_jobs.finish(job, error=f"unexpected error: {exc}")
        else:
            if job.cancel_requested:
                # A cancel arrived but `terminate()` was a no-op — git had already
                # finished and the dir landed. Honor the 202 "cancelling" contract: tear
                # down the just-created project and broadcast `cancelled`, not `done`.
                await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
                invalidate_discovery_cache()
                clone_jobs.cancel(job)
            else:
                # The cloned directory may not bump projects_root's mtime at the cache's
                # resolution; drop the cache so the new project's row renders immediately.
                invalidate_discovery_cache()
                clone_jobs.finish(job)
        # Fire the #432 `clone-done` webhook off the runner's emitter (fire-and-forget,
        # fail-open, default OFF). The error_detail is redacted before egress — a clone
        # failure can echo a remote URL/host into its message. The clone url itself is
        # never sent: it can carry credentials.
        runner.emit_event(
            "clone-done",
            {
                "event_type": "clone-done",
                "project": name,
                "status": job.status,
                "error": redact_for_disk(job.error_detail) if job.error_detail else None,
            },
        )
        loop.call_later(_CLONE_JOB_TTL, clone_jobs.discard, job.id)

    @app.post("/api/projects/clone", status_code=202)
    async def api_clone_project(body: dict) -> dict:
        """Start an async clone; returns a job id to watch via ``/ws/clone-progress``."""
        if not config.clone.enabled:
            raise HTTPException(status_code=403, detail="clone is disabled in config")
        name = body.get("name")
        url = body.get("url")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=422, detail="body must include a 'name' string")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(status_code=422, detail="A Git URL is required to clone.")
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail=f"invalid project name: {name!r}")
        if (config.projects_root / name).exists():
            raise HTTPException(
                status_code=409, detail=f"a directory named {name!r} already exists"
            )
        shallow = bool(body.get("shallow", False))
        # Validate the URL up front (scheme + SSRF host resolve) so an obviously
        # bad clone fails the request itself; the DNS resolve runs off the loop.
        try:
            await asyncio.to_thread(validate_clone_url, url, config.clone)
        except InvalidCloneUrl as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except BlockedCloneHost as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        job = clone_jobs.create(name)
        task = asyncio.create_task(_run_clone(job, name, url, shallow))
        _clone_tasks.add(task)
        task.add_done_callback(_clone_tasks.discard)
        return {"job_id": job.id, "name": name}

    @app.post("/api/projects/clone/{job_id}/cancel", status_code=202)
    async def api_clone_cancel(job_id: str) -> dict:
        """Cancel an in-progress clone: terminate the git worker + clean its partial dir.

        404 for an unknown/expired job, 409 once it's already terminal (done/error/
        cancelled). The worker reports the ``cancelled`` outcome over the progress WS;
        the partial temp dir is torn down on the terminated git's non-zero exit (#573).
        """
        job = clone_jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown or expired clone job")
        if not job.request_cancel():
            raise HTTPException(status_code=409, detail=f"clone job is already {job.status}")
        return {"job_id": job.id, "cancelling": True}

    @app.get("/api/projects/clone/active")
    async def api_clone_active() -> dict:
        """List in-flight clone jobs so a second tab can reattach to live progress (#659).

        Clone progress streams over the per-job WebSocket only to tabs that started (or
        already reattached to) it. A tab opened mid-clone polls this on load: a running
        job here lets it reattach to ``/ws/clone-progress/{job_id}`` and show the same
        live bar. The clone URL is never returned (it can carry credentials) — only the
        job id, project name, and current ``{phase, percent}``.
        """
        return {"jobs": [job.status_snapshot() for job in clone_jobs.active_jobs()]}

    @app.get("/api/instances")
    async def api_instances() -> list[RemoteControlInstance]:
        """Every managed bridge, one row per instance (registration order).

        A project may contribute several rows (#778): at most one standard
        (server-mode) bridge plus any number of interactive (pty) sessions.
        Group client-side by ``project`` and key rows by ``instance_id`` —
        ``project`` is not unique.
        """
        return runner.list_instances()

    @app.get("/api/hosted")
    async def api_hosted() -> list[RemoteControlInstance]:
        """Hosted (claustrum stream-json) sessions, each status-synced to its live session.

        Kept separate from ``/api/instances`` (project-keyed bridges): hosted
        sessions live in their own ``HostedManager`` registry, are keyed by a
        client-chosen id, and there may be several per project. The dashboard's
        hosted panel polls this; empty list when the channel is unused. Auth-gated
        by the guard middleware like every other ``/api/*`` route.
        """
        instances = app.state.hosted.list_instances()
        # Debounced (no-op when unchanged): refresh the persisted reattach cursors on
        # the dashboard's poll cadence, so a restart replays from a recent daemon seq.
        await app.state.hosted.persist()
        return instances

    @app.get("/api/widget")
    async def api_widget() -> dict:
        """Compact dashboard-widget summary (e.g. Homepage/Homarr custom API widgets).

        Returns a small, stable, flat JSON shape sourced entirely from live runner
        state plus the project list — the same data the dashboard already renders, so
        nothing here is computed or invented beyond what's observable.

        Shape::

            {
                "projects_total": int,                # discovered projects under projects_root
                "bridges": {<InstanceStatus>: int},   # every status key present, 0 when none
                "running_total": int,                 # == bridges["running"]
                "version": str,                       # clauster package version
            }

        Read-only and auth-gated by the guard middleware like every other ``/api/*``
        route. NB: a Homepage-style scraper hitting an auth-enabled deploy still needs
        to supply auth / network reachability itself — that's not solved here (same
        follow-up as the metrics endpoints).
        """
        instances = runner.list_instances()
        # Enumerate the enum so every status key is always present (0 when none),
        # giving the widget a stable schema regardless of the current bridge mix.
        by_status = {status.value: 0 for status in InstanceStatus}
        for inst in instances:
            by_status[inst.status.value] += 1
        projects = await list_projects()
        return {
            "projects_total": len(projects),
            "bridges": by_status,
            "running_total": by_status[InstanceStatus.RUNNING.value],
            "version": __version__,
        }

    @app.get("/api/sessions")
    async def api_sessions() -> dict[str, list[WorkingSession]]:
        """External (unmanaged) working sessions grouped by project name (bug #4)."""
        return runner.external_sessions_by_project()

    @app.get("/api/sessions/tracked")
    async def api_sessions_tracked() -> dict[str, list[WorkingSession]]:
        """Live working sessions owned by each managed bridge, keyed by instance (#570).

        A standard ``claude remote-control`` bridge is multi-session; this exposes
        every live session under it (not just the starter) so the dashboard can list
        them. Driven by the same ``agents --json`` reconcile join as ``/api/sessions``
        — no new poll. pty (single-session) bridges simply map to their one session.
        """
        return runner.tracked_sessions_by_instance()

    @app.get("/api/sessions/adoptable")
    async def api_sessions_adoptable() -> list[str]:
        """Project names whose live external session is a standard bridge safe to adopt (#330).

        The dashboard gates its per-project Adopt affordance on this list — a pty
        (flag-form) external bridge is excluded (unsafe to adopt; see runner.adopt).
        Off-loaded to a thread (filesystem + ``psutil``). Auth-gated by the guard
        middleware like every other ``/api/*`` route.
        """
        return sorted(await asyncio.to_thread(runner.adoptable_external_projects))

    @app.get("/api/config")
    async def api_config_get() -> dict:
        """Tier-A editable config values + a content hash, for the in-app editor (FE-3).

        Only allowlisted operational fields are returned — no auth/secret/bind/
        structural value is ever surfaced (structural redaction). Auth-gated like
        every ``/api/*`` route by the guard middleware.
        """
        cfg = app.state.config
        path = cfg.source_path
        # Serve the values from the on-disk file (consistent with the content hash
        # below), not the startup config: a prior save writes the file but does not
        # live-reload the runtime, so app.state.config goes stale after any save —
        # reopening the editor would then show pre-save values and a successful save
        # looks reverted. Fall back to the in-memory config if the file can't be
        # re-read (missing / externally corrupted) so the editor still opens.
        fields = None
        content_hash = None
        present = None
        if path is not None:
            fields = config_editor.editable_values_on_disk(path)
            # One read of the on-disk file yields both the external-edit hash and the set of
            # Tier-A keys literally present — the latter lets the editor drop a deprecated row
            # once the user removed its key (e.g. via `config reconcile`). Both are None when
            # the file can't be read (deleted / unreadable after startup): the editor still
            # opens on the in-memory `fields` fallback below, a save is safely rejected for the
            # missing hash, and nothing is hidden (fail-open on display — never hide a field we
            # can't prove is absent).
            content_hash, present = config_editor.disk_state(path)
        if fields is None:
            fields = config_editor.editable_values(cfg)
        specs = config_editor.field_specs(present, config=cfg)
        # The front-end builds its rendered rows from `editable`, so a hidden deprecated
        # field is removed by dropping it here — editing `specs` alone would not hide the row.
        editable = [p for p in config_editor.EDITABLE_FIELDS if not specs[p]["hidden"]]
        return {
            "fields": fields,
            "editable": editable,
            "specs": specs,
            "hash": content_hash,
        }

    @app.put("/api/config")
    async def api_config_put(body: dict) -> dict:
        """Apply allowlisted config edits: re-validate + backup + atomic write.

        Tier-A only — a non-allowlisted key is a 400, never a silent drop. Requires
        the ``hash`` from GET (external-edit guard → 409 on mismatch). Writes to disk
        but does **not** live-reload; the response flags that a restart is needed.
        """
        cfg = app.state.config
        path = cfg.source_path
        if path is None:
            raise HTTPException(status_code=409, detail="config has no on-disk source to edit")
        edits = body.get("edits")
        if not isinstance(edits, dict) or not edits:
            raise HTTPException(
                status_code=422, detail="body must include a non-empty 'edits' map"
            )
        expected = body.get("hash")
        if not isinstance(expected, str) or not expected:
            raise HTTPException(
                status_code=422, detail="body must include the 'hash' from GET /api/config"
            )
        try:
            new_hash = await asyncio.to_thread(
                config_writer.write_edits, path, edits, expected_hash=expected
            )
        except config_editor.DisallowedFieldError as exc:
            raise HTTPException(status_code=400, detail=f"not editable: {exc}") from exc
        except config_editor.StaleConfigError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except config_editor.ConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=f"invalid config: {exc}") from exc
        return {"hash": new_hash, "restart_required": True}

    def _require_advanced(request: Request) -> None:
        """Fail-closed gate for the Tier-B "Advanced" config surface (#978).

        404 when config-write is disabled (invisible surface, like the reaper), then
        403 ``reauth_required`` until the caller has re-proved the password via
        ``POST /api/reauth``. Ordered so a disabled surface never advertises itself
        with a 403.
        """
        if not config.config_write.enabled:
            raise HTTPException(status_code=404, detail="config-write is disabled")
        require_elevated(request)

    @app.get("/api/config/advanced")
    async def api_config_advanced_get(request: Request) -> dict:
        """Tier-B config values + a content hash, behind config-write + step-up (#978).

        Mirrors ``GET /api/config`` but over :data:`~clauster.config_editor.TIER_B_FIELDS`
        only — the operational-but-sensitive ``clone.*`` / ``webhooks.*`` scalars. No
        Tier-C secret/bind/auth value is ever surfaced (structural redaction, same as
        Tier-A). Values are read from disk so they stay consistent with the hash and
        reflect what the next restart will load.
        """
        _require_advanced(request)
        cfg = app.state.config
        path = cfg.source_path
        fields = None
        content_hash = None
        present = None
        if path is not None:
            fields = config_editor.editable_values_on_disk(
                path, fields=config_editor.TIER_B_FIELDS
            )
            content_hash, present = config_editor.disk_state(
                path, fields=config_editor.TIER_B_FIELDS
            )
        if fields is None:
            fields = config_editor.editable_values(cfg, fields=config_editor.TIER_B_FIELDS)
        specs = config_editor.field_specs(present, fields=config_editor.TIER_B_FIELDS, config=cfg)
        editable = [p for p in config_editor.TIER_B_FIELDS if not specs[p]["hidden"]]
        return {
            "fields": fields,
            "editable": editable,
            "specs": specs,
            "hash": content_hash,
        }

    @app.put("/api/config/advanced")
    async def api_config_advanced_put(request: Request, body: dict) -> dict:
        """Apply Tier-B config edits: capability + step-up gated, backup + atomic write (#978).

        Fail-closed order: capability (404 when off) → step-up (403 ``reauth_required``)
        → external-edit hash guard (409) → re-validate against TIER_B **only** (a Tier-A
        or Tier-C key is a 400, never a silent drop) → backup + atomic write. Does not
        live-reload; the response flags a restart is needed. Records an audit line with
        the touched key NAMES only (never values).
        """
        _require_advanced(request)
        cfg = app.state.config
        path = cfg.source_path
        if path is None:
            raise HTTPException(status_code=409, detail="config has no on-disk source to edit")
        edits = body.get("edits")
        if not isinstance(edits, dict) or not edits:
            raise HTTPException(
                status_code=422, detail="body must include a non-empty 'edits' map"
            )
        expected = body.get("hash")
        if not isinstance(expected, str) or not expected:
            raise HTTPException(
                status_code=422,
                detail="body must include the 'hash' from GET /api/config/advanced",
            )
        try:
            new_hash = await asyncio.to_thread(
                config_writer.write_edits,
                path,
                edits,
                expected_hash=expected,
                allowed=frozenset(config_editor.TIER_B_FIELDS),
            )
        except config_editor.DisallowedFieldError as exc:
            raise HTTPException(status_code=400, detail=f"not editable: {exc}") from exc
        except config_editor.StaleConfigError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except config_editor.ConfigValidationError as exc:
            raise HTTPException(status_code=422, detail=f"invalid config: {exc}") from exc
        await config_audit.arecord(
            cfg.state_dir,
            surface="config-advanced",
            scope="global",
            target=str(path),
            action="edit",
            actor=_SESSION_USER,
            keys=sorted(edits),
        )
        return {"hash": new_hash, "restart_required": True}

    @app.post("/api/restart", status_code=202)
    async def api_restart() -> dict:
        """Restart Clauster in place to apply a saved config change (#483).

        Re-exec mechanism (``os.execv``): uniform across systemd/launchd/terminal/
        Docker, needs no unit change, keeps the same PID, and reloads config (read at
        startup). Because the swap keeps the same PID and never stops the unit, child
        processes are untouched: ``runner.shutdown()`` leaves bridges running and
        ``hosted.aclose()`` detaches (not stops) hosted sessions, so clauster-managed
        sessions (standard + pty bridges + browser/hosted) survive the swap and are
        reattached on startup (#663). Only in-flight HTTP/WS connections drop during the
        re-bind window; the UI polls ``/healthz`` and reloads once the new image binds.

        Fail-closed: auth-gated by the guard middleware like every ``/api/*`` route, and
        a 503 (not a half-restarted process) if no live server is wired to shut down.
        Returns 202 **before** the swap so the client gets a response; the server then
        shuts down gracefully (releasing the socket) and ``_run`` re-execs.
        """
        server = getattr(app.state, "uvicorn_server", None)
        if server is None:
            raise HTTPException(
                status_code=503, detail="no live server to restart (not running under uvicorn)"
            )
        # Flag the restart, then ask uvicorn to shut down gracefully. The flag is read
        # in ``_run`` AFTER serve() returns: a clean shutdown that releases the socket,
        # then the re-exec. `should_exit` only takes effect once this handler's response
        # has flushed (uvicorn checks it in its main loop), so the 202 reaches the client.
        app.state.restart_requested = True
        server.should_exit = True
        return {"restarting": True}

    @app.get("/api/agents")
    async def api_agents() -> list[BackgroundJob]:
        """Agent-view background sessions (`claude --bg`), observed read-only.

        Sourced from the supervisor's docs-acknowledged on-disk state
        (``jobs/<id>/state.json`` + ``daemon/roster.json``) — no subprocess, no
        daemon protocol. Empty list when agent view is unused. Auth-gated by the
        guard middleware like every other ``/api/*`` route.
        """
        return await asyncio.to_thread(supervisor.list_background_jobs)

    @app.post("/api/agents", status_code=201)
    async def api_dispatch_agent(body: dict) -> dict:
        """Dispatch a `claude --bg` background session in a managed project.

        Validates the project name (same guard as the other project routes),
        pre-trusts the cwd, and fires `claude --bg [--rc <name>]`; returns the new
        job id. The bg-agents panel reflects its live state via `GET /api/agents`.

        Body: ``{project, prompt?, rc_name?, model?, permission_mode?}``. A
        ``rc_name`` opens the cloud door (a cloud-visible Remote Control session).
        NOTE: stop/teardown is a later slice — a dispatched `--rc` session must
        currently be stopped from the CLI (and a local stop only orphans the cloud
        registration), so no dispatch control is wired into the UI yet.
        """
        raw_name = body.get("project")
        if not isinstance(raw_name, str):
            raise HTTPException(status_code=422, detail="project must be a string")
        name = raw_name.strip()
        if not is_valid_project_name(name):
            raise HTTPException(status_code=422, detail="invalid project name")

        def _opt_text(field: str, *, empty_ok: bool = True) -> str | None:
            """Validate an optional text field from the arbitrary JSON body.

            Absent/null → None. A present non-string → 422 (rather than letting a
            bad type reach — and partially execute — the spawn path). A present
            empty string → None when ``empty_ok`` (a missing prompt), else 422:
            an explicit empty ``rc_name``/``model``/``permission_mode`` is a
            caller mistake, not "use the default", so it fails loudly instead of
            silently dispatching a different session than intended.
            """
            value = body.get(field)
            if value is None:
                return None
            if not isinstance(value, str):
                raise HTTPException(status_code=422, detail=f"{field} must be a string")
            if value == "":
                if empty_ok:
                    return None
                raise HTTPException(status_code=422, detail=f"{field} must not be empty")
            return value

        prompt = _opt_text("prompt")
        if prompt is not None and not prompt.replace("\ufeff", "").strip():
            # Whitespace-only == no prompt (#1033): the dashboard trims before its
            # gate, so the API must normalize identically or a direct request
            # dispatches a nominally-promptless session around the 422 below.
            # JS trim() also strips U+FEFF (ECMA WhiteSpace) while Python's
            # strip() does not — drop it from the emptiness test for parity.
            prompt = None
        rc_name = _opt_text("rc_name", empty_ok=False)
        model = _opt_text("model", empty_ok=False)
        permission_mode = _opt_text("permission_mode", empty_ok=False)
        cwd = config.projects_root / name
        if not await asyncio.to_thread(cwd.is_dir):
            raise HTTPException(status_code=404, detail=f"project {name!r} not found")
        # Resolve the effective mode (request override, else the configured default) and gate
        # it AFTER the existence check (so a missing project still 404s, not a 403 leaked by
        # the ceiling) but BEFORE dispatch — mirrors the hosted/bridge channels so the bypass
        # ceiling can't be sidestepped by omitting permission_mode when the default is
        # bypassPermissions. (Also makes bg honor instance_defaults, like the other channels.)
        pm = permission_mode or config.instance_defaults.permission_mode
        _enforce_bypass_ceiling(name, pm)
        if prompt is None and rc_name is None:
            # #1033: an un-registered background session has no composer and no
            # cloud surface — dispatched without a prompt it parks at "send a
            # prompt to start" forever, with no way to ever receive one (the bg
            # card offers only Stop/Forget). ``rc_name`` opens the claude.ai door,
            # where the session is conversational, so a blank prompt is legitimate
            # there. Gated after the 404/403 checks so existence and the bypass
            # ceiling keep their precedence (same ordering rationale as
            # _enforce_bypass_ceiling above).
            raise HTTPException(
                status_code=422,
                detail=(
                    "prompt is required for a background session unless rc_name "
                    "registers it on claude.ai"
                ),
            )
        try:
            job_id = await asyncio.to_thread(
                supervisor.dispatch_background_job,
                cwd,
                prompt=prompt,
                rc_name=rc_name,
                model=model,
                permission_mode=pm,
                binary=config.claude.binary,
                claude_json=runner.claude_json,
            )
        except claude_cli.ClaudeNotFound as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except supervisor.DispatchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"id": job_id}

    @app.delete("/api/agents/{job_id}")
    async def api_stop_agent(job_id: str) -> dict:
        """Stop a `claude --bg` background session and remove its job, cleanly.

        Double-SIGINTs the session process so the CLI runs an orderly shutdown and
        deregisters its cloud bridge session (vs `claude stop`, which SIGKILLs and
        leaves a cloud orphan), then `claude rm`s the job. The job id is validated
        to the 8-hex short-id shape (also the `claude rm` argv-injection guard).

        Returns `{id, settled, removed, detail}`. `settled` is True only for a
        confirmed cloud-deregistering stop; `settled:false` (no live worker found)
        is a 200 whose `detail` flags the unconfirmed stop / possible cloud orphan.
        A session that was signalled but didn't settle in time raises StopError → 409
        (escalate from the CLI — we don't force-kill, which would orphan the cloud
        session). When `claude rm` soft-fails for a confirmed-dead worker, clauster
        drops the orphaned job record itself so the row can still be forgotten (#485);
        any residual `removed:false` is reported in the body.
        """
        if not supervisor.valid_job_id(job_id):
            raise HTTPException(status_code=422, detail="invalid job id")
        try:
            result = await asyncio.to_thread(
                supervisor.stop_background_job, job_id, binary=config.claude.binary
            )
        except claude_cli.ClaudeNotFound as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except supervisor.StopError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # Fire the #432 `bg-settled` webhook (fire-and-forget, fail-open, default OFF):
        # a background `claude --bg` job reached a terminal state via the supervisor.
        # The job id is this deployment's own short handle (not a foreign secret, same
        # posture as /api/agents); `detail` can carry a `claude rm` path/stderr tail so
        # it is redacted before egress.
        # Bind detail once: pyright narrows the local to str inside the truthiness
        # guard, which a repeated result.get("detail") would not (Unknown | None).
        detail = result.get("detail")
        runner.emit_event(
            "bg-settled",
            {
                "event_type": "bg-settled",
                "id": result.get("id"),
                "settled": result.get("settled"),
                "removed": result.get("removed"),
                "detail": redact_for_disk(detail) if detail else None,
            },
        )
        return result

    @app.post("/api/agents/{job_id}/resume", status_code=201)
    async def api_resume_agent(job_id: str) -> dict:
        """Resume an ended `claude --bg` session into a new bg job inheriting its transcript.

        The path `{job_id}` is the 8-hex short id (validated like the stop path); the
        `--resume` argument is the job's full session UUID, validated separately
        (`valid_session_id`) — the 8-hex guard would reject a UUID. Resume mints a NEW
        job id (and session UUID) that inherits the prior transcript, so `{id}` returned
        here differs from the path id; the panel surfaces it as a new row.
        """
        if not supervisor.valid_job_id(job_id):
            raise HTTPException(status_code=422, detail="invalid job id")
        try:
            new_id = await asyncio.to_thread(
                supervisor.resume_background_job,
                job_id,
                binary=config.claude.binary,
                claude_json=runner.claude_json,
            )
        except claude_cli.ClaudeNotFound as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except supervisor.ResumeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except supervisor.DispatchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"id": new_id}

    def _enforce_bypass_ceiling(project: str, permission_mode: str | None) -> None:
        """Reject ``bypassPermissions`` when a project's config ceiling forbids it.

        The runner enforces this hard ceiling for the bridge channel
        (:class:`PermissionModeNotAllowed`, mapped to 403 below). The hosted and
        background-agent channels spawn outside the runner, so they must mirror the
        gate here or a crafted request could run a session in bypass mode that the
        project's ``allow_bypass_permissions`` ceiling explicitly forbids. Both
        paths share the single :meth:`ClausterConfig.bypass_denied` decision so they
        can't diverge; this channel keeps its own exception type. Fail closed with
        the same 403 + wording the bridge path uses.
        """
        if config.bypass_denied(project, permission_mode):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"bypassPermissions is not enabled for project {project!r}. Set "
                    "projects.<name>.allow_bypass_permissions: true in clauster.yml first."
                ),
            )

    async def _spawn_or_http(coro: Awaitable[_SpawnT]) -> _SpawnT:
        """Await a spawn/resume coroutine, mapping its exceptions to HTTP codes.

        Shared by the create and resume routes so the mapping lives in one place.
        Generic over the coroutine's result; both routes now await a
        :class:`~clauster.runner.SpawnOutcome` (#778, #1145).
        """
        try:
            return await coro
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidSpawnOption as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PermissionModeNotAllowed as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except SpawnError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _spawn_body(
        instance: RemoteControlInstance,
        *,
        created: bool,
        reason: str | None = None,
        warnings: list[str] | None = None,
    ) -> dict:
        """Serialize a spawn response: the instance plus additive outcome keys (#778).

        The instance's own fields stay at the top level (existing clients read
        ``status``/``session_url`` etc. straight off the body); ``created`` /
        ``reason`` / ``warnings`` are additive so pre-#778 clients ignore them.
        """
        return {
            **instance.model_dump(mode="json"),
            "created": created,
            "reason": reason,
            "warnings": warnings or [],
        }

    @app.post("/api/instances", status_code=201)
    async def api_spawn(body: dict, response: Response) -> dict:
        """Start a bridge/session; 201 when launched, 200 when an existing one was reused.

        The body is the instance plus outcome keys (#778): ``created`` is False —
        with ``reason`` — when the standard-singleton cap returned the already-live
        standard bridge instead of launching a second one; ``warnings`` carries
        non-blocking advisories (an interactive pty session launched without a
        worktree risks conflicting concurrent edits).
        """
        project = body.get("project")
        if not isinstance(project, str) or not project:
            raise HTTPException(status_code=422, detail="body must include a 'project' string")
        spawn_mode = body.get("spawn_mode")
        permission_mode = body.get("permission_mode")
        resume_mode = body.get("resume_mode")
        # Optional custom bridge/session display name (#780) — --name for a standard
        # bridge in place of the project name. Blank/omitted keeps today's default;
        # runner.spawn_detailed validates it (length/control chars) before any spawn
        # side effect, surfaced here as a 422 via _spawn_or_http's InvalidSpawnOption
        # mapping.
        name = body.get("name")
        # Optional per-launch sandbox toggle (#780) — tri-state "default"/"on"/"off" for a
        # standard bridge. DISABLED for 1.0 (#1037): still accepted + enum-validated by
        # runner.spawn_detailed (422 on a bad value), but inert — the runner emits no
        # --sandbox flag and coerces persisted values to "default" until #1046 re-enables it.
        sandbox = body.get("sandbox")
        # Optional past-conversation fork for a pty launch (#303) — the transcripts
        # API's session uuid, spawned as `--resume <uuid> --fork-session`. Format,
        # pty-only, and revive-exclusivity rules are enforced by runner.spawn_detailed
        # BEFORE any spawn side effect (InvalidSpawnOption → 422 via _spawn_or_http);
        # here we only type-gate like the sibling optional fields.
        resume_session_id = body.get("resume_session_id")
        channel = body.get("channel", "remote-control")
        for field, value in (
            ("spawn_mode", spawn_mode),
            ("permission_mode", permission_mode),
            ("resume_mode", resume_mode),
            ("name", name),
            ("sandbox", sandbox),
            ("resume_session_id", resume_session_id),
            ("channel", channel),
        ):
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=422, detail=f"{field} must be a string")
        if channel == "hosted":
            return _spawn_body(await _spawn_hosted(project, permission_mode), created=True)
        if channel != "remote-control":
            raise HTTPException(status_code=422, detail=f"unknown channel: {channel!r}")
        outcome = await _spawn_or_http(
            runner.spawn_detailed(
                project,
                spawn_mode=spawn_mode,
                permission_mode=permission_mode,
                resume_mode=resume_mode,
                custom_name=name,
                sandbox=sandbox,
                resume_session_id=resume_session_id,
            )
        )
        if not outcome.created:
            # Nothing was launched — the existing live instance came back. 200, not
            # 201: no resource was created, and `reason` says why.
            response.status_code = 200
        return _spawn_body(
            outcome.instance,
            created=outcome.created,
            reason=outcome.reason,
            warnings=outcome.warnings,
        )

    async def _hosted_prereqs(project: str) -> tuple[object, Path, str]:
        """Resolve (daemon client, trusted project path, claude binary) for a hosted op.

        Shared by hosted spawn and resume; raises the same HTTP errors the spawn path
        has always used (503 no daemon / 503 binary missing, 409 untrusted directory).
        """
        daemon = getattr(app.state, "claustrum_daemon", None)
        client = daemon.client if daemon is not None else None
        if client is None:
            raise HTTPException(
                status_code=503,
                detail="hosted channel unavailable: claustrum daemon not connected",
            )
        path = await _resolve_project_path(project)
        if not await asyncio.to_thread(is_trusted, path, runner.claude_json):
            raise HTTPException(
                status_code=409,
                detail=f"directory not trusted: {path}. Use the Trust action first.",
            )
        try:
            binary = await asyncio.to_thread(claude_cli.resolve_binary, config.claude.binary)
        except claude_cli.ClaudeNotFound as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return client, path, binary

    async def _spawn_hosted(project: str, permission_mode: str | None) -> RemoteControlInstance:
        """Start a hosted (claustrum stream-json) session for ``project``."""
        # Confirm the project exists first so a missing name 404s instead of leaking a 403
        # from the ceiling, then gate the effective mode before any daemon/trust/spawn work.
        await _resolve_project_path(project)
        pm = permission_mode or config.instance_defaults.permission_mode
        # Validate the mode before any daemon/spawn work — parity with the bridge channel
        # (runner rejects an unknown mode pre-argv). Not exploitable (list-argv, no
        # injection), but an unknown mode is a client error: 422, not a 502 from a daemon
        # spawn that fails downstream.
        if pm not in PERMISSION_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"invalid permission_mode {pm!r}; expected one of {PERMISSION_MODES}",
            )
        _enforce_bypass_ceiling(project, pm)
        client, path, binary = await _hosted_prereqs(project)
        try:
            return await app.state.hosted.spawn(
                client,
                project=project,
                label=f"hosted:{project}",
                cwd=str(path),
                claude_binary=binary,
                permission_mode=pm,
            )
        except ClaustrumError as exc:
            raise HTTPException(status_code=502, detail=f"hosted spawn failed: {exc}") from exc

    async def _resume_hosted(
        hosted_id: str, instance: RemoteControlInstance
    ) -> RemoteControlInstance:
        """Resume a lost/ended hosted session by id, respawning with ``--resume <uuid>``.

        ``instance`` is the row the route already fetched. Maps the engine's
        :class:`HostedSessionError` (unknown / still-running / no-uuid) to 409 and a
        daemon spawn failure to 502.
        """
        client, path, binary = await _hosted_prereqs(instance.project)
        try:
            return await app.state.hosted.resume(
                client, hosted_id, cwd=str(path), claude_binary=binary
            )
        except HostedSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ClaustrumError as exc:
            raise HTTPException(status_code=502, detail=f"hosted resume failed: {exc}") from exc

    @app.get("/api/instances/{instance_id}")
    async def api_instance(instance_id: str) -> RemoteControlInstance:
        # Accept the project name the current client sends as a bridge identity, or a
        # raw instance_id / hosted id (#777; the #778 API split moves fully to ids).
        resolved = runner.resolve_bridge_id(instance_id)
        instance = (
            runner.get_instance(resolved) if resolved is not None else None
        ) or app.state.hosted.get_instance(instance_id)
        if instance is None:
            raise _unresolved_bridge(runner, instance_id, f"no such instance: {instance_id}")
        return instance

    @app.post("/api/instances/{instance_id}/message", status_code=202)
    async def api_hosted_message(instance_id: str, body: dict) -> dict:
        """Send one user turn to a hosted session (the conversation input path)."""
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise HTTPException(
                status_code=422, detail="body must include a non-empty 'text' string"
            )
        if app.state.hosted.get_instance(instance_id) is None:
            raise HTTPException(status_code=404, detail=f"no such hosted session: {instance_id}")
        try:
            await app.state.hosted.send(instance_id, text)
        except HostedSessionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/instances/{instance_id}/permissions/{request_id}", status_code=202)
    async def api_hosted_permission(instance_id: str, request_id: str, body: dict) -> dict:
        """Answer a parked tool-permission request on a hosted session (CL-5).

        The engine parks every tool-permission ``control_request`` and waits
        (fail-closed) until something answers — this is that explicit human gate.
        ``decision`` is ``"allow"`` or ``"deny"`` (a deny may carry a short
        ``message``), mapped to the SDK ``can_use_tool`` response ``{"behavior": …}``.
        """
        decision = body.get("decision")
        if decision not in ("allow", "deny"):
            raise HTTPException(status_code=422, detail="decision must be 'allow' or 'deny'")
        if app.state.hosted.get_instance(instance_id) is None:
            raise HTTPException(status_code=404, detail=f"no such hosted session: {instance_id}")
        if decision == "allow":
            response: dict = {"behavior": "allow"}
        else:
            message = body.get("message")
            response = {
                "behavior": "deny",
                "message": message
                if isinstance(message, str) and message
                else "Denied by operator",
            }
        try:
            await app.state.hosted.respond(instance_id, request_id, response)
        except HostedSessionError as exc:
            # Already answered, or no such parked request — not in a state to answer.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.delete("/api/instances/{instance_id}")
    async def api_stop(instance_id: str) -> RemoteControlInstance:
        if app.state.hosted.get_instance(instance_id) is not None:
            # A live hosted session stops cleanly; one with no live session (an orphan
            # that survived a daemon restart, or an already-dead row) is killed/cleaned
            # up by id — kill_orphan, not stop, since stop requires a live session.
            try:
                if app.state.hosted.session(instance_id) is not None:
                    return await app.state.hosted.stop(instance_id)
                return await app.state.hosted.kill_orphan(instance_id)
            except HostedSessionError as exc:
                # The row vanished between the existence check and the awaited call
                # (concurrent stop/reattach) — treat as gone, not a 500.
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        resolved = runner.resolve_bridge_id(instance_id)
        if resolved is None:
            raise _unresolved_bridge(runner, instance_id, f"no managed instance: {instance_id!r}")
        try:
            return await runner.stop(resolved)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/instances/{instance_id}/resume")
    async def api_resume(instance_id: str) -> dict:
        """Re-spawn a stopped/crashed bridge or hosted session into its prior conversation.

        Bridges reuse their stored spawn/permission modes; a hosted session respawns
        a fresh daemon process with ``--resume <claude_session_uuid>`` (CL-7).

        The body is the instance plus the same additive outcome keys the create route
        returns (#778): ``created`` is False — with ``reason`` — when nothing was
        revived, because the standard-singleton cap handed back the already-live bridge
        for that project instead, or because an interactive (pty) target was already
        live. A caller that ignores ``created`` reports a resume that never happened as
        success (#1145). The body's ``instance_id`` is usually a *different* bridge, but
        the pty path hands back the target itself — so comparing ids is not a substitute
        for reading ``created``.
        """
        hosted = app.state.hosted.get_instance(instance_id)
        if hosted is not None:
            return _spawn_body(await _resume_hosted(instance_id, hosted), created=True)
        resolved = runner.resolve_bridge_id(instance_id)
        if resolved is None:
            raise _unresolved_bridge(
                runner, instance_id, f"no managed instance to resume: {instance_id!r}"
            )
        try:
            outcome = await _spawn_or_http(runner.resume_detailed(resolved))
            return _spawn_body(
                outcome.instance,
                created=outcome.created,
                reason=outcome.reason,
                warnings=outcome.warnings,
            )
        except HTTPException as exc:
            # Only a genuine spawn failure (SpawnError -> 409) means the bridge tried to
            # come back and could not — that is the case worth a #541 reconnect-failed
            # notification. A 404 (the instance vanished / unknown), 422 (bad spawn option)
            # or 403 (permission-mode) is a precondition error, not a failed reconnect, so
            # it must NOT notify (#652). Fire only for 409, then re-raise unchanged.
            if exc.status_code == 409:
                runner.notify_app_event(
                    "reconnect-failed",
                    "clauster: reconnect failed",
                    f"Resuming the bridge for {instance_id!r} failed.",
                )
            raise

    @app.post("/api/instances/{instance_id}/forget")
    async def api_forget(instance_id: str) -> dict:
        """Drop a stopped/crashed session's record so it leaves the Recent/resumable list.

        Both bridges and hosted sessions persist a record that survives a Stop (so they
        stay Resumable); forget removes it to start fresh. Fail closed: a still-live
        session is refused with 409 (Stop/Kill it first) — forget never terminates a
        process — and an unknown id is 404.
        """
        hosted = app.state.hosted.get_instance(instance_id)
        try:
            if hosted is not None:
                # A known hosted id can only fail here as "still live" -> 409 (unknown
                # hosted ids are None above and fall through to the bridge runner).
                await app.state.hosted.forget(instance_id)
            else:
                # Refuse an ambiguous prefix BEFORE the verbatim fallback below: `or
                # instance_id` would otherwise hand `forget` the raw prefix, and the
                # operator would get a bare 404 for an id that names several real
                # bridges rather than being told which (#1099).
                if runner.bridge_id_candidates(instance_id):
                    raise _unresolved_bridge(
                        runner, instance_id, f"no such instance: {instance_id}"
                    )
                # Accept the project name the current client sends as well as a raw
                # instance_id (#777); fall back to the id verbatim so a purely-persisted
                # (not-yet-materialized) record still reaches runner.forget's own lookup.
                await runner.forget(runner.resolve_bridge_id(instance_id) or instance_id)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InstanceStillLive, HostedSessionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": instance_id, "forgotten": True}

    @app.post("/api/projects/{name}/adopt")
    async def api_adopt(name: str) -> RemoteControlInstance:
        """Take over a live standard external session as a managed instance (#330).

        Fail closed: unknown project -> 404; already managed, or no live standard
        bridge to adopt (it ended, or it's a pty bridge) -> 409. A pty external
        session is never adoptable — it stays display-only.
        """
        try:
            return await runner.adopt(name)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (InstanceStillLive, AdoptionUnavailable) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/projects/{name}/trust")
    async def api_trust(name: str) -> Project:
        try:
            return await runner.trust_project(name)
        except UnknownProject as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OSError as exc:
            # ~/.claude.json exists but couldn't be read/written (e.g. permissions).
            # Surface it instead of silently dropping the operator's other settings.
            raise HTTPException(
                status_code=500, detail=f"could not update trust state: {exc}"
            ) from exc

    async def _resolve_project_path(name: str) -> Path:
        """Map a project name to its path, refusing unknown/unsafe names (traversal)."""
        if not is_valid_project_name(name):
            raise HTTPException(status_code=404, detail=f"project {name!r} not found")
        for proj in await list_projects():
            if proj.name == name:
                return proj.path
        raise HTTPException(status_code=404, detail=f"project {name!r} not found")

    def _bridge_running(name: str) -> bool:
        # Any-RUNNING scan, not the canonical-instance resolver: with N instances per
        # project (#778) the canonical pick can transiently be a STARTING standard
        # bridge while a pty session is already RUNNING — this flag must not miss it.
        if runner.has_running_instance(name):
            return True
        return name in runner.external_sessions_by_project()

    @app.get("/api/projects/{name}/claude-md")
    async def api_claude_md_get(name: str) -> ClaudeMdDoc:
        path = await _resolve_project_path(name)
        try:
            doc = await asyncio.to_thread(read_claude_md, path)
        except ClaudeMdError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        doc.bridge_running = _bridge_running(name)
        return doc

    @app.put("/api/projects/{name}/claude-md")
    async def api_claude_md_put(name: str, body: dict) -> ClaudeMdDoc:
        path = await _resolve_project_path(name)
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=422, detail="body must include a 'content' string")
        base_sha = body.get("base_sha256")
        if base_sha is not None and not isinstance(base_sha, str):
            raise HTTPException(status_code=422, detail="base_sha256 must be a string")
        try:
            doc = await asyncio.to_thread(
                write_claude_md,
                path,
                content,
                base_sha256=base_sha,
                state_dir=config.state_dir,
                user=_SESSION_USER,
                claude_json=runner.claude_json,
            )
        except ClaudeMdTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except ClaudeMdNotTrusted as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ClaudeMdConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ClaudeMdError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        doc.bridge_running = _bridge_running(name)
        return doc

    @app.get("/api/instances/{instance_id}/qr")
    async def api_instance_qr(instance_id: str) -> Response:
        """SVG QR for the primary deep link (feature 5) — scan to open on mobile."""
        resolved = runner.resolve_bridge_id(instance_id)
        instance = runner.get_instance(resolved) if resolved is not None else None
        if instance is None:
            raise _unresolved_bridge(runner, instance_id, f"no such instance: {instance_id}")
        target = instance.session_url or instance.url
        if not target:
            raise HTTPException(status_code=409, detail="no session URL available yet")
        buf = io.BytesIO()
        segno.make(target, error="m").save(buf, kind="svg", scale=4, border=2)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")

    async def _ws_authorized(websocket: WebSocket) -> bool:
        """Strict Origin check + session/proxy/token auth, BEFORE accepting (D12)."""
        user, _via_proxy, via_token = await _authenticate(websocket)
        if user is None:
            return False
        # The Origin allowlist is a cross-site WS-hijack defense for ambient
        # (cookie) credentials — browsers always send Origin. A Bearer-token
        # client (headless/API) carries no ambient credential and sends no
        # Origin, so the check would wrongly reject it; exempt the token path
        # exactly as the HTTP CSRF gate does. Cookie/proxy auth still needs it.
        if via_token:
            return True
        origin = websocket.headers.get("origin")
        return bool(origin) and auth.normalize_origin(origin) in _allowed_origins

    async def _ws_gate(websocket: WebSocket) -> bool:
        """Whether the WS handshake may proceed, BEFORE accept() (D12).

        The Origin allowlist is a cross-site WS-hijack defence, NOT an
        authentication method, so it must run even when ``config.auth.enabled``
        is false (the shipped default). Tying it to the auth master switch let
        ``config.auth.enabled and ...`` short-circuit, so ``_ws_authorized``
        never ran and any page the operator visited could open a socket to the
        loopback service and read-stream live output (CWE-1385). A browser
        WebSocket ALWAYS sends Origin, so an absent Origin means a non-browser
        client (never the cross-site attack) and passes; a present,
        non-allowlisted Origin is rejected. When auth is enabled the full
        session/proxy/token gate applies unchanged (it also rejects an absent
        Origin for ambient cookie credentials, and exempts the Bearer path).
        """
        if config.auth.enabled:
            return await _ws_authorized(websocket)
        origin = websocket.headers.get("origin")
        return origin is None or auth.normalize_origin(origin) in _allowed_origins

    @app.websocket("/ws/bridge-log/{instance_id}")
    async def ws_bridge_log(websocket: WebSocket, instance_id: str) -> None:
        """Tail the bridge debug log — ANSI-stripped and ID-redacted (feature 6, D11)."""
        if not await _ws_gate(websocket):
            await websocket.close(
                code=1008
            )  # validate before accept — never open an unauthed socket
            return
        await websocket.accept()
        # The client tags the tail with the project name (#777); resolve it to the
        # registry's instance_id before lookup so the socket doesn't 1008 for a live bridge.
        resolved = runner.resolve_bridge_id(instance_id)
        instance = runner.get_instance(resolved) if resolved is not None else None
        if instance is None or instance.bridge_debug_log_path is None:
            await websocket.close(code=1008)  # nothing to stream
            return
        # Stream the verbatim raw parse-source (== the debug log unless on-disk
        # redaction split it off), sanitizing each line in-flight as always — so the
        # live stream stays current regardless of the at-rest mirror's refresh cadence.
        path = instance.bridge_raw_log_path or instance.bridge_debug_log_path
        strip = config.logs.strip_ansi_in_stream
        offset = await asyncio.to_thread(logstream.initial_offset, path)

        async def _stream() -> None:
            carry = ""
            local_offset = offset
            while True:
                local_offset, text = await asyncio.to_thread(
                    logstream.read_new, path, local_offset
                )
                if text:
                    # Buffer whole lines so redaction never misses an id split
                    # across two reads.
                    *lines, carry = (carry + text).split("\n")
                    for line in lines:
                        await websocket.send_text(sanitize_line(line, strip_ansi_seq=strip))
                await asyncio.sleep(0.5)

        try:
            await stream_until_disconnect(websocket, _stream)
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.websocket("/ws/hosted/{instance_id}")
    async def ws_hosted(websocket: WebSocket, instance_id: str) -> None:
        """Stream a hosted session's live events, replaying the ring past ``?after=``."""
        if not await _ws_gate(websocket):
            await websocket.close(code=1008)  # validate before accept
            return
        await websocket.accept()
        session = app.state.hosted.session(instance_id)
        if session is None:
            await websocket.close(code=1008)  # unknown / already gone
            return
        try:
            after = int(websocket.query_params.get("after", "0"))
        except (TypeError, ValueError):
            after = 0
        queue = session.subscribe(after_seq=after)

        async def _stream() -> None:
            while True:
                await websocket.send_json(await queue.get())

        try:
            await stream_until_disconnect(websocket, _stream)
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            session.unsubscribe(queue)

    @app.websocket("/ws/clone-progress/{job_id}")
    async def ws_clone_progress(websocket: WebSocket, job_id: str) -> None:
        """Stream a clone job's ``{phase, percent}`` progress, then a terminal frame."""
        if not await _ws_gate(websocket):
            await websocket.close(code=1008)  # validate before accept
            return
        await websocket.accept()
        job = clone_jobs.get(job_id)
        if job is None:
            await websocket.close(code=1008)  # unknown / already pruned
            return
        # Subscribe before the status check so a terminal that fires between the
        # check and the snapshot send below lands in our queue, not the void.
        queue = job.subscribe()

        async def _stream() -> None:
            if job.status != "running":
                # Already finished (e.g. a reconnect after completion).
                await websocket.send_json(job.terminal_event())
                return
            await websocket.send_json(job.progress_event())  # current snapshot
            while True:
                event = await queue.get()
                await websocket.send_json(event)
                if event.get("type") == "done":
                    break

        try:
            await stream_until_disconnect(websocket, _stream)
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            job.unsubscribe(queue)

    @app.websocket("/ws/pty-screen/{instance_id}")
    async def ws_pty_screen(websocket: WebSocket, instance_id: str) -> None:
        """Stream a pty bridge's redacted, cells-only live-screen frames (read-only, #534).

        The keeper publishes redacted frames to a screen sidecar (off by default); this polls
        that file and forwards each new frame — de-duped by its monotonic ``seq`` — as JSON.
        The wire carries only pyte-rendered cells + cursor + state, never raw ANSI, so the
        at-rest redaction invariant holds end to end.
        """
        if not await _ws_gate(websocket):
            await websocket.close(code=1008)  # validate before accept
            return
        await websocket.accept()
        # The client tags the view with the project name (#777); resolve to instance_id.
        resolved = runner.resolve_bridge_id(instance_id)
        instance = runner.get_instance(resolved) if resolved is not None else None
        # The live screen exists only for a pty bridge with the (default-off) tap enabled;
        # anything else has no sidecar to stream, so refuse rather than hang silently.
        if (
            instance is None
            or instance.resume_mode != "pty"
            or instance.bridge_debug_log_path is None
            or not config.claude.pty_screen_enabled
        ):
            await websocket.close(code=1008)
            return
        sidecar = pty_screen.screen_sidecar_path(instance.bridge_debug_log_path)

        async def _stream() -> None:
            last_seq = -1
            while True:
                frame = await asyncio.to_thread(pty_screen.read_screen_sidecar, sidecar)
                if frame is not None:
                    seq = frame.get("seq")
                    if isinstance(seq, int) and seq > last_seq:
                        last_seq = seq
                        await websocket.send_json(frame)
                await asyncio.sleep(_SCREEN_POLL_INTERVAL)

        try:
            await stream_until_disconnect(websocket, _stream)
        except (WebSocketDisconnect, RuntimeError):
            return

    async def _dashboard_context() -> dict:
        """Build the shared template context for the dashboard."""
        projects = await list_projects()
        return {
            "projects": projects,
            "version": __version__,
            "projects_root": str(config.projects_root),
            "auth_enabled": config.auth.enabled,
            # Whether a PASSWORD is configured — the real prerequisite for the Advanced
            # step-up (#978). auth.enabled can be true with no password (reverse-proxy /
            # API-token-only auth), where /api/reauth can never accept a password; gate the
            # unlock form on this, not on auth_enabled.
            "auth_password_set": config.auth.password_hash is not None,
            "reaper_ui_enabled": config.reaper.ui_enabled,
            "default_spawn_mode": config.instance_defaults.spawn_mode,
            "default_permission_mode": config.instance_defaults.permission_mode,
            "default_resume_mode": config.claude.launch_mode,
            # Canonical permission-mode label map (#685): one server-injected source
            # of truth ({mode: {short, long, effect}}) drives the launch <select>, the
            # JS permLabel()/permissionEffect() helpers, and the config editor.
            "permission_labels": PERMISSION_LABELS,
            "bypass_desktop_hint": BYPASS_DESKTOP_HINT,
            # Recognized hook lifecycle events (#958 Part 5): the single server-injected
            # source of truth for the config editor's Hooks rows <select>, sorted for a
            # stable order and derived from the backend validator so the two never drift.
            "hook_events": sorted(config_write_hooks.RECOGNIZED_EVENTS),
            # Interactive Session (true-resume pty) works on POSIX always and on Windows
            # via the ConPTY keeper when the `pty` extra (pywinpty) is installed (#914).
            "pty_supported": _pty_supported(),
            # Usage badge: mode ("cost"|"tokens"|"off"), the currency code + its
            # resolved symbol, the static USD->display multiplier, and whether
            # cache tokens count toward the displayed token total. mode "off"
            # hides the badge and skips the per-project /usage fetch.
            "usage_mode": config.usage.mode,
            "currency": config.usage.currency,
            "currency_symbol": config.usage.effective_symbol,
            "fx_rate": config.usage.fx_rate,
            "token_total_includes_cache": config.usage.token_total_includes_cache,
            # Live per-bridge metrics: master toggle, disk-part toggle, poll cadence.
            "metrics_enabled": config.metrics.enabled,
            "metrics_show_disk": config.metrics.show_disk,
            "metrics_poll_ms": int(config.metrics.poll_seconds * 1000),
            # Hosted channel (CL-4c): the live-view panel only renders when the
            # claustrum daemon is configured; otherwise there's nothing to host.
            "claustrum_enabled": config.claustrum.enabled,
            # Live pty-screen view (#534): the per-bridge "Live terminal" button only
            # renders when the (default-off) tap is enabled; it streams /ws/pty-screen.
            "pty_screen_enabled": config.claude.pty_screen_enabled,
            # Optional `pty` extra presence (#904): pyte backs the live-terminal render
            # and is NOT bundled in the signed binary (LGPL). Detected separately from the
            # config tap so the control can render enabled-but-greyed with an install hint
            # when the operator turned the tap on without the extra — no silent no-op.
            "pty_extra_present": deps.probe(deps.by_key("pyte")),
            "pty_extra_hint": deps.install_hint(deps.by_key("pyte")),
            # The hint is always a runnable command now (#904 slice 2b): `pip install
            # 'clauster[pty]'` off the binary, `clauster deps install pty` on it (pip is bundled).
            # The template prepends "run" for both, so the greyed control names a real command.
            "pty_extra_is_command": True,
            # Browser (Web Notifications) channel (#541): the master switch plus the
            # per-event toggles the client honours when a polled instance transitions.
            # The client requests Notification permission only when the channel is on.
            "browser_notifications_enabled": config.notifications.browser_enabled,
            "browser_notify_on_crash": config.notifications.notify_on_crash,
            "browser_notify_on_ready": config.notifications.notify_on_ready,
            "browser_notify_on_stop": config.notifications.notify_on_stop,
            # Config-management surface (#773): the navbar trigger + its modal render
            # only when config-write is enabled — the same invisible-surface invariant
            # the /api/config-write/* routes enforce (404 when off). allow_user_scope
            # gates whether the User scope option is offered at all.
            "config_write_enabled": config.config_write.enabled,
            "config_write_allow_user_scope": config.config_write.allow_user_scope,
            # Login shepherd (#839): the maintenance-zone panel only renders when
            # explicitly enabled — same invisible-surface invariant as the reaper UI
            # and config-write (the /api/login-shepherd/* routes 404 when off too).
            # allow_setup_token (#846) is the second, independent opt-in that gates
            # whether the higher-risk "Create a long-lived token" mode is offered at
            # all — when off, only the `login` (subscription sign-in) mode renders.
            "login_shepherd_enabled": config.login_shepherd.enabled,
            "login_shepherd_allow_setup_token": config.login_shepherd.allow_setup_token,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        return _render(request, "dashboard.html", await _dashboard_context())

    # Must run LAST: every /api/... route the public v1 surface aliases has to
    # already be registered above (#302).
    _mirror_v1_routes(app, _V1_PUBLIC_ROUTES)

    return app
