"""Runner wiring for the session-event history (#363).

The runner records a ``spawned`` / ``ready`` / ``ended`` / ``crashed`` row at the
single ``_emit_lifecycle`` chokepoint (alongside the webhook). These tests assert:
the right kind per event, the mode (hosted vs the standard/pty resume axis), the
hashed ``session_ref`` (never a raw bearer id), the terminal-row cost snapshot
sourced from the on-disk transcripts, and the best-effort posture — a history
store error never escapes into the lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.pointers import sanitize_cwd
from clauster.runner import SessionRunner


def _runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


async def _drain(runner: SessionRunner) -> None:
    """Await the background history/webhook tasks the chokepoint spawned."""
    await asyncio.gather(*runner._notify_tasks)


def _write_transcript(claude_projects_dir, project_path, *, model="claude-opus-4-7", in_tok=1000):
    """Write a one-message transcript JSONL under Claude's per-cwd dir for a project."""
    tdir = claude_projects_dir / sanitize_cwd(project_path)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "sess.jsonl").write_text(
        json.dumps(
            {
                "message": {
                    "model": model,
                    "usage": {"input_tokens": in_tok, "output_tokens": 200},
                }
            }
        )
        + "\n"
    )


async def test_spawn_records_spawned_row(runner_config):
    runner = _runner(runner_config)
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STARTING, resume_mode="standard"
    )
    runner._record_event("spawn", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    assert event.kind == "spawned"
    assert event.mode == "standard"
    assert event.cost_usd is None  # non-terminal -> no cost


async def test_ready_records_ready_row_with_pty_mode(runner_config):
    runner = _runner(runner_config)
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.RUNNING, resume_mode="pty"
    )
    runner._record_event("ready", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    assert event.kind == "ready"
    assert event.mode == "pty"


async def test_hosted_channel_records_hosted_mode(runner_config):
    runner = _runner(runner_config)
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STARTING, channel="hosted"
    )
    runner._record_event("spawn", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    assert event.mode == "hosted"


async def test_stop_records_ended_row_with_cost_snapshot(runner_config):
    config, _ = runner_config
    runner = _runner(runner_config)
    # A transcript exists for alpha, so the terminal row snapshots its cumulative cost.
    _write_transcript(runner._claude_projects_dir, config.projects_root / "alpha")
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="standard"
    )
    runner._record_event("stop", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    assert event.kind == "ended"
    assert event.cost_usd is not None and event.cost_usd > 0
    assert event.input_tokens == 1000  # summed from the transcript

    rollup = runner._history.rollup_for("alpha")
    assert rollup.total_cost_usd == event.cost_usd
    assert rollup.last_used is not None


async def test_crash_records_crashed_row(runner_config):
    config, _ = runner_config
    runner = _runner(runner_config)
    _write_transcript(runner._claude_projects_dir, config.projects_root / "alpha")
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.CRASHED, resume_mode="standard"
    )
    runner._record_event("crash", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    assert event.kind == "crashed"
    assert event.cost_usd is not None  # crash is terminal -> snapshot taken


async def test_terminal_event_without_transcripts_records_zero_cost(runner_config):
    # No transcript for the project: the snapshot legitimately finds zero usage, so the
    # terminal row records $0.00 / 0 tokens (a truthful "no measurable cost"), not a
    # null. A null cost is reserved for the "couldn't read the data at all" OSError path.
    runner = _runner(runner_config)
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="pty"
    )
    runner._record_event("stop", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    assert event.kind == "ended"
    assert event.cost_usd == 0.0
    assert event.input_tokens == 0


async def test_terminal_event_records_null_cost_when_transcript_read_fails(
    runner_config, monkeypatch
):
    # The snapshot raises OSError (an unreadable transcript dir): the terminal row is
    # still recorded — with a null cost — rather than dropped entirely.
    runner = _runner(runner_config)

    def _boom(*_args, **_kwargs):
        raise OSError("transcripts unreadable")

    monkeypatch.setattr("clauster.runner.aggregate_project_usage_cached", _boom)
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="standard"
    )
    runner._record_event("stop", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    assert event.kind == "ended"
    assert event.cost_usd is None
    assert event.input_tokens is None


async def test_record_event_hashes_session_ref_never_raw(runner_config):
    runner = _runner(runner_config)
    sid = "session_01TESTSTARTERAAAAAAAAAA"
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STARTING, resume_mode="standard"
    )
    inst.starter_session_id = sid
    runner._record_event("spawn", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    expected = hmac.new(
        runner._session_ref_key(), sid.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]
    assert event.session_ref == expected
    assert event.session_ref != sid  # the raw bearer id is never persisted


async def test_unknown_event_records_nothing(runner_config):
    runner = _runner(runner_config)
    inst = RemoteControlInstance(project="alpha", label="alpha")
    runner._record_event("bogus", inst)
    assert not runner._notify_tasks  # gated out before creating a task
    assert runner._history.history_for("alpha") == []


async def test_record_event_async_swallows_store_failure(runner_config, caplog):
    # Best-effort: an append that raises inside the background task must be swallowed and
    # logged (not left as asyncio's "Task exception was never retrieved"), so the drain
    # completes cleanly and the lifecycle is never affected.
    runner = _runner(runner_config)

    def _boom(**_kwargs):
        raise RuntimeError("db gone")

    runner._history.append = _boom  # type: ignore[method-assign]
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="standard"
    )
    runner._record_event("stop", inst)
    results = await asyncio.gather(*runner._notify_tasks, return_exceptions=True)
    assert not any(isinstance(r, Exception) for r in results)  # no leaked task exception
    assert "could not record session event" in caplog.text


