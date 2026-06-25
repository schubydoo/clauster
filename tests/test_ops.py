"""Ops CLIs (spec §"v0.2"): doctor / backup / restore / migrate / install-service."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from clauster.config import load_config
from clauster.ops import (
    FAIL,
    OK,
    WARN,
    _check_auth,
    _check_claude_login,
    _check_repo_freshness,
    _check_systemd_killmode,
    _version_ge,
    make_backup,
    migrate_state,
    project_preflight_checks,
    render_service_unit,
    restore_backup,
    run_doctor,
)

# .cmd on Windows so the version probe resolves on Python 3.11 too (3.12+ would
# find the sibling claude.cmd via PATHEXT, but 3.11 won't — be explicit).
_WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""
FAKE_CLAUDE = (
    Path(__file__).resolve().parent / "fixtures" / "fake_claude" / f"claude{_WIN_STUB_SUFFIX}"
)


# ----- _version_ge ------------------------------------------------------


@pytest.mark.parametrize(
    "have,want,expected",
    [
        ("2.1.156", "2.1.145", True),
        ("2.1.145", "2.1.145", True),
        ("2.1.144", "2.1.145", False),
        ("2.2.0", "2.1.999", True),
        ("2.1", "2.1.0", True),  # missing patch treated as 0
        ("10.0.0", "9.9.9", True),  # numeric, not lexical
    ],
)
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


def test_repo_freshness_none_for_non_git_install(tmp_path):
    # A PyPI/Docker install (no .git) reports nothing — there's no in-place upgrade.
    assert _check_repo_freshness(tmp_path) is None


def _fake_git(monkeypatch, *, returncode=0, stdout=""):
    import subprocess as sp

    def fake_run(*a, **k):
        return sp.CompletedProcess(a[0] if a else [], returncode, stdout=stdout, stderr="")

    monkeypatch.setattr("clauster.ops.subprocess.run", fake_run)


def test_repo_freshness_behind_warns(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _fake_git(monkeypatch, returncode=0, stdout="3\t0\n")  # 3 behind, 0 ahead
    c = _check_repo_freshness(tmp_path)
    assert c is not None and c.status == WARN and "3 commits behind" in c.detail


def test_repo_freshness_up_to_date_ok(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _fake_git(monkeypatch, returncode=0, stdout="0\t2\n")  # 0 behind, 2 local ahead
    c = _check_repo_freshness(tmp_path)
    assert c is not None and c.status == OK and "+2 local" in c.detail


def test_repo_freshness_no_upstream_ok(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _fake_git(monkeypatch, returncode=128, stdout="")  # @{upstream} unresolvable
    c = _check_repo_freshness(tmp_path)
    assert c is not None and c.status == OK and "no upstream" in c.detail


# ----- _check_systemd_killmode ------------------------------------------


def _fake_systemctl(monkeypatch, *, present=True, returncode=0, stdout=""):
    import subprocess as sp

    monkeypatch.setattr(
        "clauster.ops.shutil.which", lambda n: "/bin/systemctl" if present else None
    )

    def fake_run(*a, **k):
        return sp.CompletedProcess(a[0] if a else [], returncode, stdout=stdout, stderr="")

    monkeypatch.setattr("clauster.ops.subprocess.run", fake_run)


def test_killmode_no_systemctl_returns_none(monkeypatch):
    _fake_systemctl(monkeypatch, present=False)
    assert _check_systemd_killmode() is None


def test_killmode_unit_not_loaded_returns_none(monkeypatch):
    _fake_systemctl(monkeypatch, stdout="LoadState=not-found\nKillMode=control-group\n")
    assert _check_systemd_killmode() is None


def test_killmode_query_failure_returns_none(monkeypatch):
    _fake_systemctl(monkeypatch, returncode=1, stdout="")
    assert _check_systemd_killmode() is None


def test_killmode_process_ok(monkeypatch):
    _fake_systemctl(monkeypatch, stdout="LoadState=loaded\nKillMode=process\n")
    c = _check_systemd_killmode()
    assert c is not None and c.status == OK and "process" in c.detail


def test_killmode_none_ok(monkeypatch):
    _fake_systemctl(monkeypatch, stdout="LoadState=loaded\nKillMode=none\n")
    c = _check_systemd_killmode()
    assert c is not None and c.status == OK


def test_killmode_control_group_warns(monkeypatch):
    _fake_systemctl(monkeypatch, stdout="LoadState=loaded\nKillMode=control-group\n")
    c = _check_systemd_killmode()
    assert c is not None and c.status == WARN
    assert "KillMode=process" in c.detail and "pty" in c.detail
    # The remediation must be actionable: a concrete install + reload command.
    assert "install-service systemd --write" in c.detail
    assert "systemctl daemon-reload" in c.detail


def test_killmode_subprocess_error_returns_none(monkeypatch):
    monkeypatch.setattr("clauster.ops.shutil.which", lambda n: "/bin/systemctl")

    def boom(*a, **k):
        raise OSError("no systemd")

    monkeypatch.setattr("clauster.ops.subprocess.run", boom)
    assert _check_systemd_killmode() is None


def test_doctor_state_dir_not_writable_fails(write_config, tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x")  # state_dir points at a file -> probe fails
    cfg = str(write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {blocker}\n"))
    by = {c.name: c for c in run_doctor(cfg)[0]}
    assert by["state_dir"].status == FAIL


def test_doctor_absent_state_dir_ok_without_creating(write_config, tmp_path):
    sd = tmp_path / "willcreate"  # absent
    cfg = str(write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {sd}\n"))
    by = {c.name: c for c in run_doctor(cfg)[0]}
    assert by["state_dir"].status == OK
    assert not sd.exists()  # read-only diagnostic must NOT create the tree


def test_doctor_port_in_use_warns(write_config, tmp_path):
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        cfg = str(
            write_config(
                f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\nport: {port}\n"
            )
        )
        by = {c.name: c for c in run_doctor(cfg)[0]}
        assert by["port"].status == WARN and str(port) in by["port"].detail
        # check_port=False (the running server's dashboard preflight): the port is held
        # by that server, so the probe is a guaranteed false positive — omit it entirely.
        names = {c.name for c in run_doctor(cfg, check_port=False)[0]}
        assert "port" not in names
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


# ----- project_preflight_checks -----------------------------------------


def _preflight(project):
    return {c.name: c for c in project_preflight_checks(project)}


def test_project_preflight_trusted_git_all_ok():
    from clauster.models import Project, TrustState

    proj = Project(
        name="alpha",
        path=Path("/p/alpha"),
        is_git_repo=True,
        trust_state=TrustState.TRUSTED,
    )
    checks = _preflight(proj)
    assert checks["trust"].status == OK
    assert checks["git"].status == OK
    # advisory only: trust/git are never FAIL, so a preflight never hard-blocks
    assert all(c.status != FAIL for c in checks.values())


def test_project_preflight_untrusted_warns():
    from clauster.models import Project, TrustState

    proj = Project(name="alpha", path=Path("/p/alpha"), trust_state=TrustState.UNTRUSTED)
    check = _preflight(proj)["trust"]
    assert check.status == WARN
    assert "untrusted" in check.detail and "Trust" in check.detail


def test_project_preflight_non_git_warns_about_worktree():
    from clauster.models import Project, TrustState

    proj = Project(
        name="alpha", path=Path("/p/alpha"), is_git_repo=False, trust_state=TrustState.TRUSTED
    )
    check = _preflight(proj)["git"]
    assert check.status == WARN
    assert "worktree" in check.detail


# ----- _check_claude_login ----------------------------------------------


def _creds(tmp_path: Path, payload: str) -> Path:
    p = tmp_path / ".credentials.json"
    p.write_text(payload)
    return p


def test_login_ok_with_token(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = _check_claude_login(_creds(tmp_path, '{"claudeAiOauth": {"accessToken": "tok"}}'))
    assert c.name == "claude-login" and c.status == OK


def test_login_missing_file_warns(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = _check_claude_login(tmp_path / "nope.json")
    assert c.status == WARN and "not logged in" in c.detail


def test_login_api_key_overrides_absent_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    c = _check_claude_login(tmp_path / "nope.json")
    assert c.status == OK and "ANTHROPIC_API_KEY" in c.detail


def test_login_present_but_no_token_warns(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = _check_claude_login(_creds(tmp_path, '{"claudeAiOauth": {}}'))
    assert c.status == WARN and "no access token" in c.detail


def test_login_malformed_json_warns(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = _check_claude_login(_creds(tmp_path, "{not json"))
    assert c.status == WARN and "not valid JSON" in c.detail


@pytest.mark.parametrize("payload", ["null", "123", "[]", '"x"'])
def test_login_non_object_json_does_not_crash(tmp_path, monkeypatch, payload):
    # Valid JSON that isn't an object must WARN, never raise AttributeError (CodeRabbit).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = _check_claude_login(_creds(tmp_path, payload))
    assert c.status == WARN


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
    dest = tmp_path / "freshstate"  # does not exist yet
    with pytest.raises(FileExistsError):
        restore_backup(archive, state_dir=dest, config_out=existing)
    # config conflict is detected up front, so state must NOT be half-applied.
    assert not dest.exists()
    assert existing.read_text() == "keep me"  # untouched


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


def test_restore_force_is_replace_not_merge(write_config, tmp_path):
    # A forced restore must REPLACE the state dir, not merge into it: a stale file
    # that isn't in the backup must be gone afterwards (it would otherwise survive
    # and silently outlive the restore).
    config = load_config(_cfg_file(write_config, tmp_path))
    _seed_state(config.state_dir)
    archive = make_backup(config, tmp_path / "out")
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "stale.json").write_text("from a previous life")

    restore_backup(archive, state_dir=dest, force=True)

    assert (dest / "state.json").is_file()  # backup contents present
    assert not (dest / "stale.json").exists()  # stale file replaced away


def test_restore_rolls_back_on_copy_failure(write_config, tmp_path, monkeypatch):
    # If the copy fails partway, the original state_dir must be left intact — never
    # a half-applied mix of old and new.
    config = load_config(_cfg_file(write_config, tmp_path))
    _seed_state(config.state_dir)
    archive = make_backup(config, tmp_path / "out")
    dest = tmp_path / "live"
    dest.mkdir()
    (dest / "keepme.json").write_text("precious")

    import clauster.ops as ops

    def boom(src, dst, *a, **k):
        raise OSError("simulated: disk full mid-restore")

    monkeypatch.setattr(ops.shutil, "copy2", boom)
    with pytest.raises(OSError):
        restore_backup(archive, state_dir=dest, force=True)

    assert (dest / "keepme.json").read_text() == "precious"  # untouched
    assert not (dest / "state.json").exists()  # nothing half-applied


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
    sj.write_text(
        json.dumps(
            {
                "schema_version": 0,
                "instances": {
                    "alpha": {
                        "label": "alpha",
                        "intentional_stop": True,
                        "spawn_mode": "same-dir",
                    }
                },
            }
        )
    )
    result = migrate_state(config)
    assert result["schema_version"] == 1
    assert json.loads(sj.read_text())["schema_version"] == 1
    assert (config.state_dir / "state.json.bak").is_file()  # migration backed up


# ----- install-service --------------------------------------------------


def test_service_systemd():
    unit = render_service_unit(
        "systemd",
        python="/usr/bin/python3",
        config_path="/etc/clauster/clauster.yml",
        user="clauster",
    )
    assert "[Service]" in unit and "ExecStart=/usr/bin/python3 -m clauster run" in unit
    assert "User=clauster" in unit and "Restart=on-failure" in unit
    # KillMode=process so detached pty (true-resume) bridges survive a restart
    # instead of being reaped with the service cgroup (the default control-group).
    assert "KillMode=process" in unit


def test_service_launchd():
    unit = render_service_unit(
        "launchd", python="/usr/bin/python3", config_path="/etc/clauster/clauster.yml"
    )
    assert "<plist" in unit and "org.clauster.daemon" in unit and "RunAtLoad" in unit


def test_service_windows():
    unit = render_service_unit(
        "windows", python="C:\\py\\python.exe", config_path="C:\\clauster.yml"
    )
    assert "nssm install Clauster" in unit


def test_service_launchd_escapes_xml_special_chars():
    # Item-5 (#408): a path with XML-significant chars (& < >) must be escaped so the
    # plist stays well-formed (and can't be injected). Parsing both proves it's valid
    # XML and lets us assert the escaped values round-trip back to the originals.
    import xml.dom.minidom

    unit = render_service_unit(
        "launchd",
        python="/opt/py & co/python3",
        config_path="/etc/clauster/<weird>&.yml",
        workdir="/srv/a&b<c>",
    )
    # The literal special chars never appear unescaped inside the rendered document.
    assert "/opt/py & co/python3" not in unit  # the raw '&' was escaped
    assert "&amp;" in unit and "&lt;" in unit and "&gt;" in unit
    # Valid XML (would raise on a stray & / unescaped <); the escaped values
    # round-trip back to the originals when parsed. Parsing our OWN rendered output,
    # not untrusted data, so S318 (defusedxml) does not apply.
    doc = xml.dom.minidom.parseString(unit)  # noqa: S318
    strings = [n.firstChild.data for n in doc.getElementsByTagName("string") if n.firstChild]
    assert "/opt/py & co/python3" in strings
    assert "/srv/a&b<c>" in strings


def test_service_windows_rejects_quote_in_path():
    # Item-5 (#408): a double-quote is illegal in a Windows path; reject it rather
    # than let it break out of the "%s" quoting and inject extra batch tokens.
    with pytest.raises(ValueError, match="illegal double-quote"):
        render_service_unit("windows", python='C:\\p"x\\python.exe')
    with pytest.raises(ValueError, match="illegal double-quote"):
        render_service_unit("windows", python="C:\\py\\python.exe", workdir='C:\\bad"dir')


def test_service_frozen_binary_drops_module_flag(monkeypatch):
    # #587: for a frozen/standalone binary sys.executable IS clauster, so the unit
    # must invoke it directly. Prepending `-m clauster` produced `clauster -m clauster
    # run`, which clauster's own argparse rejects — the service never started.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/home/u/.local/bin/clauster")
    unit = render_service_unit("systemd", config_path="/etc/clauster/clauster.yml")
    assert "ExecStart=/home/u/.local/bin/clauster run -c /etc/clauster/clauster.yml" in unit
    assert "-m clauster" not in unit


def test_service_frozen_binary_drops_module_flag_all_kinds(monkeypatch):
    # The argv builder is shared across kinds, so launchd and windows must drop the
    # `-m clauster` prefix for a frozen binary too — not just systemd.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/clauster/clauster")
    for kind in ("systemd", "launchd", "windows"):
        unit = render_service_unit(kind, config_path="/etc/clauster/clauster.yml")
        assert "-m clauster" not in unit
        assert "/opt/clauster/clauster" in unit


def test_service_console_script_drops_module_flag(monkeypatch):
    # #587: a `clauster` console script on PATH (uv tool / pipx / pip) is a stable
    # entry point — invoke it directly rather than `<venv-python> -m clauster`.
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/home/u/.local/bin/clauster")
    unit = render_service_unit("systemd", config_path="/etc/clauster/clauster.yml")
    assert "ExecStart=/home/u/.local/bin/clauster run -c" in unit
    assert "-m clauster" not in unit


def test_service_interpreter_fallback_keeps_module_flag(monkeypatch):
    # Dev / `python -m clauster`: no frozen binary and no console script on PATH, so
    # the bare interpreter still needs the `-m clauster` module form.
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr("shutil.which", lambda name: None)
    unit = render_service_unit("systemd", config_path="/etc/clauster/clauster.yml")
    assert "ExecStart=/usr/bin/python3 -m clauster run -c" in unit


def test_service_unknown_kind():
    with pytest.raises(ValueError):
        render_service_unit("upstart")


def test_default_service_path_per_kind():
    from clauster.ops import default_service_path

    assert default_service_path("systemd") == Path("/etc/systemd/system/clauster.service")
    assert default_service_path("launchd").name == "org.clauster.daemon.plist"
    assert default_service_path("windows").name == "install-clauster-service.bat"


def test_default_service_path_unknown_kind():
    from clauster.ops import default_service_path

    with pytest.raises(ValueError):
        default_service_path("upstart")
