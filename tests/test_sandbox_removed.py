"""The removed per-launch sandbox option: no spawn path can emit a sandbox flag.

Claude Code 2.1.300 removed ``--sandbox`` / ``--no-sandbox`` from ``claude remote-control``,
and passing either now makes the bridge exit at once. Clauster's toggle (#780, disabled by
#1037) is gone with them. These tests pin both halves of that:

* No argv builder or spawn entry point takes a sandbox input at all, and every builder's
  output over its whole input domain carries neither flag (standard, pty, hosted).
* An old caller or record that still carries a sandbox value keeps working: the API spawn
  and resume, the pty spawn and resume, and a persisted row whose ``sandbox_mode`` column
  says "on" all load and launch with no flag.

The MCP and CLI entry points are covered next to their own suites
(``test_mcp_server.py``, ``test_cli_write.py``), the hosted route in ``test_app_hosted.py``,
and the store's treatment of the old column in ``test_db.py``.
"""

from __future__ import annotations

import inspect
import itertools
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from clauster.app import create_app
from clauster.bridge_launch import BridgeLaunch
from clauster.config import PERMISSION_MODES, SANDBOX_REMOVED_NOTE, SPAWN_MODES
from clauster.engine import ClausterEngine
from clauster.hosted import HostedManager, build_hosted_argv
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner
from clauster.spawn_coordinator import SpawnCoordinator

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="pty mode is POSIX-only")

# Every value an old client could have sent, plus off-type ones: none may be refused.
_LEGACY_VALUES = ["on", "off", "default", "bogus", 5, True, {"enabled": True}]


def _has_sandbox_flag(argv: list[str]) -> bool:
    """Return True when any argv token is, or starts with, a sandbox flag."""
    return any(tok.startswith(("--sandbox", "--no-sandbox")) for tok in argv)


def _argv(inst_or_body) -> list[str]:
    """Read the argv the fake ``claude`` recorded beside the bridge's debug log."""
    path = (
        inst_or_body["bridge_debug_log_path"]
        if isinstance(inst_or_body, dict)
        else inst_or_body.bridge_debug_log_path
    )
    return json.loads(Path(str(path) + ".argv.json").read_text())


def test_the_sandbox_flag_detector_sees_a_flag():
    # Positive control for the detector every test below relies on.
    assert _has_sandbox_flag(["claude", "remote-control", "--sandbox"])
    assert _has_sandbox_flag(["claude", "--no-sandbox"])
    assert _has_sandbox_flag(["claude", "--sandbox=on"])
    assert not _has_sandbox_flag(["claude", "remote-control", "--name", "sandbox"])


@pytest.mark.parametrize(
    "fn",
    [
        BridgeLaunch._build_cmd,
        BridgeLaunch._popen,
        BridgeLaunch._build_pty_bridge_argv,
        build_hosted_argv,
        HostedManager.spawn,
        SessionRunner.spawn,
        SessionRunner.spawn_detailed,
        SessionRunner._spawn_locked,
        SessionRunner._build_cmd,
        SessionRunner._popen,
        SessionRunner._validate_spawn_options,
        SpawnCoordinator.spawn,
        SpawnCoordinator.spawn_detailed,
        SpawnCoordinator._spawn_locked,
        ClausterEngine.start,
    ],
    ids=lambda fn: fn.__qualname__,
)
def test_no_spawn_entry_point_takes_a_sandbox_input(fn):
    # Structural: with no parameter to carry it, no request value can reach an argv.
    assert not [p for p in inspect.signature(fn).parameters if "sandbox" in p]


def test_the_instance_model_has_no_sandbox_field():
    assert not [f for f in RemoteControlInstance.model_fields if "sandbox" in f]


def test_standard_argv_never_carries_a_sandbox_flag(runner_config):
    # Every spawn mode x permission mode, with the config-driven flags both off and on.
    config, claude_json = runner_config
    for verbose, prefix in itertools.product((False, True), (None, "acme")):
        config.instance_defaults.verbose = verbose
        config.instance_defaults.session_name_prefix = prefix
        launch = SessionRunner(config, claude_json=claude_json)._launch
        for spawn_mode, permission_mode in itertools.product(SPAWN_MODES, PERMISSION_MODES):
            argv = launch._build_cmd(Path("x.log"), "alpha", spawn_mode, permission_mode)
            assert argv[1] == "remote-control"
            assert not _has_sandbox_flag(argv), argv


def test_pty_argv_never_carries_a_sandbox_flag(runner_config):
    runner = SessionRunner(runner_config[0], claude_json=runner_config[1])
    uuid = "abcdefab-1234-5678-9abc-def012345678"
    for permission_mode, resume, session_id, worktree in itertools.product(
        PERMISSION_MODES, (False, True), (None, uuid), (None, "wt-1")
    ):
        argv = runner._build_pty_bridge_argv(
            Path("x.log"),
            "alpha",
            permission_mode,
            resume=resume,
            resume_session_id=session_id,
            worktree_name=worktree,
        )
        assert "--remote-control" in argv
        assert not _has_sandbox_flag(argv), argv


