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
    WARN,
    _check_auth,
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


def test_doctor_invalid_config_fails(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text(f"projects_root: {tmp_path}/does-not-exist\n")  # fails validation -> ValueError
    checks, ok = run_doctor(str(bad))
    assert ok is False and checks[0].name == "config" and checks[0].status == FAIL


def test_doctor_claude_not_found(write_config, tmp_path):
    cfg = str(write_config(f"claude:\n  binary: no-such-claude-bin\nstate_dir: {tmp_path}/.s\n"))
    by = {c.name: c for c in run_doctor(cfg)[0]}
    assert by["claude"].status == FAIL


def test_doctor_probe_exception(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "clauster.ops.claude_cli.claude_version",
        lambda b: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    by = {c.name: c for c in run_doctor(_cfg_file(write_config, tmp_path))[0]}
    assert by["claude"].status == FAIL and "boom" in by["claude"].detail


def test_doctor_git_missing_warns(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr("clauster.ops.shutil.which", lambda n: None)
    by = {c.name: c for c in run_doctor(_cfg_file(write_config, tmp_path))[0]}
    assert by["git"].status == WARN


def test_doctor_state_dir_not_writable_fails(write_config, tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x")  # state_dir points at a file -> mkdir fails
    cfg = str(write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {blocker}\n"))
    by = {c.name: c for c in run_doctor(cfg)[0]}
    assert by["state_dir"].status == FAIL


def test_doctor_port_in_use_warns(write_config, tmp_path):
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        cfg = str(write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\nport: {port}\n"))
        by = {c.name: c for c in run_doctor(cfg)[0]}
        assert by["port"].status == WARN and str(port) in by["port"].detail
    finally:
        srv.close()


def test_check_auth_branches(write_config, tmp_path):
    config = load_config(_cfg_file(write_config, tmp_path))
    assert _check_auth(config).status == OK
    # password_required but no hash -> FAIL (mutate past the config validator)
    config.auth.password_required = True
    config.auth.password_hash = None
    assert _check_auth(config).status == FAIL
    # non-loopback with no auth -> FAIL; with explicit opt-out -> WARN
    c2 = load_config(_cfg_file(write_config, tmp_path))
    c2.host = "0.0.0.0"
    assert _check_auth(c2).status == FAIL
    c2.auth.allow_unauthenticated_network = True
    assert _check_auth(c2).status == WARN


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


def test_restore_recreates_subdirs(write_config, tmp_path):
    config = load_config(_cfg_file(write_config, tmp_path))
    sd = config.state_dir
    (sd / "sub").mkdir(parents=True, exist_ok=True)
    (sd / "sub" / "nested.json").write_text("{}")
    outdir = tmp_path / "o"
    outdir.mkdir()
    archive = make_backup(config, outdir)
    dest = tmp_path / "restored"
    restore_backup(archive, state_dir=dest)
    assert (dest / "sub" / "nested.json").is_file()  # directory member rebuilt


def test_restore_config_out_conflict_without_force(write_config, tmp_path):
    config = load_config(_cfg_file(write_config, tmp_path))
    _seed_state(config.state_dir)
    outdir = tmp_path / "o"
    outdir.mkdir()
    archive = make_backup(config, outdir)
    existing = tmp_path / "existing.yml"
    existing.write_text("keep me")
    with pytest.raises(FileExistsError):
        restore_backup(archive, state_dir=tmp_path / "st", config_out=existing)


def test_restore_skips_link_members(tmp_path):
    import io
    arch = tmp_path / "s.tar.gz"
    with tarfile.open(arch, "w:gz") as tar:
        data = b"hi"
        f = tarfile.TarInfo("state/ok.txt")
        f.size = len(data)
        tar.addfile(f, io.BytesIO(data))
        link = tarfile.TarInfo("state/evil-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    res = restore_backup(arch, state_dir=tmp_path / "out")
    assert (tmp_path / "out" / "ok.txt").is_file()
    assert not (tmp_path / "out" / "evil-link").exists()  # link member dropped
    assert res["state_files"] == 1


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
