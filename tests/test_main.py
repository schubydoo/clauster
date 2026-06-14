"""CLI entry (`clauster.__main__:main`) — subcommand dispatch + exit codes.

Drives main([...]) directly; uvicorn.run and getpass are mocked so `run` and
`hash-password` don't block. Exercises the argparse wiring that the ops/* unit
tests don't touch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from clauster import __main__ as cli
from clauster.config import ClausterConfig

# .cmd on Windows: the extensionless stub isn't launchable by subprocess, and
# only Python 3.12+ falls back to PATHEXT to find a sibling claude.cmd — be
# explicit so the version probe also resolves on 3.11.
_WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""
FAKE_CLAUDE = (
    Path(__file__).resolve().parent / "fixtures" / "fake_claude" / f"claude{_WIN_STUB_SUFFIX}"
)


def _cfg(write_config, tmp_path, claude_extra: str = "") -> str:
    extra = f"claude:\n  binary: {FAKE_CLAUDE}\n{claude_extra}state_dir: {tmp_path}/.cstate\n"
    return str(write_config(extra))


# ----- top-level --------------------------------------------------------


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])
    assert ei.value.code == 0
    assert "clauster" in capsys.readouterr().out


# ----- run (default) ----------------------------------------------------


def test_run_invokes_uvicorn(write_config, tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.update(kw) or None)
    rc = cli.main(["run", "-c", _cfg(write_config, tmp_path)])
    assert rc == 0
    assert calls.get("port") == 7621 and calls.get("proxy_headers") is False


def test_bare_args_default_to_run(write_config, tmp_path, monkeypatch):
    # Backward compat: `clauster -c x` means `run`.
    ran = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: ran.setdefault("yes", True))
    assert cli.main(["-c", _cfg(write_config, tmp_path)]) == 0
    assert ran == {"yes": True}


def test_run_missing_config_exits_2(monkeypatch):
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: None)
    assert cli.main(["run", "-c", "/no/such/clauster.yml"]) == 2


def test_run_claude_not_found_exits_2(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: None)
    # point the binary at something that won't resolve
    bad = str(
        write_config(f"claude:\n  binary: definitely-not-claude\nstate_dir: {tmp_path}/.s\n")
    )
    assert cli.main(["run", "-c", bad]) == 2


# ----- hash-password ----------------------------------------------------


def test_hash_password_ok(monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: "hunter2")
    assert cli.main(["hash-password"]) == 0
    assert capsys.readouterr().out.strip().startswith("$argon2")


def test_hash_password_mismatch_exits_2(monkeypatch):
    answers = iter(["a", "b"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: next(answers))
    assert cli.main(["hash-password"]) == 2


def test_hash_password_empty_exits_2(monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda *_: "")
    assert cli.main(["hash-password"]) == 2


# ----- doctor -----------------------------------------------------------


def test_doctor_ok_exit_0(write_config, tmp_path):
    assert cli.main(["doctor", "-c", _cfg(write_config, tmp_path)]) == 0


def test_doctor_failure_exit_1(write_config, tmp_path):
    assert (
        cli.main(["doctor", "-c", _cfg(write_config, tmp_path, '  min_version: "9.9.9"\n')]) == 1
    )


# ----- backup / restore / migrate --------------------------------------


def test_backup_then_restore(write_config, tmp_path, capsys):
    cfg = _cfg(write_config, tmp_path)
    Path(f"{tmp_path}/.cstate").mkdir(parents=True, exist_ok=True)
    (Path(f"{tmp_path}/.cstate") / "state.json").write_text('{"schema_version":1,"instances":{}}')
    outdir = tmp_path / "bk"
    outdir.mkdir()
    assert cli.main(["backup", "-c", cfg, "-o", str(outdir)]) == 0
    archive = capsys.readouterr().out.strip().splitlines()[-1]
    assert Path(archive).is_file()
    assert cli.main(["restore", archive, "--state-dir", str(tmp_path / "restored")]) == 0
    assert (tmp_path / "restored" / "state.json").is_file()


def test_restore_missing_backup_exit_2(tmp_path):
    assert (
        cli.main(
            [
                "restore",
                str(tmp_path / "nope.tar.gz"),
                "--state-dir",
                str(tmp_path / "r"),
            ]
        )
        == 2
    )


def test_migrate_exit_0(write_config, tmp_path):
    cfg = _cfg(write_config, tmp_path)
    sd = Path(f"{tmp_path}/.cstate")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "state.json").write_text(json.dumps({"schema_version": 0, "instances": {}}))
    assert cli.main(["migrate", "-c", cfg]) == 0
    assert json.loads((sd / "state.json").read_text())["schema_version"] == 1


def test_backup_failure_exit_1(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "clauster.ops.make_backup",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert cli.main(["backup", "-c", _cfg(write_config, tmp_path)]) == 1


def test_run_warns_on_insecure_cookie(write_config, tmp_path, monkeypatch, capsys):
    from clauster import auth

    pw = auth.hash_password(auth.make_hasher(), "pw")
    extra = f'auth:\n  enabled: true\n  password_required: true\n  password_hash: "{pw}"\n'
    cfg = _cfg(write_config, tmp_path, extra)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **k: None)
    assert cli.main(["run", "-c", cfg]) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "Secure" in err


# ----- install-service --------------------------------------------------


@pytest.mark.parametrize(
    "kind,marker",
    [
        ("systemd", "[Service]"),
        ("launchd", "<plist"),
        ("windows", "nssm install Clauster"),
    ],
)
def test_install_service(kind, marker, capsys):
    assert cli.main(["install-service", kind, "-c", "/etc/clauster/clauster.yml"]) == 0
    assert marker in capsys.readouterr().out


def test_install_service_bad_kind_exits_2():
    with pytest.raises(SystemExit) as ei:  # argparse choices -> exit 2
        cli.main(["install-service", "upstart"])
    assert ei.value.code == 2


def test_install_service_write_to_path(tmp_path, capsys):
    dest = tmp_path / "clauster.service"
    rc = cli.main(
        ["install-service", "systemd", "-c", "/etc/clauster/clauster.yml", "--write", str(dest)]
    )
    assert rc == 0
    assert "[Service]" in dest.read_text(encoding="utf-8")
    assert "KillMode=process" in dest.read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert str(dest) in err and "daemon-reload" in err  # confirms write + next-step hint


def test_install_service_write_unwritable_returns_1(tmp_path, capsys, monkeypatch):
    # A write the process can't perform fails closed with a hint, not a traceback.
    def boom(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "write_text", boom)
    rc = cli.main(["install-service", "systemd", "--write", str(tmp_path / "x.service")])
    assert rc == 1
    assert "sudo" in capsys.readouterr().err.lower()


def test_install_service_write_without_path_uses_default(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "clauster.service"
    monkeypatch.setattr(cli.ops, "default_service_path", lambda _kind: dest)
    rc = cli.main(["install-service", "systemd", "--write"])
    assert rc == 0
    assert dest.is_file()
    assert "KillMode=process" in dest.read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert str(dest) in err and "daemon-reload" in err


# ----- process title (instance_name) ------------------------------------


def test_process_title_none_without_instance_name(projects_root):
    assert cli._process_title(ClausterConfig(projects_root=projects_root)) is None


def test_process_title_formats_instance_name(projects_root):
    cfg = ClausterConfig(projects_root=projects_root, instance_name="dev")
    assert cli._process_title(cfg) == "clauster[dev]"


def test_set_process_title_calls_setproctitle(projects_root, monkeypatch):
    seen: list[str] = []
    fake = type("S", (), {"setproctitle": staticmethod(lambda t: seen.append(t))})
    monkeypatch.setattr(cli, "_setproctitle", fake)
    cli._set_process_title(ClausterConfig(projects_root=projects_root, instance_name="dev"))
    assert seen == ["clauster[dev]"]


def test_set_process_title_noop_without_name(projects_root, monkeypatch):
    seen: list[str] = []
    fake = type("S", (), {"setproctitle": staticmethod(lambda t: seen.append(t))})
    monkeypatch.setattr(cli, "_setproctitle", fake)
    cli._set_process_title(ClausterConfig(projects_root=projects_root))
    assert seen == []


def test_set_process_title_swallows_runtime_error(projects_root, monkeypatch):
    # The retitle is cosmetic: a setproctitle() that throws at runtime (restricted
    # env / platform quirk / permissions) must never crash `clauster run`.
    def boom(_title):
        raise RuntimeError("no can do")

    fake = type("S", (), {"setproctitle": staticmethod(boom)})
    monkeypatch.setattr(cli, "_setproctitle", fake)
    cli._set_process_title(ClausterConfig(projects_root=projects_root, instance_name="dev"))
