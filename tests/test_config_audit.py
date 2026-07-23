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


# --- unit: audit-log rotation (#1011) --------------------------------------


def _target_of(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8").strip())["target"]


def test_record_does_not_rotate_under_ceiling(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config_audit.record(state, surface="x", scope="user", target="a", action="create")
    config_audit.record(state, surface="x", scope="user", target="b", action="update")
    assert not (state / "config_audit.log.1").exists()  # well under the default 5 MB ceiling
    assert len((state / "config_audit.log").read_text().strip().splitlines()) == 2


def test_record_rotates_when_over_ceiling(tmp_path: Path, monkeypatch) -> None:
    # A tiny ceiling makes any existing content trigger rotation on the next append (#1011).
    monkeypatch.setattr(config_audit, "_AUDIT_MAX_BYTES", 1)
    state = tmp_path / "state"
    config_audit.record(state, surface="x", scope="user", target="a", action="create")
    config_audit.record(state, surface="x", scope="user", target="b", action="update")
    assert _target_of(state / "config_audit.log.1") == "a"  # prior content rotated out
    assert _target_of(state / "config_audit.log") == "b"  # fresh log has the newest record


def test_record_rotation_shifts_and_drops_beyond_keep(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config_audit, "_AUDIT_MAX_BYTES", 1)
    monkeypatch.setattr(config_audit, "_AUDIT_KEEP_ROTATED", 2)
    state = tmp_path / "state"
    for t in ("r1", "r2", "r3", "r4"):
        config_audit.record(state, surface="x", scope="user", target=t, action="update")
    assert _target_of(state / "config_audit.log") == "r4"
    assert _target_of(state / "config_audit.log.1") == "r3"
    assert _target_of(state / "config_audit.log.2") == "r2"
    assert not (state / "config_audit.log.3").exists()  # keep=2 bound; r1 dropped


def test_record_rotation_error_is_swallowed(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(config_audit, "_AUDIT_MAX_BYTES", 1)
    state = tmp_path / "state"
    config_audit.record(state, surface="x", scope="user", target="r1", action="create")

    def _boom(self, target):
        raise OSError("boom")

    monkeypatch.setattr(config_audit.Path, "replace", _boom)  # rotation rename fails
    with caplog.at_level(logging.WARNING, logger="clauster.config_audit"):
        config_audit.record(state, surface="x", scope="user", target="r2", action="update")
    assert any("rotation failed" in r.message for r in caplog.records)
    # rotation failed, so the append continued against the existing file — nothing lost.
    lines = (state / "config_audit.log").read_text().strip().splitlines()
    assert [json.loads(line)["target"] for line in lines] == ["r1", "r2"]


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


# --- unit: file fingerprints (#958 Part 6 CLI side-effect visibility) --------


def test_file_fingerprints_hashes_present_and_none_for_absent(tmp_path: Path) -> None:
    present = tmp_path / "a.json"
    present.write_bytes(b'{"x": 1}')
    fp = config_audit.file_fingerprints([present, tmp_path / "gone.json"])
    assert fp[str(present)] == {"sha256": fp[str(present)]["sha256"], "bytes": 8}
    assert len(fp[str(present)]["sha256"]) == 64
    assert fp[str(tmp_path / "gone.json")] is None  # absent -> None (a create is detectable)


def test_diff_fingerprints_created_and_omits_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "f.json"
    before = {str(p): None, "/stable": {"sha256": "s", "bytes": 1}}
    p.write_bytes(b"hi")
    after = config_audit.file_fingerprints([p])
    after["/stable"] = {"sha256": "s", "bytes": 1}  # unchanged -> must be omitted
    changes = config_audit.diff_fingerprints(before, after)
    assert len(changes) == 1
    c = changes[0]
    assert c["file"] == str(p) and c["change"] == "created"
    assert "before_sha256" not in c and c["after_bytes"] == 2 and len(c["after_sha256"]) == 64


def test_diff_fingerprints_modified_and_removed() -> None:
    before = {"/m": {"sha256": "old", "bytes": 3}, "/r": {"sha256": "x", "bytes": 1}}
    after = {"/m": {"sha256": "new", "bytes": 5}, "/r": None}
    changes = {c["file"]: c for c in config_audit.diff_fingerprints(before, after)}
    assert changes["/m"]["change"] == "modified"
    assert changes["/m"]["before_sha256"] == "old" and changes["/m"]["after_sha256"] == "new"
    assert changes["/r"]["change"] == "removed"
    assert changes["/r"]["before_sha256"] == "x" and "after_sha256" not in changes["/r"]


def test_file_fingerprints_unreadable_is_indeterminate(tmp_path: Path, monkeypatch) -> None:
    # A file that EXISTS but can't be read must be distinct from absent (None), so a diff
    # never miscalls it a create/remove — and file_fingerprints must not raise on the
    # critical path of a committed write.
    p = tmp_path / "locked.json"
    p.write_bytes(b"x")
    real = Path.read_bytes

    def boom(self):
        if self == p:
            raise PermissionError("nope")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    fp = config_audit.file_fingerprints([p])
    assert fp[str(p)] == {"unreadable": True}
    changes = config_audit.diff_fingerprints({str(p): {"sha256": "old", "bytes": 1}}, fp)
    assert changes == [{"file": str(p), "change": "indeterminate"}]


# --- route wiring: representative surfaces ----------------------------------


def test_route_mcp_write_audits_changed_file_fingerprint(
    write_config, tmp_path, projects_root
) -> None:
    # An entry with an env value routes to the DIRECT writer (no subprocess) and writes
    # <project>/.mcp.json; the audit records WHICH file changed (path + sha256 + size),
    # never its contents — the env value below must not appear anywhere in the record.
    with _client(write_config, tmp_path) as c:
        r = c.post(
            "/api/config-write/mcp/server",
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "op": "add",
                "name": "srv",
                "entry": {"command": "x", "env": {"K": "SECRET-marker-9f3"}},
            },
        )
        assert r.status_code == 200
    entry = next(e for e in _audit(tmp_path) if e["surface"] == "mcp")
    changed = [f for f in entry["files"] if f["file"].endswith(".mcp.json")]
    assert changed, f"expected .mcp.json in audited files, got {entry['files']}"
    assert changed[0]["change"] in ("created", "modified")
    assert len(changed[0]["after_sha256"]) == 64 and changed[0]["after_bytes"] > 0
    assert "SECRET-marker-9f3" not in json.dumps(entry)  # fingerprint only, never contents


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
