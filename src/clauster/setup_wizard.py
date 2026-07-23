"""First-run setup wizard (#978): a minimal, loopback-only app served when no config exists.

``clauster run`` with no ``clauster.yml`` starts this instead of aborting. It collects the
minimum needed to boot safely — ``projects_root``, the bind host, and an operator password
— writes a fresh ``clauster.yml`` with **authentication enabled**, and signals a re-exec so
the real app boots on the new config. Deliberately tiny + standalone: :func:`clauster.app.
create_app` needs a full valid config, which by definition does not exist on first run.

Security envelope:

* **Loopback only.** There is no auth yet, so the server binds ``127.0.0.1`` (``SETUP_HOST``)
  — only a local operator (or an SSH tunnel) can reach it. A non-loopback bind without auth
  is already refused at load (#88), so the wizard is inherently local, and the loopback bind
  is the boundary (a redundant peer-IP check would just be dead code behind it).
* **CSRF-gated.** The submit is Origin-checked against the loopback origin so a cross-site
  page in the operator's browser cannot POST a password to ``localhost``.
* **Fail closed.** A bad ``projects_root`` / short / mismatched password writes nothing, and
  the generated config is re-validated (``ClausterConfig``) before it is committed.
"""

from __future__ import annotations

import asyncio
import os
import secrets
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
from .config import ClausterConfig

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


def _loopback_origins(port: int) -> set[str]:
    """Return the Origins a same-origin submit to the loopback setup server can carry."""
    return {f"http://{host}:{port}" for host in _LOOPBACK_HOSTNAMES}


def _display_url(host: str, port: int) -> str:
    """Return the URL to point the operator at after setup (a wildcard bind has no host)."""
    if host in _WILDCARD_HOSTS:
        return f"http://<this-host>:{port}/"
    return f"http://{host}:{port}/"


def _build_config_data(projects_root: str, host: str, port: int, password_hash: str) -> dict:
    """Build the clauster.yml mapping the wizard writes — auth enabled, password required."""
    return {
        "projects_root": projects_root,
        "host": host,
        "port": port,
        "auth": {
            "enabled": True,
            "password_required": True,
            "password_hash": password_hash,
        },
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


def create_setup_app(write_path: Path, *, port: int = DEFAULT_PORT) -> FastAPI:
    """Build the loopback-only first-run setup app that writes ``write_path`` (#978).

    On a successful submit it sets ``app.state.setup_complete`` and asks the wired uvicorn
    server to shut down, so the ``clauster run`` caller can re-exec onto the new config.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["NO_AUTOFILL"] = NO_AUTOFILL  # #1036 (this env is separate from app's)
    app.state.setup_complete = False
    app.state.uvicorn_server = None
    allowed_origins = _loopback_origins(port)
    hasher = auth.make_hasher()
    setup_lock = asyncio.Lock()  # serialize concurrent submits so last-writer-wins can't lock out

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "setup"}

    @app.get("/", response_class=HTMLResponse)
    async def form(request: Request) -> Response:
        nonce = secrets.token_urlsafe(16)
        resp = templates.TemplateResponse(
            request,
            "setup.html",
            {
                "csp_nonce": nonce,
                "asset_version": __version__,
                "min_password_len": MIN_PASSWORD_LEN,
                "default_port": port,
            },
        )
        resp.headers["Content-Security-Policy"] = _CSP.format(nonce=nonce)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    @app.post("/setup")
    async def submit(request: Request) -> Response:
        # No auth exists yet. The server only ever binds loopback (SETUP_HOST), so a remote
        # peer can't reach this; the remaining risk is a cross-site page in the operator's
        # own browser POSTing a password to localhost, so gate on a loopback Origin (CSRF).
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

        host = str(body.get("host", "")).strip() or SETUP_HOST
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
            data = _build_config_data(pr_raw, host, port_val, password_hash)
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