def test_hosted_argv_never_carries_a_sandbox_flag():
    uuid = "abcdefab-1234-5678-9abc-def012345678"
    for permission_mode, resume_uuid in itertools.product(PERMISSION_MODES, (None, uuid)):
        argv = build_hosted_argv(
            "/usr/bin/claude", permission_mode=permission_mode, resume_uuid=resume_uuid
        )
        assert not _has_sandbox_flag(argv), argv


def _runner_client(runner_config) -> TestClient:
    config, claude_json = runner_config
    return TestClient(create_app(config, runner=SessionRunner(config, claude_json=claude_json)))


@pytest.mark.parametrize("value", _LEGACY_VALUES, ids=repr)
def test_api_standard_spawn_and_resume_ignore_a_legacy_sandbox_value(
    runner_config, monkeypatch, caplog, value
):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _runner_client(runner_config) as client:
        with caplog.at_level("WARNING", logger="clauster.routes.instances"):
            resp = client.post("/api/instances", json={"project": "alpha", "sandbox": value})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "sandbox_mode" not in body
        assert SANDBOX_REMOVED_NOTE in body["warnings"]
        assert SANDBOX_REMOVED_NOTE in caplog.text
        assert body["resume_mode"] == "standard"
        assert not _has_sandbox_flag(_argv(body))

        iid = body["instance_id"]
        assert client.delete(f"/api/instances/{iid}").status_code == 200
        resumed = client.post(f"/api/instances/{iid}/resume")
        assert resumed.status_code in (200, 201), resumed.text
        assert not _has_sandbox_flag(_argv(resumed.json()))
        client.delete(f"/api/instances/{iid}")


def test_api_spawn_without_sandbox_adds_no_note(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    with _runner_client(runner_config) as client:
        resp = client.post("/api/instances", json={"project": "alpha"})
        assert resp.status_code == 201, resp.text
        assert SANDBOX_REMOVED_NOTE not in resp.json()["warnings"]
        client.delete(f"/api/instances/{resp.json()['instance_id']}")


@_POSIX_ONLY
@pytest.mark.parametrize("value", ["on", "off"])
def test_api_pty_spawn_and_resume_ignore_a_legacy_sandbox_value(runner_config, value):
    with _runner_client(runner_config) as client:
        resp = client.post(
            "/api/instances", json={"project": "alpha", "resume_mode": "pty", "sandbox": value}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["resume_mode"] == "pty"
        assert SANDBOX_REMOVED_NOTE in body["warnings"]
        assert not _has_sandbox_flag(_argv(body))

        iid = body["instance_id"]
        assert client.delete(f"/api/instances/{iid}").status_code == 200
        resumed = client.post(f"/api/instances/{iid}/resume")
        assert resumed.status_code in (200, 201), resumed.text
        argv = _argv(resumed.json())
        assert "--continue" in argv
        assert not _has_sandbox_flag(argv)
        client.delete(f"/api/instances/{iid}")


async def test_an_old_row_with_sandbox_on_loads_and_resumes_without_a_flag(
    runner_config, monkeypatch
):
    # A row an older build wrote with the toggle's "on" in the `sandbox_mode` column. It must
    # load into a STOPPED card (never a refused load), resume with no flag, and keep the
    # column as it was, so a downgrade to that older build reads back exactly what it wrote.
    config, claude_json = runner_config
    seed = SessionRunner(config, claude_json=claude_json)
    seed.persistence.state_store().save(
        {"iid-old": {"project_name": "alpha", "label": "alpha", "resume_mode": "standard"}}
    )
    with seed.persistence._engine.begin() as conn:
        conn.execute(text("UPDATE instances SET sandbox_mode = 'on'"))
    await seed.shutdown()

    restarted = SessionRunner(config, claude_json=claude_json)
    await restarted.rediscover()  # persists by default
    card = restarted.get_instance("iid-old")
    assert card is not None
    assert card.status is InstanceStatus.STOPPED

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    resumed = await restarted.resume("iid-old")
    try:
        assert not _has_sandbox_flag(_argv(resumed))
    finally:
        await restarted.stop(resumed.instance_id)
    with restarted.persistence._engine.connect() as conn:
        assert conn.execute(text("SELECT sandbox_mode FROM instances")).scalar_one() == "on"
    await restarted.shutdown()


def test_a_legacy_record_dict_with_sandbox_mode_rebuilds_a_card(runner_config):
    # A record shaped by a pre-removal build (the key still in the dict) rebuilds normally.
    runner = SessionRunner(runner_config[0], claude_json=runner_config[1])
    runner._persisted = {
        "iid-1": {"project_name": "alpha", "label": "alpha", "sandbox_mode": "off"},
    }
    inst = runner._stopped_from_persisted("alpha")
    assert inst is not None
    assert inst.status is InstanceStatus.STOPPED
    assert "sandbox_mode" not in inst.model_dump()
