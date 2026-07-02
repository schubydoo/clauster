"""CLI entry (`clauster.__main__:main`) — subcommand dispatch + exit codes.

Drives main([...]) directly; uvicorn.run and getpass are mocked so `run` and
`hash-password` don't block. Exercises the argparse wiring that the ops/* unit
tests don't touch.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from clauster import __main__ as cli
from clauster import auth
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


def test_help_documents_config_search_order(capsys):
    # `clauster --help` surfaces the auto-discovery order so needing -c isn't a
    # surprise (issue #482); the three locations must appear in search order.
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "$CLAUSTER_CONFIG" in out
    assert "./clauster.yml" in out
    assert "$CLAUSTER_HOME/clauster.yml" in out
    # In search order: env override, then cwd, then CLAUSTER_HOME.
    assert (
        out.index("$CLAUSTER_CONFIG")
        < out.index("./clauster.yml")
        < out.index("$CLAUSTER_HOME/clauster.yml")
    )


# ----- run (default) ----------------------------------------------------


def _stub_server(monkeypatch, on_run=None):
    """Replace ``uvicorn.Server`` so ``_run`` never binds a socket or blocks.

    ``_run`` now builds ``uvicorn.Server(uvicorn.Config(...))`` and calls
    ``server.run()`` (so the #483 restart path can ``should_exit`` it). The fake
    captures the Config kwargs and runs ``on_run(server)`` in place of serving — the
    restart tests use ``on_run`` to flip ``app.state.restart_requested`` mid-"serve".
    Returns the captured-config dict the test can assert on.
    """
    captured: dict = {}

    class _FakeServer:
        def __init__(self, config):
            self.config = config
            captured.update(
                {
                    "app": config.app,
                    "host": config.host,
                    "port": config.port,
                    "proxy_headers": config.proxy_headers,
                    # None when no tls is configured; the resolved abs path otherwise.
                    "ssl_certfile": getattr(config, "ssl_certfile", None),
                    "ssl_keyfile": getattr(config, "ssl_keyfile", None),
                }
            )
            self.should_exit = False

        def run(self):
            if on_run is not None:
                on_run(self)

    monkeypatch.setattr(cli.uvicorn, "Server", _FakeServer)
    return captured


def test_run_invokes_uvicorn(write_config, tmp_path, monkeypatch):
    captured = _stub_server(monkeypatch)
    rc = cli.main(["run", "-c", _cfg(write_config, tmp_path)])
    assert rc == 0
    assert captured.get("port") == 7621 and captured.get("proxy_headers") is False
    # No tls configured: uvicorn must NOT receive ssl_certfile/ssl_keyfile.
    assert captured.get("ssl_certfile") is None
    assert captured.get("ssl_keyfile") is None


def _tls_cert_key(tmp_path):
    """Throwaway cert + key files (filesystem-only validation needs no real TLS).

    The key is chmod 0600 so the (non-fatal) world-readable-key warning stays silent
    by default — tests that assert on the warning set the mode explicitly.
    """
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("CERT", encoding="utf-8")
    key.write_text("KEY", encoding="utf-8")
    if os.name == "posix":
        key.chmod(0o600)
    return cert, key


def test_run_passes_ssl_files_to_uvicorn_when_tls_set(write_config, tmp_path, monkeypatch):
    cert, key = _tls_cert_key(tmp_path)
    extra = f"tls:\n  cert_file: {cert}\n  key_file: {key}\n"
    captured = _stub_server(monkeypatch)
    # Throwaway files aren't real PEM, so stub the SSL pre-flight parse; the path
    # resolution + uvicorn wiring under test are independent of cert validity.
    monkeypatch.setattr(cli, "_verify_cert_chain", lambda cert, key: None)
    rc = cli.main(["run", "-c", _cfg(write_config, tmp_path, extra)])
    assert rc == 0
    # The resolved absolute cert/key reach uvicorn's Config so it terminates TLS itself.
    assert captured.get("ssl_certfile") == str(cert.resolve())
    assert captured.get("ssl_keyfile") == str(key.resolve())


def test_run_aborts_when_cert_unparseable(write_config, tmp_path, monkeypatch, capsys):
    # Defense-in-depth: a cert/key that exists and is readable but is NOT valid PEM
    # aborts cleanly (exit 2, our message) via the real SSL pre-flight — no raw
    # traceback, no plain-HTTP fallback. No stub here: the parse genuinely fails on
    # the throwaway non-PEM bytes, and the message must not leak key material.
    cert, key = _tls_cert_key(tmp_path)  # "CERT" / "KEY" — not real PEM
    key.write_text("SUPER-SECRET-KEY-BYTES", encoding="utf-8")
    extra = f"tls:\n  cert_file: {cert}\n  key_file: {key}\n"
    captured = _stub_server(monkeypatch)
    rc = cli.main(["run", "-c", _cfg(write_config, tmp_path, extra)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "TLS error" in err
    assert "could not be loaded" in err
    # Both cert AND key PATHS are named so a cert/key mismatch is debuggable...
    assert str(cert.resolve()) in err
    assert str(key.resolve()) in err
    assert "SUPER-SECRET-KEY-BYTES" not in err  # ...but key bytes never surface
    assert captured == {}  # server never constructed; no plain-HTTP fallback


def test_run_aborts_when_tls_cert_missing_at_start(write_config, tmp_path, monkeypatch, capsys):
    # Defense-in-depth: a cert that passed load-time validation but is gone by serve
    # time aborts startup (exit 2) — never a silent fall back to plain HTTP. Patch the
    # server-start resolver to simulate the file vanishing between load and serve.
    cert, key = _tls_cert_key(tmp_path)
    extra = f"tls:\n  cert_file: {cert}\n  key_file: {key}\n"
    captured = _stub_server(monkeypatch)

    def _boom(field, raw):
        raise ValueError(f"tls.{field} does not exist: {raw}")

    monkeypatch.setattr(cli, "resolve_cert_path", _boom)
    rc = cli.main(["run", "-c", _cfg(write_config, tmp_path, extra)])
    assert rc == 2
    assert "TLS error" in capsys.readouterr().err
    # The server was never constructed (no plain-HTTP fallback).
    assert captured == {}


def test_run_self_signed_provision_generates_and_serves(write_config, tmp_path, monkeypatch):
    # provision = self-signed: _run must call the provisioner and hand the generated
    # cert+key to uvicorn. The provisioner is real (cryptography is a core dep), so this
    # exercises the actual generate path (self-signed branch in _tls_files).
    extra = "tls:\n  provision: self-signed\n  hostnames: [localhost]\n"
    captured = _stub_server(monkeypatch)
    rc = cli.main(["run", "-c", _cfg(write_config, tmp_path, extra)])
    assert rc == 0
    # A real cert+key were generated under state_dir/tls/ and reach uvicorn.
    assert captured.get("ssl_certfile", "").endswith("self-signed.crt")
    assert captured.get("ssl_keyfile", "").endswith("self-signed.key")
    assert Path(captured["ssl_certfile"]).is_file()
    assert Path(captured["ssl_keyfile"]).is_file()


def test_run_self_signed_provision_aborts_on_provisioner_error(
    write_config, tmp_path, monkeypatch, capsys
):
    # A RuntimeError from the provisioner (e.g. cryptography missing) must abort cleanly
    # (exit 2, our TLS error message) — never a silent plain-HTTP fallback. Simulate it
    # by stubbing the provisioner to raise.
    extra = "tls:\n  provision: self-signed\n  hostnames: [localhost]\n"
    captured = _stub_server(monkeypatch)

    def _boom(state_dir, hostnames):
        raise RuntimeError("tls.provision = self-signed requires the 'cryptography' package")

    monkeypatch.setattr(cli, "generate_self_signed", _boom)
    rc = cli.main(["run", "-c", _cfg(write_config, tmp_path, extra)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "TLS error" in err
    assert "cryptography" in err
    # The server was never constructed (no plain-HTTP fallback).
    assert captured == {}


def test_bare_args_default_to_run(write_config, tmp_path, monkeypatch):
    # Backward compat: `clauster -c x` means `run`.
    ran = {}
    _stub_server(monkeypatch, on_run=lambda s: ran.setdefault("yes", True))
    assert cli.main(["-c", _cfg(write_config, tmp_path)]) == 0
    assert ran == {"yes": True}


def test_run_missing_config_exits_2(monkeypatch):
    _stub_server(monkeypatch)
    assert cli.main(["run", "-c", "/no/such/clauster.yml"]) == 2


def test_run_claude_not_found_exits_2(write_config, tmp_path, monkeypatch):
    _stub_server(monkeypatch)
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


# ----- hash-token (#360) ------------------------------------------------


def test_hash_token_prints_raw_to_stdout_and_hash_to_stderr(capsys):
    assert cli.main(["hash-token"]) == 0
    captured = capsys.readouterr()
    # The raw token (and only it) is on stdout so `hash-token | client` is clean.
    raw = captured.out.strip()
    assert raw.startswith("clauster_pat_")
    assert raw not in captured.err  # raw secret is never echoed into the guidance
    # The hash to paste into the config goes to stderr, and round-trips with the raw.
    assert "api_token_hash:" in captured.err
    assert auth.hash_token(raw) in captured.err
    assert auth.verify_token(raw, auth.hash_token(raw)) is True


# ----- hash-metrics-token (#473) ----------------------------------------


def test_hash_metrics_token_prints_raw_to_stdout_and_hash_to_stderr(capsys):
    assert cli.main(["hash-metrics-token"]) == 0
    captured = capsys.readouterr()
    # The raw token (and only it) is on stdout so the command can be piped cleanly.
    raw = captured.out.strip()
    assert raw.startswith("clauster_pat_")
    assert raw not in captured.err  # raw secret is never echoed into the guidance
    # The hash to paste into observability goes to stderr, and round-trips with the raw.
    assert "metrics_token_hash:" in captured.err
    assert auth.hash_token(raw) in captured.err
    assert auth.verify_token(raw, auth.hash_token(raw)) is True


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
    _stub_server(monkeypatch)
    assert cli.main(["run", "-c", cfg]) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "Secure" in err


def test_run_quiets_cookie_warning_under_tls(write_config, tmp_path, monkeypatch, capsys):
    # With native TLS the connection is https, so the cookie ships Secure under
    # cookie_secure: auto — the plain-http warning must NOT fire.
    from clauster import auth

    cert, key = _tls_cert_key(tmp_path)
    pw = auth.hash_password(auth.make_hasher(), "pw")
    extra = (
        f'auth:\n  enabled: true\n  password_required: true\n  password_hash: "{pw}"\n'
        f"tls:\n  cert_file: {cert}\n  key_file: {key}\n"
    )
    cfg = _cfg(write_config, tmp_path, extra)
    captured = _stub_server(monkeypatch)
    monkeypatch.setattr(cli, "_verify_cert_chain", lambda cert, key: None)
    assert cli.main(["run", "-c", cfg]) == 0
    err = capsys.readouterr().err
    assert "WARNING" not in err
    # And the banner advertises https rather than http.
    assert f"https://127.0.0.1:{7621}" in err
    assert captured.get("ssl_certfile") == str(cert.resolve())


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_run_warns_on_world_readable_key(write_config, tmp_path, monkeypatch, capsys):
    # Non-fatal hygiene nudge: an over-permissive (group/other-accessible) private key
    # warns but STILL serves (rc 0, server constructed) — never aborts startup.
    cert, key = _tls_cert_key(tmp_path)
    key.chmod(0o644)  # group + other readable
    extra = f"tls:\n  cert_file: {cert}\n  key_file: {key}\n"
    captured = _stub_server(monkeypatch)
    monkeypatch.setattr(cli, "_verify_cert_chain", lambda cert, key: None)
    rc = cli.main(["run", "-c", _cfg(write_config, tmp_path, extra)])
    assert rc == 0  # non-fatal: it warns but serves
    err = capsys.readouterr().err
    assert "WARNING" in err and "group/other-accessible" in err
    assert captured.get("ssl_certfile") == str(cert.resolve())  # server still wired up


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_run_silent_for_owner_only_key(write_config, tmp_path, monkeypatch, capsys):
    # A 0600 key is the correct posture — no warning.
    cert, key = _tls_cert_key(tmp_path)
    key.chmod(0o600)  # owner-only
    extra = f"tls:\n  cert_file: {cert}\n  key_file: {key}\n"
    _stub_server(monkeypatch)
    monkeypatch.setattr(cli, "_verify_cert_chain", lambda cert, key: None)
    assert cli.main(["run", "-c", _cfg(write_config, tmp_path, extra)]) == 0
    assert "group/other-accessible" not in capsys.readouterr().err


def test_key_perms_warning_skipped_on_non_posix(tmp_path, monkeypatch, capsys):
    # On Windows (os.name != "posix") the mode-bit check is skipped entirely — even a
    # would-be over-permissive key produces no warning, since the bits don't apply.
    key = tmp_path / "key.pem"
    key.write_text("KEY", encoding="utf-8")
    monkeypatch.setattr(cli.os, "name", "nt")
    cli._warn_if_key_world_readable(key)
    assert capsys.readouterr().err == ""


def test_run_reexecs_when_restart_requested(write_config, tmp_path, monkeypatch):
    # #483: when the in-app restart endpoint flips app.state.restart_requested during
    # serve(), _run re-execs in place exactly once after the server returns.
    calls = []
    monkeypatch.setattr(cli, "_reexec", lambda: calls.append(1))

    def _request_restart(server):
        server.config.app.state.restart_requested = True

    _stub_server(monkeypatch, on_run=_request_restart)
    assert cli.main(["run", "-c", _cfg(write_config, tmp_path)]) == 0
    assert calls == [1]


def test_run_does_not_reexec_on_normal_shutdown(write_config, tmp_path, monkeypatch):
    # A plain shutdown (no restart request) must NOT re-exec — it exits cleanly.
    calls = []
    monkeypatch.setattr(cli, "_reexec", lambda: calls.append(1))
    _stub_server(monkeypatch)  # on_run is a no-op: restart_requested stays False
    assert cli.main(["run", "-c", _cfg(write_config, tmp_path)]) == 0
    assert calls == []


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


def test_recap_hook_subcommand_dispatches(monkeypatch):
    # The hidden frozen-binary entry point dispatches to the recap hook and exits 0
    # (and isn't rewritten to `run` by the bare-args shim).
    called = {"ran": False}
    monkeypatch.setattr(
        "clauster.hooks.resume_recap.main", lambda: called.__setitem__("ran", True)
    )
    assert cli.main(["__recap-hook__"]) == 0
    assert called["ran"] is True


def test_recap_hook_subcommand_swallows_errors(monkeypatch):
    # A hook must never break the session it serves: an error in the recap still exits 0.
    def boom():
        raise RuntimeError("simulated recap failure")

    monkeypatch.setattr("clauster.hooks.resume_recap.main", boom)
    assert cli.main(["__recap-hook__"]) == 0


def test_pty_keeper_subcommand_forwards_to_keeper_main(monkeypatch):
    # The frozen-binary keeper entry point hands argv[1:] to pty_keeper.main and returns
    # its rc verbatim (and isn't rewritten to `run` by the bare-args shim).
    seen: dict[str, list[str]] = {}

    def fake_keeper_main(argv):
        seen["argv"] = argv
        return 7

    monkeypatch.setattr(cli.pty_keeper, "main", fake_keeper_main)
    rc = cli.main(["__pty-keeper__", "--sidecar", "/s.json", "--", "claude", "--rc", "p"])
    assert rc == 7
    assert seen["argv"] == ["--sidecar", "/s.json", "--", "claude", "--rc", "p"]


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


def test_install_service_write_non_permission_oserror_returns_1(tmp_path, capsys, monkeypatch):
    # A non-PermissionError write failure (e.g. ENOSPC) also fails closed with the
    # error line, but WITHOUT the privilege hint (that's PermissionError-specific).
    def boom(self, *a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_text", boom)
    rc = cli.main(["install-service", "systemd", "--write", str(tmp_path / "x.service")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not write" in err.lower()
    assert "privileges" not in err.lower()  # the sudo hint is PermissionError-only


@pytest.mark.parametrize(
    "kind,marker,hint",
    [
        ("launchd", "<plist", "launchctl load"),
        ("windows", "nssm install Clauster", "elevated prompt"),
    ],
)
def test_install_service_write_prints_platform_next_step(kind, marker, hint, tmp_path, capsys):
    # The --write next-step hint is platform-specific: launchctl on macOS, an
    # elevated nssm prompt on Windows (systemd's daemon-reload hint is covered above).
    dest = tmp_path / f"clauster.{kind}"
    rc = cli.main(
        ["install-service", kind, "-c", "/etc/clauster/clauster.yml", "--write", str(dest)]
    )
    assert rc == 0
    assert marker in dest.read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert str(dest) in err and hint in err


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