async def test_terminal_event_records_row_when_project_path_lookup_fails(
    runner_config, caplog, monkeypatch
):
    # _project_path walks the filesystem and can raise OSError (a discovery re-walk on a
    # vanished projects_root). That must degrade only the COST to null — the terminal row
    # is still written, per the "an unreadable transcript must not drop the row" invariant.
    runner = _runner(runner_config)

    def _boom(_name):
        raise OSError("projects_root vanished")

    monkeypatch.setattr(runner, "_project_path", _boom)
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.CRASHED, resume_mode="pty"
    )
    runner._record_event("crash", inst)  # must not raise
    await _drain(runner)

    [event] = runner._history.history_for("alpha")  # the row IS recorded
    assert event.kind == "crashed"
    assert event.cost_usd is None  # only the cost is dropped
    assert "session-history cost snapshot failed" in caplog.text


async def test_terminal_event_records_row_when_cost_snapshot_raises_non_oserror(
    runner_config, caplog, monkeypatch
):
    # The cost snapshot can raise a non-OSError (e.g. a transcript-parse ValueError). That
    # must still degrade only the COST to null — the terminal row is recorded regardless.
    runner = _runner(runner_config)

    def _boom(*_args, **_kwargs):
        raise ValueError("malformed transcript")

    monkeypatch.setattr("clauster.runner.aggregate_project_usage_cached", _boom)
    config, _ = runner_config
    _write_transcript(runner._claude_projects_dir, config.projects_root / "alpha")
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="standard"
    )
    runner._record_event("stop", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")  # the row IS recorded
    assert event.kind == "ended"
    assert event.cost_usd is None  # only the cost is dropped
    assert "session-history cost snapshot failed" in caplog.text


async def test_unresolved_resume_mode_falls_back_to_standard(runner_config):
    # mode is NOT NULL; a None resume_mode would make the INSERT drop the row. The
    # prologue falls back to "standard" so the row is always recorded with a valid mode.
    runner = _runner(runner_config)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.STARTING)
    inst.resume_mode = None  # type: ignore[assignment]  # simulate an unresolved axis
    runner._record_event("spawn", inst)
    await _drain(runner)

    [event] = runner._history.history_for("alpha")
    assert event.mode == "standard"


async def test_emit_lifecycle_records_and_webhooks_together(runner_config):
    # The single chokepoint fires both sinks. With webhooks off (default), history is
    # still recorded — the two are independent.
    runner = _runner(runner_config)
    assert runner._webhooks.active is False  # default config: webhooks disabled
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STARTING, resume_mode="standard"
    )
    runner._emit_lifecycle("spawn", inst)
    await _drain(runner)
    [event] = runner._history.history_for("alpha")
    assert event.kind == "spawned"


# ----- synchronous (no running loop) fallback path ------------------------
# These are plain (non-async) tests so there is no running event loop, exercising
# the inline-append branch the synchronous status-apply call path relies on.


def test_record_event_sync_path_appends_inline(runner_config):
    config, _ = runner_config
    runner = _runner(runner_config)
    _write_transcript(runner._claude_projects_dir, config.projects_root / "alpha")
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="pty"
    )
    runner._record_event("stop", inst)  # no event loop -> inline append
    assert not runner._notify_tasks  # never scheduled a task
    [event] = runner._history.history_for("alpha")
    assert event.kind == "ended"
    assert event.cost_usd is not None and event.cost_usd > 0


def test_record_event_sync_path_swallows_store_failure(runner_config, caplog):
    # The inline path must also be best-effort: an append that raises is logged and
    # swallowed, never surfaced into the (synchronous) lifecycle caller.
    runner = _runner(runner_config)

    def _boom(**_kwargs):
        raise RuntimeError("db gone")

    runner._history.append = _boom  # type: ignore[method-assign]
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STARTING, resume_mode="standard"
    )
    runner._record_event("spawn", inst)  # must not raise
    assert "could not record session event" in caplog.text
