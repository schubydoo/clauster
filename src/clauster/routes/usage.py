"""Usage and metrics dashboard routes, split from ``create_app`` (#1156).

These read-only badges roll up per-project cost/token usage from Claude's own
``.jsonl`` transcripts (invariant 5: read-only) and report the runner's cached
CPU/memory/disk samples. The two project-scoped routes validate the project name
for path-component safety before any disk read, and a transcript read failure
degrades to a defined 503 rather than a bare 500 that could leak an on-disk path.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from .. import usage
from ..dependencies import ConfigDep, RunnerDep
from ..discovery import is_valid_project_name

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/projects/{name}/usage")
async def api_project_usage(name: str, config: ConfigDep) -> dict:
    """Return the project's cost/token rollup, degrading to 503 if transcripts won't read."""
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
        raise HTTPException(status_code=503, detail="could not read usage transcripts") from exc
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


# ----- dashboard metrics: per-project and aggregate ------------------------------------------
@router.get("/api/projects/{name}/metrics")
async def api_project_metrics(name: str, config: ConfigDep, runner: RunnerDep) -> dict:
    """Return the cached CPU/memory/disk sample for this project's running bridge."""
    # Live CPU/memory/disk for a project's running bridge (dashboard badge). Served
    # from the server-side snapshot the runner's metrics task refreshes every
    # metrics.poll_seconds (#354): a cheap in-memory fold of the cache, with no
    # per-request thread or subprocess and no sampling cost. A project with no
    # current sample (off, just-started, or stopped) reports {running: false}.
    if not is_valid_project_name(name):
        raise HTTPException(status_code=422, detail="invalid project name")
    if not config.metrics.enabled:
        return {"running": False}
    sample = runner.metrics_snapshot(name)
    if sample is None:
        return {"running": False}
    return {"running": True, **sample}


@router.get("/api/metrics")
async def api_metrics_batch(config: ConfigDep, runner: RunnerDep) -> dict:
    """Return the cached metrics sample for every running bridge, keyed by instance_id."""
    # Batch counterpart to the per-project endpoint (#354): one in-memory read of
    # every running bridge's cached sample, so a dashboard can refresh all badges in
    # a single request instead of one per bridge.
    #
    # Keyed by instance_id, NOT project (#1090). Several bridges may share one project,
    # and the project-folded figure was presented on a single row as that bridge's own
    # usage — a Server-Mode row silently reporting its project's Interactive Sessions'
    # CPU/RAM too. The per-project total stays available on
    # /api/projects/{name}/metrics and in the Prometheus exposition.
    if not config.metrics.enabled:
        return {}
    return {
        iid: {"running": True, **sample}
        for iid, sample in runner.metrics_snapshots_by_instance().items()
    }
