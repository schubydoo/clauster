"""FastAPI application factory (spec §7)."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sys
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from . import (
    __version__,
    atomicio,
    auth,
    claude_md,
    config_audit,
    config_write,
    config_write_hooks,
    config_write_mcp,
    config_write_mcp_cli,
    config_write_permissions,
    config_write_plugins,
    config_write_settings,
    config_write_skills,
    config_write_subagents,
    login_shepherd,
    login_status,
    setup_wizard,
    usage,
)
from .auth import LoginThrottle
from .claustrum_client import ClaustrumError
from .claustrum_daemon import ClaustrumDaemon
from .clone_jobs import CloneJobManager
from .config import ClausterConfig
from .db.stores import ApiTokenStore
from .discovery import (
    is_valid_project_name,
)
from .engine import ClausterEngine
from .hosted import HostedManager
from .models import (
    RemoteControlInstance,
)
from .redact import sanitize_line
from .routes import agents, dashboard, instances, login, transcripts, websockets
from .routes import ops as ops_routes
from .routes import projects as projects_routes
from .routes import usage as usage_routes
from .runner import (
    SessionRunner,
    _conpty_keeper_available,
)

logger = logging.getLogger(__name__)


def _pty_supported() -> bool:
    """Whether Interactive Session (pty) can launch on this host, for the dashboard mode picker.

    POSIX always (`pty.openpty`); on Windows only when the ConPTY keeper's `pywinpty` (the
    `pty` extra) is installed — otherwise a `launch_mode: pty` request falls back to Server
    Mode, so the picker shouldn't offer it (#914). Mirrors the runner's launch-time gate.

    The live caller moved to ``routes/projects.py`` with the dashboard row (#1156); this
    copy is retained as the canonical source the ``routes/*`` duplicates assert against,
    until the config-write domain also moves and they merge.
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
    config back.

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


def _iter_api_routes(routes: list) -> Iterator[APIRoute]:
    """Yield every ``APIRoute``, descending into FastAPI included-router wrappers.

    FastAPI 0.141 includes a router lazily (#1156): ``app.include_router`` adds an
    ``_IncludedRouter`` placeholder to ``app.router.routes`` rather than flattening
    its routes, and the real ``APIRoute`` objects live on
    ``wrapper.original_router.routes``. A wrapper is detected by duck-typing on
    ``original_router`` so a rename of the private ``_IncludedRouter`` class does not
    break the walk. A rename of the ``original_router`` attribute itself would drop
    the wrapper here; the route-table snapshot test's loud UNKNOWN fallback is the
    backstop that would catch that. Recurses, because an included router can itself
    include another.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        else:
            included = getattr(route, "original_router", None)
            if included is not None:
                # Refactor convention (#1156): routers declare full paths and are
                # included with no prefix, so ``route.path`` is the served URL and the
                # mirror + snapshot callers can read it verbatim. A prefix would map a
                # wrong path silently, so fail loudly instead of composing it here.
                ctx_prefix = getattr(getattr(route, "include_context", None), "prefix", "")
                if ctx_prefix or included.prefix:
                    raise RuntimeError(
                        "clauster: a routes/ router carries a prefix "
                        f"(include={ctx_prefix!r}, router={included.prefix!r}); "
                        "declare full paths (#1156)"
                    )
                yield from _iter_api_routes(included.routes)


def _mirror_v1_routes(app: FastAPI, public: frozenset[tuple[str, str]]) -> None:
    """Alias the public resource subset under ``/api/v1`` (#302), DRY.

    Must run AFTER every ``/api/...`` route in ``public`` is registered — it
    walks the routes already on ``app`` (including those on any router added via
    ``include_router``, see :func:`_iter_api_routes`) and, for each
    ``(method, path)`` match, re-registers the SAME ``endpoint`` callable (and its
    ``status_code`` / ``response_model``) under ``/api/v1/...``. No handler is
    copy-pasted, so the v1 alias can never drift from the internal route's
    behaviour.

    Fails loudly (``RuntimeError``) if any entry in ``public`` matches no
    registered route — a renamed/removed internal route must break the build,
    not silently vanish from the documented v1 surface.
    """
    found: set[tuple[str, str]] = set()
    for route in _iter_api_routes(list(app.router.routes)):
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
    the ``security_headers`` middleware. The module comment above owns the #442/#533
    rationale for the nonce gating and the dropped ``'unsafe-inline'``/``'unsafe-eval'``.

    Fail-closed: when ``nonce is None`` (a defensive degraded path that should not
    occur in normal request flow), both script-src and style-src still omit
    ``'unsafe-inline'`` — a degraded policy is *stricter*, never looser.
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


_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_DIR / "templates"
_STATIC_DIR = _PKG_DIR / "static"

# One year in seconds — the conventional far-future max-age for fingerprinted assets.
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"

# Every rendered HTML page carries a per-request CSP nonce and reflects live auth/session
# state, so it must never be served from the browser cache or bfcache: a cached copy would
# replay a stale nonce and could survive a deploy/hot-swap as an outdated render. `no-store`
# also makes the page bfcache-ineligible in Chrome, so a backgrounded tab reloads fresh.
_NO_STORE_CACHE = "no-store"


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


# Most a hosted session's transcript rehydration (#1045) will READ off disk. A live
# session's JSONL grows without bound and the rehydration runs inside the startup
# lifespan, so an unbounded parse could exhaust memory before the app serves anything.
# Past this the tail is read instead — comfortably more than the 200-turn render cap
# in `hosted._REHYDRATE_MAX_TURNS`, so the cap, not this, is what normally binds.
_HOSTED_HISTORY_MAX_BYTES = 4 * 1024 * 1024


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

    def _hosted_history(instance: RemoteControlInstance) -> list[dict]:
        """Read a hosted session's prior conversation from claude's transcript (#1045).

        The hosted stream lives only in server memory, so a restart left a reattached
        Direct session's view empty. claude has written the same conversation to its own
        ``.jsonl``; this reads it **read-only** (invariant 5 — clauster never mutates a
        transcript) via ``usage.read_transcript_turns``, which redacts every turn before
        it leaves the reader (invariant 4 — and ``HostedSession`` re-redacts on emit).

        Returns ``[]`` — "nothing to restore", never an error — when the row has no
        captured session uuid, an unsafe project name, or no transcript on disk. The
        project path is derived the same way a spawn derives it (``projects_root/name``),
        because a hosted row records the project *name*, not its cwd. Blocking; the
        manager runs it in a thread.

        The **read** is bounded, not just the ring insert: a long-running session's
        JSONL grows without limit, and this runs in the startup lifespan, so parsing a
        multi-gigabyte file whole could exhaust memory before the app ever serves. Past
        the cap only the tail is read. That tail starts mid-line; ``_line_to_turn``
        already skips an unparseable record, so the partial first line is dropped rather
        than rendered corrupt.
        """
        name = instance.project
        session_uuid = instance.claude_session_uuid
        if not session_uuid or not name or not is_valid_project_name(name):
            return []
        path = usage.resolve_session_transcript(config.projects_root / name, session_uuid)
        if path is None:
            return []
        size = path.stat().st_size
        if size <= _HOSTED_HISTORY_MAX_BYTES:
            return usage.read_transcript_turns(path)
        turns, _offset, _reset = usage.read_transcript_turns_from_offset(
            path, size - _HOSTED_HISTORY_MAX_BYTES
        )
        return turns

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Start the poll loop and hosted daemon on startup; detach from both on shutdown.

        Shutdown deliberately leaves the daemon, hosted sessions, and bridges running —
        it only detaches, cancels the poll task, reaps the login shepherd, and disposes
        the DB connection pool.
        """
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
                    await app.state.hosted.reattach_all(daemon.client, history_for=_hosted_history)
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
    # Publish the engine for the typed get_engine accessor that moved route modules
    # use (#1156); a handler inside create_app closed over it directly.
    app.state.engine = engine
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
        # Notification channel (#541): the "come look" signal — fail-closed (unlike the
        # fail-open webhook above), fire-and-forget.
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
    # Hold strong refs to in-flight clone tasks so they aren't GC'd mid-run; published
    # on app.state so the clone route (routes/projects.py) shares the per-app set.
    clone_tasks: set[asyncio.Task] = set()
    app.state.clone_tasks = clone_tasks
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
        response = templates.TemplateResponse(request, name, ctx, **kwargs)
        # The per-request nonce above (and the live session state each page reflects) must
        # never be reused from cache — see _NO_STORE_CACHE.
        response.headers["Cache-Control"] = _NO_STORE_CACHE
        return response

    # Publish the nonce-aware renderer for the get_render accessor that moved route
    # modules use (#1156); a handler inside create_app closed over it directly.
    app.state.render = _render

    # Compress responses over the threshold — the ~665KB uncompressed Tabler/Alpine
    # bundle and the JSON poll responses both shrink ~4-5x for remote/proxied clients
    # that don't compress at the proxy (invisible on LAN). Below it, the gzip overhead
    # isn't worth it.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", _ImmutableStaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ----- error handling: one JSON/HTML shape for every HTTPException ---------------------------
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        """Render a friendly HTML 404 for browser navigation; keep JSON for the API.

        An unmatched route otherwise dead-ends a stale/mistyped URL on a bare
        ``{"detail": "Not Found"}`` body with no way back. Only a 404 on a non-API path
        from an ``Accept: text/html`` client gets the page (the method is not consulted);
        ``/api`` + ``/ws`` and JSON clients keep the machine-readable error, so the API
        contract (and its tests) stay intact.
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
    # Published for the moved WebSocket gate (routes/websockets.py) to read via
    # dependencies.get_allowed_origins. The in-app HTTP CSRF gate (_origin_allowed)
    # still reads the closure var, so both share the one set computed here (#1156).
    app.state.allowed_origins = _allowed_origins
    _throttle = LoginThrottle()
    # Session epoch: cookies embed it at issue; logout bumps it so every
    # outstanding cookie (incl. a captured one) is revoked. Persisted, so a
    # restart doesn't silently un-revoke. Cached on app.state — single uvicorn
    # worker, so the in-memory value is authoritative.
    app.state.session_epoch = auth.read_epoch(config.state_dir)
    # Published for the moved login/logout/reauth routes (routes/login.py) to read via
    # dependencies (#1156). The objects stay built here -- _authenticate (stays) reads the
    # session serializer, require_elevated (stays) reads the elevation serializer, and both
    # login and reauth must share the single throttle instance and password hasher.
    app.state.login_serializer = _serializer
    app.state.elevation_serializer = _elevation_serializer
    app.state.password_hasher = _hasher
    app.state.login_throttle = _throttle

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

    # Published for the moved /healthz route (routes/ops.py) to read via
    # dependencies.get_authenticate. The closure stays here -- it binds the auth
    # config, signing serializer, and token store -- so no auth logic moves (#1156).
    app.state.authenticate = _authenticate

    def _origin_allowed(request: Request) -> bool:
        """Return whether the request's Origin is allowlisted; an absent Origin is rejected."""
        # Origin only: Referer is spoofable/suppressible (Referrer-Policy, downgrades)
        # so it's not trusted for CSRF. Modern browsers always send Origin on a
        # state-changing fetch/XHR/form POST; its absence => reject.
        origin = request.headers.get("origin")
        if origin is None:
            return False
        return auth.normalize_origin(origin) in _allowed_origins

    def _cookie_secure(request: Request) -> bool:
        """Decide whether the session cookie gets the ``Secure`` flag for this request."""
        mode = config.auth.cookie_secure
        if mode != "auto":
            return mode == "always"
        if request.url.scheme == "https":
            return True
        rp = config.auth.reverse_proxy
        if rp.enabled and auth.peer_trusted(auth.peer_ip(request), rp.trusted_ips):
            return request.headers.get("x-forwarded-proto", "").lower() == "https"
        return False

    # Published for the moved login/logout/reauth routes (routes/login.py) to read via
    # dependencies.get_cookie_secure. The closure stays here -- the security_headers
    # middleware also calls it for the HSTS decision, so the cookie Secure flag and the
    # HSTS header always agree (#1156).
    app.state.cookie_secure = _cookie_secure

    def _is_public(path: str) -> bool:
        """Return whether ``path`` is reachable without an authenticated session."""
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

    # ----- middleware: the fail-closed auth gate, UI guard, security headers ---------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Apply the CSRF Origin gate to unsafe methods, and authenticate when auth is on.

        Proxy-HMAC and Bearer credentials are exempt from the Origin check; with auth
        off, only a *present* non-allowlisted Origin is rejected (an absent one passes).
        """
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
        """Return 404 for the dashboard surfaces when ``ui.enabled`` is off."""
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
        HSTS follows the same ``_cookie_secure`` decision the session cookie's
        ``Secure`` flag uses: under the default ``auth.cookie_secure: auto`` that
        means an https scheme (or a trusted proxy's ``X-Forwarded-Proto``), so a
        plain-HTTP LAN deployment never pins a browser to a scheme it can't serve.
        The ``always`` / ``never`` overrides force it on or off regardless of scheme.

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

    # ----- step-up elevation gate for the Tier-B config-write surface ------------------------
    # The login/logout/reauth routes moved to routes/login.py and the dashboard page to
    # routes/dashboard.py (#1156); it stays because config-write routes still in app.py use it.
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

    # Published for the moved Tier-B config routes (routes/ops.py) to read via
    # dependencies.get_require_elevated. The closure stays here -- it binds the
    # elevation serializer and reads the live app.state.session_epoch (#1156).
    app.state.require_elevated = require_elevated

    # ----- config-write (Tier-B): MCP, permissions, hooks, CLAUDE.md, subagents, -----------------
    # -----   skills, settings, plugins, marketplaces ---------------------------------------------
    @app.get("/api/config-write/status")
    async def api_config_write_status() -> dict:
        """Report the config-write opt-in flags, or 404 when the capability is off."""
        # Foundation surface for the code-executing config-write trust tier (#347/#687);
        # the concrete writers (#688-#691) sit behind this same gate. It fail-closes:
        # when config_write.enabled is off this 404s (the surface is invisible, same as
        # the reaper), so a disabled deployment exposes nothing. The body reflects only
        # the two opt-in flags, never any config content.
        config_write.require_capability(config, "project")
        return config_write.capability_status(config)

    def _resolve_cw_project(name: object, *, require_exists: bool = False) -> Path:
        """Resolve a project-scope config-write path, validating containment before any I/O.

        A missing or non-string name is a 422 and an escaping one a 400; with
        ``require_exists`` an absent project directory is a clean 404 rather than an
        unhandled error inside the writer.
        """
        # Validate-before-I/O path containment: an escaping path raises PathEscapeError
        # before any write. The name is also the type-the-name confirm token
        # (server-re-derived below).
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
        """Map a typed config-write failure to its fail-closed HTTP status."""
        # InvalidCandidate ⇒ 422 (bad shape); Stale/ServerExists ⇒ 409; ServerNotFound,
        # AgentNotFound, PluginNotFound, MarketplaceNotFound ⇒ 404; ReadOnlyAgent ⇒ 403.
        # Every other ConfigWriteError — including PathEscapeError, which the routes catch
        # earlier as a 400 before the writer is even reached — falls through to a 400.
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
        """Return the structurally redacted MCP server map for a surface."""
        # Gated exactly like the status route: 404 when config-write is off, and 404 for
        # user scope when allow_user_scope is off — the surface is invisible, never 403.
        # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s for ANY
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
        """Replace the whole MCP server map for a surface."""
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
        """Add, edit, or remove a single MCP server entry behind the config-write gate."""
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
            """Write one entry with this scope's direct, non-spawning writer."""
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
            """Read the current entry unredacted, in memory only, to enable an edit rollback."""
            # UNREDACTED single-entry read for the edit-rollback (in-memory, same request,
            # never serialized to a response/log — see config_write_mcp.snapshot_server_entry).
            return config_write_mcp.snapshot_server_entry(
                scope,  # type: ignore[arg-type]
                name,
                claude_json=runner.claude_json,
                project_dir=cli_cwd,
            )

        def _work() -> None:
            """Dispatch the add/edit/remove to the CLI or to the direct writer."""
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
                    """Put the pre-edit entry back, reporting whether one actually existed."""
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
        """Return the project's ``.mcp.json`` server approval lists."""
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
        """Replace the project's enabled/disabled MCP approval lists."""
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
        """Clear both of the project's approval lists via the CLI's reset verb."""
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
        """Resolve the user-scope ``settings.json``, failing closed with a 404 without a runner."""
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
        """Return the permission-rules block for a surface, 404 when the surface is gated off."""
        # Gated exactly like the MCP/status routes: 404 when config-write is off, and 404
        # for user scope when allow_user_scope is off — the surface is invisible, never
        # 403. A corrupt/non-object on-disk
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
        """Replace the permission-rules block for a surface."""
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
        """Return the stored (inert) hooks block for a surface; reading never runs a command."""
        # Gated exactly like the permissions/MCP/status routes: 404 when config-write is
        # off, and 404 for user scope when allow_user_scope is off — the surface is
        # invisible, never 403. A corrupt/non-object on-disk settings.json raises
        # InvalidCandidateError from _load_json_obj; map it through the same helper as the
        # PUT route so a hand-edited file is a clean 422, never a 500.
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
        """Replace the hooks block for a surface, storing commands as inert, unexecuted data."""
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
        """Return CLAUDE.md for a surface as raw text — content is never redacted (#768)."""
        # Gated exactly like the permissions/hooks/MCP routes: 404 when config-write is
        # off, and 404 for user scope when allow_user_scope is off — the surface is
        # invisible, never 403. Content-tier: unlike the skills file route, this one
        # returns free-form file text with no redacted companion field (#768 threat model).
        #
        # Capability gate FIRST, before the scope-enum check, so a disabled surface 404s
        # for ANY request (a bogus scope included), never a differing 422 that would leak
        # that the endpoint exists (#819/#768).
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
        """Replace CLAUDE.md for a surface behind the capability and type-the-name gates."""
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
        """List the subagents defined at user or project scope."""
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
        """Return one subagent's detail doc; a built-in name yields a synthetic read-only doc."""
        # The synthetic built-in doc is 200-shaped (it really exists in Claude Code,
        # just not as a file) — never a 404. A missing real file raises AgentNotFoundError,
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
        """Create or replace one subagent, refusing built-in and plugin-owned names."""
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
        """Delete one subagent; built-in and plugin-owned names are refused, absent ones no-op."""
        # Same fail-closed gate order as the PUT route (capability -> scope-enum ->
        # confirm -> read-only/path guards inside the deleter). A refusal is a 403; a
        # genuinely absent ordinary name is `deleted: false`, never an error.
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
        """Resolve the user-scope ``~/.claude.json``, failing closed with 404 without a runner."""
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
        """List the skills defined at user or project scope."""
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
        """Return one redacted file from inside a skill directory (``SKILL.md`` by default)."""
        # Redaction closes the #813 INFO-1 gap here, unlike the CLAUDE.md content-tier
        # route -- see config_write_skills' module docstring.
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
        """Create or replace a skill directory; any script body needs a second explicit confirm."""
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
        """Delete a skill directory behind the same type-the-name confirm a write requires."""
        # The confirm is because deletion is irreversible — no undo store. Capability
        # gate FIRST, before the scope-enum check.
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
        """Return the ``skillOverrides`` visibility map for a surface."""
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
        """Replace the ``skillOverrides`` map — inert visibility state, never executed."""
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
        """Return the settings.json keys no dedicated surface owns, with env values masked."""
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
        """Replace those settings keys, keeping any env value the client resent as the mask."""
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
        """Return each setting's effective value plus the scope layer that supplied it."""
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
        """Resolve the directory ``claude plugin ...`` should be spawned from for this scope."""
        # User scope has no project — an arbitrary safe directory the
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
        """List the installed plugins via the CLI — this state has no file to read directly."""
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
        """Read the plugin enable/disable map straight from the settings file, no CLI spawn."""
        # Mirrors the MCP surface's "file read for display" doctrine; no secret ever
        # lives here.
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
        """Return one plugin's component inventory and token-cost projection from the CLI."""
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
        """Enable, disable, install, uninstall, or update a plugin; install needs a 2nd confirm."""
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
            """Run the requested plugin verb through the CLI."""
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
        """List the merged marketplace pool via the CLI."""
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
        """Read which marketplaces this scope declares — what the merged list cannot tell you."""
        # Direct (non-spawning) read of the PER-SCOPE `extraKnownMarketplaces`
        # declaration -- needed to know where a remove/add would land.
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
        """Add, remove, or update a marketplace behind the scope/confirm plumbing.

        ``add``/``remove`` always state their scope explicitly; ``update`` takes none.
        """
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
            """Run the requested marketplace verb through the CLI."""
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

    # Domain routers split out of create_app (#1156). Registered before the v1
    # mirror below so their /api/... routes exist when the mirror walks the table.
    app.include_router(projects_routes.router)
    app.include_router(transcripts.router)
    app.include_router(usage_routes.router)
    app.include_router(agents.router)
    app.include_router(instances.router)
    app.include_router(ops_routes.router)
    app.include_router(websockets.router)
    app.include_router(login.router)
    app.include_router(dashboard.router)

    # Must run LAST: every /api/... route the public v1 surface aliases has to
    # already be registered above (#302).
    _mirror_v1_routes(app, _V1_PUBLIC_ROUTES)

    return app
