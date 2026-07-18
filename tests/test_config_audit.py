"""Config-change audit trail (#958 Part 6): the config_audit module + route wiring.

Unit-tests :func:`clauster.config_audit.record` (schema, best-effort, redaction-neutral),
then drives representative config-write routes end to end and asserts the audit line lands
in ``<state_dir>/config_audit.log`` with the right surface/scope/target/action/keys.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from clauster import config_audit
from clauster.app import create_app
from clauster.config import load_config

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"
_ENABLED = "config_write:\n  enabled: true\n  allow_user_scope: true\n"


def _client(write_config, tmp_path: Path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ENABLED}")
    return TestClient(create_app(load_config(cfg)))


def _audit(tmp_path: Path) -> list[dict]:
    log = tmp_path / ".s" / "config_audit.log"
    return (
        [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )


# --- unit: config_audit.record ---------------------------------------------


def test_record_skips_when_state_dir_none() -> None:
    # Unit contexts pass state_dir=None -> silent no-op (must not raise, nothing written).
    config_audit.record(None, surface="x", scope="user", target="t", action="update")


def test_record_writes_core_and_optional_fields(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config_audit.record(
        state,
        surface="permissions",
        scope="project",
        target="/p/.claude/settings.json",
        action="update",
        actor="admin",
        keys=["allow", "deny"],
        extra={"note": "x"},
    )
    entry = json.loads((state / "config_audit.log").read_text(encoding="utf-8").strip())
    assert entry["surface"] == "permissions" and entry["scope"] == "project"
    assert entry["target"].endswith("settings.json") and entry["action"] == "update"
    assert entry["actor"] == "admin" and entry["keys"] == ["allow", "deny"]
    assert entry["note"] == "x" and "ts" in entry


def test_record_omits_keys_when_none(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config_audit.record(
        state, surface="claude-md", scope="user", target="/u/CLAUDE.md", action="update"
    )
    assert "keys" not in json.loads((state / "config_audit.log").read_text().strip())


def test_record_extra_never_shadows_core_schema(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config_audit.record(
        state,
        surface="s",
        scope="user",
        target="t",
        action="update",
        extra={"surface": "EVIL", "detail": "ok"},
    )
    entry = json.loads((state / "config_audit.log").read_text().strip())
    assert entry["surface"] == "s" and entry["detail"] == "ok"  # core field wins


def test_record_best_effort_on_oserror(tmp_path: Path, caplog) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "config_audit.log").mkdir()  # a directory -> open("a") raises OSError
    with caplog.at_level(logging.ERROR, logger="clauster.config_audit"):
        config_audit.record(state, surface="s", scope="user", target="t", action="update")
    assert any("audit append failed" in r.message for r in caplog.records)


def test_record_appends_one_line_per_call(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config_audit.record(state, surface="a", scope="user", target="t1", action="update")
    config_audit.record(state, surface="b", scope="user", target="t2", action="delete")
    lines = (state / "config_audit.log").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["action"] == "delete"


async def test_arecord_offloads_and_appends(tmp_path: Path) -> None:
    # The async entry point offloads to a thread (so a slow state dir can't stall the loop)
    # and forwards the same keyword fields to record().
    state = tmp_path / "state"
    await config_audit.arecord(
        state, surface="hooks", scope="project", target="/p", action="update", keys=["a"]
    )
    entry = json.loads((state / "config_audit.log").read_text().strip())
    assert entry["surface"] == "hooks" and entry["keys"] == ["a"]


# --- route wiring: representative surfaces ----------------------------------


def test_route_permissions_write_audits(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path) as c:
        r = c.put(
            "/api/config-write/permissions",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "permissions": {"allow": ["Bash(ls:*)"]},
            },
        )
        assert r.status_code == 200
    entry = next(e for e in _audit(tmp_path) if e["surface"] == "permissions")
    assert entry["scope"] == "project" and entry["action"] == "update"
    assert entry["actor"] == "admin" and entry["keys"] == ["allow"]
    assert entry["target"].endswith("alpha")


def test_route_settings_write_audits_keys_only(write_config, tmp_path, projects_root) -> None:
    # The audit records only the top-level KEY NAMES, never the (secret-bearing) values.
    with _client(write_config, tmp_path) as c:
        r = c.put(
            "/api/config-write/settings",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"env": {"SECRET_TOKEN": "sk-live-xxx"}, "model": "opus"},
            },
        )
        assert r.status_code == 200
    entry = next(e for e in _audit(tmp_path) if e["surface"] == "settings")
    assert entry["keys"] == ["env", "model"]
    assert "sk-live-xxx" not in json.dumps(entry)  # no secret value ever recorded


def test_route_subagent_put_then_delete_audits(write_config, tmp_path, projects_root) -> None:
    body = "---\nname: my-agent\ndescription: d\n---\nDo it.\n"
    with _client(write_config, tmp_path) as c:
        assert (
            c.put(
                "/api/config-write/subagents/my-agent",
                json={"scope": "project", "project": "alpha", "confirm": "alpha", "content": body},
            ).status_code
            == 200
        )
        assert (
            c.request(
                "DELETE",
                "/api/config-write/subagents/my-agent?scope=project&project=alpha&confirm=alpha",
            ).status_code
            == 200
        )
    sub = [e for e in _audit(tmp_path) if e["surface"] == "subagents"]
    assert [e["action"] for e in sub] == ["update", "delete"]
    assert all(e["target"] == "my-agent" for e in sub)


def test_route_subagent_delete_noop_records_not_removed(
    write_config, tmp_path, projects_root
) -> None:
    # Deleting an absent subagent is a no-op; the attempt is still recorded, but with
    # removed=false so the trail distinguishes a real deletion from a no-op.
    with _client(write_config, tmp_path) as c:
        r = c.request(
            "DELETE",
            "/api/config-write/subagents/ghost?scope=project&project=alpha&confirm=alpha",
        )
        assert r.status_code == 200 and r.json()["deleted"] is False
    entry = next(e for e in _audit(tmp_path) if e["surface"] == "subagents")
    assert entry["action"] == "delete" and entry["removed"] is False


def test_route_claude_md_write_audits(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path) as c:
        r = c.put(
            "/api/config-write/claude-md",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "content": "# managed\n",
            },
        )
        assert r.status_code == 200
    entry = next(e for e in _audit(tmp_path) if e["surface"] == "claude-md")
    assert entry["scope"] == "project" and entry["action"] == "update"
    assert entry["target"].endswith("alpha")
