"""Ops CLIs (spec §"v0.2"): doctor / backup / restore / migrate / install-service."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from clauster.config import load_config
from clauster.ops import (
    FAIL,
    OK,
    _version_ge,
    make_backup,
    migrate_state,
    render_service_unit,
    restore_backup,
    run_doctor,
)

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


# ----- _version_ge ------------------------------------------------------

@pytest.mark.parametrize("have,want,expected", [
    ("2.1.156", "2.1.145", True),
    ("2.1.145", "2.1.145", True),
    ("2.1.144", "2.1.145", False),
    ("2.2.0", "2.1.999", True),
    ("2.1", "2.1.0", True),       # missing patch treated as 0
    ("10.0.0", "9.9.9", True),    # numeric, not lexical
])
def test_version_ge(have, want, expected):
    assert _version_ge(have, want) is expected


# ----- doctor -----------------------------------------------------------

def _cfg_file(write_config, tmp_path, claude_extra: str = "") -> str:
    # Isolate state_dir under tmp (dot-prefixed so discovery never sees it as a project).
    extra = f"claude:\n  binary: {FAKE_CLAUDE}\n{claude_extra}state_dir: {tmp_path}/.cstate\n"
    return str(write_config(extra))


def test_doctor_all_ok(write_config, tmp_path):
    checks, ok = run_doctor(_cfg_file(write_config, tmp_path))
    assert ok is True
    by = {c.name: c for c in checks}
    assert by["claude"].status == OK and "2.1.156" in by["claude"].detail
    assert by["config"].status == OK


def test_doctor_missing_config_does_not_crash():
    checks, ok = run_doctor("/no/such/clauster.yml")
    assert ok is False
    assert checks[0].name == "config" and checks[0].status == FAIL


def test_doctor_old_claude_fails(write_config, tmp_path):
    checks, ok = run_doctor(_cfg_file(write_config, tmp_path, '  min_version: "9.9.9"\n'))
    by = {c.name: c for c in checks}
    assert by["claude"].status == FAIL and ok is False


# ----- backup / restore -------------------------------------------------

def _seed_state(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text('{"schema_version": 1, "instances": {}}')
    (state_dir / "claude_md_audit.log").write_text('{"project":"x"}\n')


def test_backup_restore_roundtrip(write_config, tmp_path):
    config = load_config(_cfg_file(write_config, tmp_path))
    _seed_state(config.state_dir)
    outdir = tmp_path / "out"
    outdir.mkdir()
    archive = make_backup(config, outdir)  # dir -> auto-named clauster-backup-<ts>.tar.gz
    assert archive.is_file() and archive.suffix == ".gz" and archive.parent == outdir

    dest = tmp_path / "restored-state"
    result = restore_backup(archive, state_dir=dest)
    assert (dest / "state.json").is_file()
    assert (dest / "claude_md_audit.log").is_file()
    assert result["state_files"] >= 2


def test_backup_includes_config(write_config, tmp_path):
    config = load_config(_cfg_file(write_config, tmp_path))
    _seed_state(config.state_dir)
    archive = make_backup(config, tmp_path / "out")
    cfg_out = tmp_path / "restored.yml"
    result = restore_backup(archive, state_dir=tmp_path / "st", config_out=cfg_out)
    assert cfg_out.is_file() and result["config"] == str(cfg_out)


def test_restore_refuses_nonempty_without_force(write_config, tmp_path):
    config = load_config(_cfg_file(write_config, tmp_path))
    _seed_state(config.state_dir)
    archive = make_backup(config, tmp_path / "out")
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "keepme").write_text("x")
    with pytest.raises(FileExistsError):
        restore_backup(archive, state_dir=dest)
    restore_backup(archive, state_dir=dest, force=True)  # force overwrites
    assert (dest / "state.json").is_file()


@pytest.mark.parametrize("evil", ["../evil.txt", "/etc/evil.txt"])
def test_restore_rejects_malicious_tar(tmp_path, evil):
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(evil)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError):
        restore_backup(bad, state_dir=tmp_path / "st")
    assert not (tmp_path / "evil.txt").exists()
    assert not Path("/etc/evil.txt").exists()


# ----- migrate ----------------------------------------------------------

def test_migrate_upgrades_old_schema(write_config, tmp_path):
    config = load_config(_cfg_file(write_config, tmp_path))
    config.state_dir.mkdir(parents=True, exist_ok=True)
    sj = config.state_dir / "state.json"
    sj.write_text(json.dumps({
        "schema_version": 0,
        "instances": {"alpha": {"label": "alpha", "intentional_stop": True, "spawn_mode": "same-dir"}},
    }))
    result = migrate_state(config)
    assert result["schema_version"] == 1
    assert json.loads(sj.read_text())["schema_version"] == 1
    assert (config.state_dir / "state.json.bak").is_file()  # migration backed up


# ----- install-service --------------------------------------------------

def test_service_systemd():
    unit = render_service_unit("systemd", python="/usr/bin/python3", config_path="/etc/clauster/clauster.yml", user="clauster")
    assert "[Service]" in unit and "ExecStart=/usr/bin/python3 -m clauster run" in unit
    assert "User=clauster" in unit and "Restart=on-failure" in unit


def test_service_launchd():
    unit = render_service_unit("launchd", python="/usr/bin/python3", config_path="/etc/clauster/clauster.yml")
    assert "<plist" in unit and "org.clauster.daemon" in unit and "RunAtLoad" in unit


def test_service_windows():
    unit = render_service_unit("windows", python="C:\\py\\python.exe", config_path="C:\\clauster.yml")
    assert "nssm install Clauster" in unit


def test_service_unknown_kind():
    with pytest.raises(ValueError):
        render_service_unit("upstart")
