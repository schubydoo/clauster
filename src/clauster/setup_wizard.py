"""First-run setup wizard (#978): a minimal app served when no config exists.

``clauster run`` with no ``clauster.yml`` starts this instead of aborting. It collects the
minimum needed to boot safely — ``projects_root``, the bind host, and an operator password
— writes a fresh ``clauster.yml`` with **authentication enabled**, and signals a re-exec so
the real app boots on the new config. Deliberately tiny + standalone: :func:`clauster.app.
create_app` needs a full valid config, which by definition does not exist on first run.

Security envelope — two mutually exclusive modes, chosen by the bind host:

* **Loopback (default).** There is no auth yet, so the server binds ``127.0.0.1``
  (``SETUP_HOST``) — only a local operator (or an SSH tunnel) can reach it; the loopback
  bind is the boundary. The submit is **Origin-checked** against the loopback origin so a
  cross-site page in the operator's browser cannot POST a password to ``localhost``.
* **Token-gated (non-loopback, opt-in).** A container publishes an external interface, so a
  loopback-bound wizard is unreachable (#1017): with ``CLAUSTER_SETUP_HOST`` set to a
  non-loopback address (the Docker image sets ``0.0.0.0``) the launcher binds there and mints
  a **one-time setup token** printed to the log. The token gates the page (``?token=`` on the
  GET) and the submit (an ``X-Setup-Token`` request header). The header is the CSRF defense —
  a cross-site page cannot set a custom header on a cross-origin request without a preflight
  the wizard never approves — and the token, known only from the server log, keeps a reachable
  bind from being an open, auth-less config-writer. This is strictly stronger than a bare
  non-loopback bind, which a plain ``ClausterConfig`` load refuses outright (#88).
* **Fail closed.** A bad ``projects_root`` / short / mismatched password writes nothing, and
  the generated config is re-validated (``ClausterConfig``) before it is committed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Any

import markupsafe
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import __version__, auth
from .atomicio import fsync_dir
from .config import _LOOPBACK_HOSTS, ClausterConfig

_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PKG_DIR / "templates"
_STATIC_DIR = _PKG_DIR / "static"

# Per-vendor "don't autofill" attribute set for NON-credential inputs (#1036), rendered raw via
# a `{{ NO_AUTOFILL }}` template global. `autocomplete="off"` alone is ignored by Chromium Autofill
# and the manager extensions (crbug 468153), so non-password fields also carry the 1Password /
# LastPass / Bitwarden / Dashlane opt-out `data-*` attributes. Defined here (the lighter,
# pre-config module) and shared with the main app's template env; password fields deliberately
# omit it so login autofill keeps working. `Markup` so it isn't autoescaped.
NO_AUTOFILL = markupsafe.Markup(
    'autocomplete="off" data-1p-ignore data-lpignore="true" data-bwignore data-form-type="other"'
)

SETUP_HOST = "127.0.0.1"
DEFAULT_PORT = int(ClausterConfig.model_fields["port"].default)
MIN_PASSWORD_LEN = 8
_LOOPBACK_HOSTNAMES = ("127.0.0.1", "localhost", "[::1]")
# A non-loopback bind tells the browser nothing, so those hosts are for the config only —
# never a place the wizard itself is reachable.
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})  # noqa: S104 - compared against, never a bind

_CSP = (
    "default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

# Shown (403) in token mode when the GET lacks a valid ``?token=`` — a static, resource-free
# page so it needs no CSP nonce, and it never echoes the supplied value (no reflection).
_SETUP_GATE_HTML = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    "<title>Clauster setup — token required</title></head><body>"
    "<h1>Setup token required</h1>"
    "<p>This first-run setup server is bound to a non-loopback address, so it is gated by a "
    "one-time token. Open the full setup URL printed in the server log — it looks like "
    "<code>http://&lt;host&gt;:&lt;port&gt;/?token=&hellip;</code>.</p>"
    "</body></html>"
)


def _loopback_origins(port: int) -> set[str]:
    """Return the Origins a same-origin submit to the loopback setup server can carry."""
    return {f"http://{host}:{port}" for host in _LOOPBACK_HOSTNAMES}


def _is_loopback_host(host: str) -> bool:
    """Whether binding ``host`` keeps the wizard on the loopback trust boundary.

    ``localhost`` and any loopback literal (``127.0.0.0/8``, ``::1``, bracketed or not) are
    loopback; a wildcard (``0.0.0.0``/``::``), a specific LAN address, or an unresolvable
    hostname are not — and the fail-safe for "can't tell" is **not loopback** (→ token-gated),
    never the reverse.
    """
    h = host.strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def resolve_setup_host() -> str:
    """Return the wizard bind host: ``$CLAUSTER_SETUP_HOST`` if set, else loopback (#1017).

    The Docker image sets ``CLAUSTER_SETUP_HOST=0.0.0.0`` so a published port can reach the
    wizard; a host install leaves it unset and stays loopback-only.
    """
    return os.environ.get("CLAUSTER_SETUP_HOST", "").strip() or SETUP_HOST


def host_is_bindable(host: str) -> bool:
    """Return whether ``host`` can actually be bound on this machine (#1017 review).

    ``CLAUSTER_HOST`` is written to the config and reapplied over it on the post-setup re-exec.
    Unlike the port, *any* string passes config validation, so an unresolvable name or an address
    not assigned to a local interface loads fine and then fails only when the re-exec's uvicorn
    binds — after setup already "succeeded". This resolves and attempts an ephemeral bind so the
    wizard can refuse such a value up front. Best-effort: any resolve/bind error → not bindable;
    a wildcard / loopback / locally-assigned address → bindable.
    """
    try:
        infos = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.bind(sockaddr)
                return True
        except OSError:
            continue
    return False


def resolve_env_port() -> int | None:
    """Return the ``CLAUSTER_PORT`` env override (the app's future bind port), or ``None``.

    Like ``CLAUSTER_HOST``, this env wins over the written file on the post-setup re-exec, so the
    wizard fixes its port field to it rather than offering a choice it would silently ignore
    (#1017 review). A missing / non-numeric / out-of-range value returns ``None`` — the operator
    picks, and ``load_config`` validates the env override on the restart.
    """
    raw = os.environ.get("CLAUSTER_PORT", "").strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    return val if 1 <= val <= 65535 else None


def mint_setup_token(host: str) -> str | None:
    """Return a one-time setup token for a non-loopback bind, or ``None`` for loopback (#1017).

    The token gates a reachable wizard; a loopback bind is already the boundary and needs none.
    """
    return None if _is_loopback_host(host) else secrets.token_urlsafe(32)


def setup_url(host: str, port: int, *, token: str | None = None) -> str:
    """Return the URL to reach the wizard, carrying the one-time ``?token=`` when set.

    A wildcard bind has no single host, so the host renders as a ``<this-host>`` placeholder the
    operator substitutes — but the token (the part they cannot guess) is always literal.
    """
    base = _display_url(host, port)  # ends with "/"
    return f"{base}?token={token}" if token else base


def _display_url(host: str, port: int) -> str:
    """Return the URL to point the operator at after setup (a wildcard bind has no host)."""
    if host in _WILDCARD_HOSTS:
        return f"http://<this-host>:{port}/"
    return f"http://{host}:{port}/"


def _origins_to_persist(host: str, origin: str | None) -> list[str]:
    """Return the origins to record in ``auth.allowed_origins`` for a ``host`` bind.

    ``auth.build_allowed_origins`` auto-allows ``127.0.0.1``/``localhost``/``[::1]`` only when
    the bind host is in ``_LOOPBACK_HOSTS``; every other bind auto-allows **nothing**. So
    unless the operator's browser origin is recorded here, the very next request after setup
    — the login POST — fails the Origin check with 403 and the dashboard is unreachable. The
    Docker image bakes ``CLAUSTER_HOST=0.0.0.0``, so that was every containerised first run
    (#1071).

    Keyed on ``_LOOPBACK_HOSTS`` — the exact set the runtime gate consults — and deliberately
    NOT on the neighbouring :func:`_is_loopback_host`, which counts all of ``127.0.0.0/8``: a
    ``127.0.0.2`` bind would look loopback to it, get nothing written, and still 403.

    The Origin is trusted exactly as far as the submit that carried it, and reaching this point
    already cleared the wizard's gate — token mode verified ``X-Setup-Token``, loopback mode
    checked the Origin against the loopback set. An absent or malformed Origin (a scripted
    submit, never a browser) writes nothing rather than guessing a value that would silently
    widen the allowlist.
    """
    if host in _LOOPBACK_HOSTS or not origin:
        return []
    normalized = auth.normalize_origin(origin)
    # normalize_origin returns the cleaned input unchanged when it can't parse a
    # scheme+host, so re-check the shape rather than trusting it round-tripped.
    scheme, _, rest = normalized.partition("://")
    if scheme not in ("http", "https") or not rest:
        return []
    return [normalized]


def _build_config_data(
    projects_root: str,
    host: str,
    port: int,
    password_hash: str,
    allowed_origins: list[str] | None = None,
) -> dict:
    """Build the clauster.yml mapping the wizard writes — auth enabled, password required."""
    auth_block: dict[str, Any] = {
        "enabled": True,
        "password_required": True,
        "password_hash": password_hash,
    }
    # Omitted rather than written empty when there is nothing to record, so a loopback
    # install's generated config stays exactly as it was before #1071.
    if allowed_origins:
        auth_block["allowed_origins"] = allowed_origins
    return {
        "projects_root": projects_root,
        "host": host,
        "port": port,
        "auth": auth_block,
    }


def _atomic_write_config(target: Path, text: str) -> None:
    """Atomically write the config at mode 0600 WITHOUT tightening the parent directory.

    Unlike :func:`clauster.atomicio.atomic_write_text`, this must not chmod ``target``'s
    parent to 0700 (#978 review): the wizard writes into an operator-chosen directory — often
    the cwd or ``$CLAUSTER_HOME`` — that may be shared, and forcing it owner-only would lock
    out other users/services. ``mkstemp`` creates the temp at 0600 and ``os.replace`` keeps
    that, so the config file (which carries the argon2 password hash) is private without
    disturbing the directory it lives in.
    """
    directory = target.parent
    # Create missing parents (e.g. a fresh `-c /opt/clauster/prod/clauster.yml`) with default
    # permissions. exist_ok=True never re-chmods an EXISTING directory, so a shared cwd is
    # left as-is — satisfying both "create the parent" and "don't tighten it to 0700".
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=target.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        fsync_dir(directory)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def create_setup_app(
    write_path: Path,
    *,
    port: int = DEFAULT_PORT,
    setup_token: str | None = None,
    env_host: str | None = None,
    env_port: int | None = None,
) -> FastAPI:
    """Build the first-run setup app that writes ``write_path`` (#978, token-gate #1017).

    ``setup_token`` selects the security mode (see the module docstring): pass ``None`` for the
    default **loopback** mode (Origin-checked, no token) and a secret string for **token-gated**
    mode (a non-loopback bind — the GET needs ``?token=`` and the submit an ``X-Setup-Token``
    header). On a successful submit it sets ``app.state.setup_complete`` and asks the wired
    uvicorn server to shut down, so the ``clauster run`` caller can re-exec onto the new config.

    ``env_host`` / ``env_port`` are the ``CLAUSTER_HOST`` / ``CLAUSTER_PORT`` env values when set
    (the Docker case): because an env override wins over the written file on re-exec, a
    free-choice bind field would be a control that lies. So when one is set the wizard fixes that
    field to it — it renders read-only and the submit writes the env value regardless of what was
    posted — and the operator sees the address/port the app will actually bind (#1017 review).
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["NO_AUTOFILL"] = NO_AUTOFILL  # #1036 (this env is separate from app's)
    app.state.setup_complete = False
    app.state.uvicorn_server = None
    token_required = setup_token is not None
    allowed_origins = _loopback_origins(port)
    hasher = auth.make_hasher()
    setup_lock = asyncio.Lock()  # serialize concurrent submits so last-writer-wins can't lock out

    def _token_ok(supplied: str | None) -> bool:
        """Constant-time check of a supplied token against the expected one (token mode)."""
        # setup_token is a str here (token_required is True). compare_digest avoids leaking
        # the token length/prefix through timing; a missing value fails closed.
        return supplied is not None and secrets.compare_digest(supplied, setup_token or "")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "setup"}

    @app.get("/", response_class=HTMLResponse)
    async def form(request: Request) -> Response:
        # Token mode: the page itself is gated, so a reachable non-loopback bind never even
        # renders the form to someone who didn't read the token from the server log. The token
        # is then embedded (below) so the same-origin submit can echo it as a header.
        if token_required and not _token_ok(request.query_params.get("token")):
            return HTMLResponse(_SETUP_GATE_HTML, status_code=403)
        nonce = secrets.token_urlsafe(16)
        resp = templates.TemplateResponse(
            request,
            "setup.html",
            {
                "csp_nonce": nonce,
                "asset_version": __version__,
                "min_password_len": MIN_PASSWORD_LEN,
                "default_port": port,
                # Only rendered in token mode (empty string otherwise → the attribute is omitted
                # and setup.js sends no header, exactly the loopback flow).
                "setup_token": setup_token or "",
                # When set (CLAUSTER_HOST / CLAUSTER_PORT), that field is read-only at this value —
                # the app binds it regardless of what's posted, so the control can't lie (#1017).
                "env_host": env_host or "",
                "env_port": env_port or "",
            },
        )
        resp.headers["Content-Security-Policy"] = _CSP.format(nonce=nonce)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    @app.post("/setup")
    async def submit(request: Request) -> Response:
        # No auth exists yet, so the submit is CSRF-gated — one of two ways (module docstring):
        # * Token mode (non-loopback bind): require the one-time token as an ``X-Setup-Token``
        #   header. A custom header can't be set on a cross-origin request without a CORS
        #   preflight the wizard never answers, so this is CSRF-safe, and the token (known only
        #   from the server log) keeps a reachable bind from being an open config-writer. The
        #   browser Origin is the operator's own LAN address, which can't be pre-allowlisted, so
        #   the header replaces the Origin check here.
        # * Loopback mode: a remote peer can't reach a loopback bind, so the only risk is a
        #   cross-site page in the operator's browser POSTing to localhost — gate on a loopback
        #   Origin.
        if token_required:
            if not _token_ok(request.headers.get("x-setup-token")):
                return JSONResponse({"detail": "setup token missing or invalid"}, status_code=403)
        else:
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return JSONResponse({"detail": "origin check failed"}, status_code=403)
        try:
            body: Any = await request.json()
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        errors: dict[str, str] = {}
        pr_raw = str(body.get("projects_root", "")).strip()
        if not pr_raw:
            errors["projects_root"] = "Required."
        # Existence/readability is validated by ClausterConfig below (its field validator does
        # the is_dir check) rather than with a direct path operation on request data; a bad
        # path is mapped back to a friendly field error there.

        # A CLAUSTER_HOST env override wins over the written file on re-exec, so when it's set the
        # bind is fixed to it — writing what was posted would put a value in the config the app
        # then ignores (a control that lies, #1017 review). Loopback/host installs keep the
        # posted choice.
        host = env_host or (str(body.get("host", "")).strip() or SETUP_HOST)
        if env_port is not None:
            # CLAUSTER_PORT is pre-validated (resolve_env_port) and wins on re-exec, so — like the
            # host — the posted port is ignored and the config records what the app will bind.
            port_val = env_port
        else:
            try:
                port_val = int(body.get("port", port))
            except (TypeError, ValueError):
                errors["port"] = "Must be a number."
                port_val = port
            else:
                if not 1 <= port_val <= 65535:
                    errors["port"] = "Must be between 1 and 65535."

        password = str(body.get("password", ""))
        confirm = str(body.get("confirm", ""))
        if len(password) < MIN_PASSWORD_LEN:
            errors["password"] = f"Use at least {MIN_PASSWORD_LEN} characters."
        elif password != confirm:
            errors["confirm"] = "Passwords do not match."

        if errors:
            return JSONResponse({"errors": errors}, status_code=400)

        # Serialize the write: two in-flight submits would otherwise both validate and write
        # different passwords (last-writer-wins), locking the first operator out. The second
        # to acquire sees the completed flag and is rejected (#978 review).
        async with setup_lock:
            if app.state.setup_complete:
                return JSONResponse(
                    {"detail": "setup has already been completed"}, status_code=409
                )
            password_hash = auth.hash_password(hasher, password)
            # Store projects_root as typed (the model expands `~` on load); passing the raw
            # string — instead of constructing a Path from request data — keeps path handling
            # inside the model.
            data = _build_config_data(
                pr_raw,
                host,
                port_val,
                password_hash,
                _origins_to_persist(host, request.headers.get("origin")),
            )
            # Final fail-closed gate: the model validates projects_root existence/readability
            # and every other field. A projects_root failure maps to a friendly field error;
            # anything else is generic — the raw exception (which can carry internal paths) is
            # never surfaced.
            try:
                ClausterConfig.model_validate(data)
            except ValidationError as exc:
                if any(e.get("loc") == ("projects_root",) for e in exc.errors()):
                    return JSONResponse(
                        {
                            "errors": {
                                "projects_root": "Must be a directory that already exists "
                                "and is readable."
                            }
                        },
                        status_code=400,
                    )
                return JSONResponse(
                    {"detail": "the settings could not be validated"}, status_code=400
                )
            content = "# Generated by the clauster first-run setup wizard.\n" + yaml.safe_dump(
                data, sort_keys=False, default_flow_style=False
            )
            try:
                _atomic_write_config(write_path, content)
            except OSError:
                return JSONResponse(
                    {"detail": "could not write the configuration file"}, status_code=500
                )
            app.state.setup_complete = True
            server = app.state.uvicorn_server
            if server is not None:
                server.should_exit = True  # graceful shutdown -> the run() caller re-execs
        return JSONResponse({"ok": True, "url": _display_url(host, port_val)})

    return app
