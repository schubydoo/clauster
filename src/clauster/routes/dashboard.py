"""The dashboard page route and its shared template context (#1156).

``GET /`` renders ``dashboard.html`` through the shared renderer published on
``app.state`` so it keeps the per-request CSP nonce (the page carries inline
``<script>`` blocks) and the ``Cache-Control: no-store`` header. The heavy lifting
is :func:`_dashboard_context`, which assembles the server-injected configuration the
Alpine components read once at page load -- the launch-mode picker, the usage/metrics
badges, and the invisible-surface flags (reaper, config-write, login-shepherd,
pty-screen) that gate whether a panel renders at all.

``GET /`` stays in ``_UI_ONLY_ROUTES`` (a byte-identical move keeps it there) and the
project list is read through the same discovery facade the Projects routes use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from .. import __version__, config_write_hooks, deps
from ..config import BYPASS_DESKTOP_HINT, PERMISSION_LABELS
from ..dependencies import ConfigDep, EngineDep, RenderDep
from ._common import list_projects
from .projects import _pty_supported

if TYPE_CHECKING:
    from ..config import ClausterConfig
    from ..engine import ClausterEngine

router = APIRouter()


async def _dashboard_context(config: ClausterConfig, engine: ClausterEngine) -> dict:
    """Build the shared template context for the dashboard."""
    projects = await list_projects(engine)
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


# ----- the dashboard page itself -------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, config: ConfigDep, engine: EngineDep, render: RenderDep
) -> Response:
    """Render the dashboard page."""
    return render(request, "dashboard.html", await _dashboard_context(config, engine))
