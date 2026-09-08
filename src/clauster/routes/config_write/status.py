"""The config-write capability status route (#1156).

``GET /api/config-write/status`` is the foundation surface for the code-executing
config-write trust tier: it fail-closes to 404 when the capability is off, so a
disabled deployment exposes nothing, and otherwise reflects only the opt-in flags.
"""

from __future__ import annotations

from fastapi import APIRouter

from ... import config_write
from ...dependencies import ConfigDep

router = APIRouter()


@router.get("/api/config-write/status")
async def api_config_write_status(config: ConfigDep) -> dict:
    """Report the config-write opt-in flags, or 404 when the capability is off."""
    # Foundation surface for the code-executing config-write trust tier (#347/#687);
    # the concrete writers (#688-#691) sit behind this same gate. It fail-closes:
    # when config_write.enabled is off this 404s (the surface is invisible, same as
    # the reaper), so a disabled deployment exposes nothing. The body reflects only
    # the two opt-in flags, never any config content.
    config_write.require_capability(config, "project")
    return config_write.capability_status(config)
