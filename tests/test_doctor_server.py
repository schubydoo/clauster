"""``clauster doctor`` reads the running server's own results (TASK-507).

With an API token, the CLI shows the ``/api/doctor`` rows of the server that holds the
configured port, so it matches the dashboard panel. Without one, or when the server is
unreachable or refuses the token, it shows its local checks and names the reason. The
two are never mixed.
"""

from __future__ import annotations

import http.server
import json
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from clauster import auth
from clauster import ops as ops_mod
from clauster.app import create_app
from clauster.config import load_config
from clauster.ops import FAIL, OK, WARN, run_cli_doctor, run_doctor
from clauster.tls_provision import generate_self_signed

TOKEN = "good-token-abc123"

SERVER_BODY = {
    "ok": True,
    "checks": [
        {"name": "config", "status": "ok", "detail": "loaded /srv/clauster.yml"},
        {"name": "bundle", "status": "ok", "detail": "unpacked program files present"},
        {"name": "extra:pyte", "status": "ok", "detail": "pyte importable"},
    ],
}


class _Doctor:
    """A loopback stand-in server: ``/healthz`` 200, ``/api/doctor`` by bearer token."""

    def __init__(self, doctor_status: int = 200, doctor_body: bytes | None = None) -> None:
        self.requests: list[tuple[str, str | None]] = []
        outer = self
        body = json.dumps(SERVER_BODY).encode() if doctor_body is None else doctor_body

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                presented = self.headers.get("Authorization")
                outer.requests.append((self.path, presented))
                if self.path == "/healthz":
                    status, payload = 200, b'{"status": "ok"}'
                elif presented != f"Bearer {TOKEN}":
                    status, payload = 401, b'{"detail": "authentication required"}'
                else:
                    status, payload = doctor_status, body
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]

    def tls(self, cert: Path, key: Path) -> _Doctor:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        return self

    def __enter__(self) -> _Doctor:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def doctor_requests(self) -> list[str | None]:
        return [token for path, token in self.requests if path == "/api/doctor"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(write_config, tmp_path, port: int, extra: str = "") -> str:
    return str(write_config(f"port: {port}\nstate_dir: {tmp_path / '.s'}\n{extra}"))


# ----- server reachable ------------------------------------------------------


def test_reachable_server_rows_replace_the_local_ones(write_config, tmp_path):
    with _Doctor() as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert report.source == f"showing the results of the running server on port {srv.port}"
    assert [(c.name, c.status, c.detail) for c in report.checks] == [
        (r["name"], r["status"], r["detail"]) for r in SERVER_BODY["checks"]
    ]
    # Never a mix: no local-only row joins the server's.
    assert {"port", "server"}.isdisjoint(c.name for c in report.checks)
    assert report.ok is True
    assert srv.doctor_requests() == [f"Bearer {TOKEN}"]


def test_reachable_server_fail_row_fails_the_cli(write_config, tmp_path):
    body = {
        "ok": False,
        "checks": [{"name": "bundle", "status": "fail", "detail": "3 missing; restart"}],
    }
    with _Doctor(doctor_body=json.dumps(body).encode()) as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert report.source.startswith("showing the results of the running server")
    assert report.ok is False


def test_a_fail_row_wins_over_an_ok_verdict(write_config, tmp_path):
    body = {"ok": True, "checks": [{"name": "claude", "status": "fail", "detail": "gone"}]}
    with _Doctor(doctor_body=json.dumps(body).encode()) as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert report.ok is False


def test_server_rows_cannot_drive_the_terminal(write_config, tmp_path):
    body = {"ok": True, "checks": [{"name": "x\x1b[2J", "status": "ok", "detail": "a\nb\x07"}]}
    with _Doctor(doctor_body=json.dumps(body).encode()) as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert [(c.name, c.detail) for c in report.checks] == [("x?[2J", "a?b?")]


def test_tls_server_is_read_through_the_pinned_certificate(write_config, tmp_path):
    cert, key = generate_self_signed(tmp_path / "a", ["example.test"])
    tls = f"tls:\n  cert_file: {cert.as_posix()}\n  key_file: {key.as_posix()}\n"
    with _Doctor().tls(cert, key) as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port, tls), token=TOKEN)
    assert report.source.startswith("showing the results of the running server")


# ----- server unreachable ----------------------------------------------------


def test_no_server_on_the_port_shows_local_checks(write_config, tmp_path):
    port = _free_port()
    report = run_cli_doctor(_cfg(write_config, tmp_path, port), token=TOKEN)
    assert report.source == (
        f"showing the local checks of this CLI: no server listens on 127.0.0.1:{port}"
    )
    by = {c.name: c for c in report.checks}
    assert by["port"].status == OK
    assert "config" in by


def test_a_non_http_listener_shows_local_checks(write_config, tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        port = s.getsockname()[1]
        stop = threading.Event()

        def _hang_up() -> None:
            s.settimeout(0.2)
            while not stop.is_set():
                try:
                    peer, _addr = s.accept()
                except OSError:
                    continue
                peer.close()

        t = threading.Thread(target=_hang_up, daemon=True)
        t.start()
        try:
            report = run_cli_doctor(_cfg(write_config, tmp_path, port), token=TOKEN)
        finally:
            stop.set()
            t.join(5)
    assert report.source.startswith("showing the local checks of this CLI: the server on port")
    assert "did not answer the doctor request" in report.source
    assert {c.name: c.status for c in report.checks}["port"] == WARN


def test_a_different_certificate_never_receives_the_token(write_config, tmp_path):
    cert, key = generate_self_signed(tmp_path / "a", ["example.test"])
    other, other_key = generate_self_signed(tmp_path / "b", ["example.test"])
    tls = f"tls:\n  cert_file: {other.as_posix()}\n  key_file: {other_key.as_posix()}\n"
    with _Doctor().tls(cert, key) as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port, tls), token=TOKEN)
    assert "presents a different certificate" in report.source
    assert srv.doctor_requests() == []


def test_an_unreadable_certificate_never_sends_the_token(write_config, tmp_path, monkeypatch):
    missing = tmp_path / "gone.crt"
    monkeypatch.setattr(ops_mod, "_server_cert_file", lambda config: missing)
    with _Doctor() as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert f"the certificate file {missing} is unreadable (FileNotFoundError)" in report.source
    assert srv.doctor_requests() == []


@pytest.mark.parametrize(("status", "reason"), [(404, "HTTP 404"), (500, "HTTP 500")])
def test_another_status_shows_local_checks(write_config, tmp_path, status, reason):
    with _Doctor(doctor_status=status, doctor_body=b"{}") as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert report.source.endswith(f"answered the doctor request with {reason}")


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"ok": true}',
        b'{"ok": true, "checks": []}',
        b'{"ok": "yes", "checks": [{"name": "a", "status": "ok", "detail": "d"}]}',
        b'{"ok": true, "checks": ["row"]}',
        b'{"ok": true, "checks": [{"name": "a", "status": "pass", "detail": "d"}]}',
        b'{"ok": true, "checks": [{"name": "a", "status": "ok", "detail": 3}]}',
        b"[" * 4000,
    ],
)
def test_unreadable_results_show_local_checks(write_config, tmp_path, body):
    with _Doctor(doctor_body=body) as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert report.source.endswith("sent doctor results that this CLI cannot read")
    assert "port" in {c.name for c in report.checks}


def test_an_oversized_body_shows_local_checks(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(ops_mod, "_SERVER_DOCTOR_MAX_BYTES", 16)
    with _Doctor() as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert report.source.endswith("sent doctor results that this CLI cannot read")


def test_a_config_that_does_not_load_shows_local_checks(tmp_path):
    bad = tmp_path / "clauster.yml"
    bad.write_text('claude:\n  binary: "unclosed\n', encoding="utf-8")
    report = run_cli_doctor(str(bad), token=TOKEN)
    assert report.source == "showing the local checks of this CLI: the config did not load"
    assert report.ok is False
    assert report.checks[0].name == "config"


# ----- auth refused ----------------------------------------------------------


def test_a_refused_token_shows_local_checks_and_names_the_status(write_config, tmp_path):
    with _Doctor() as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token="wrong-token")
    assert report.source == (
        "showing the local checks of this CLI: "
        f"the server on port {srv.port} refused the API token (HTTP 401)"
    )
    assert "wrong-token" not in report.source
    names = {c.name for c in report.checks}
    assert {"config", "port", "server"} <= names
    assert "bundle" not in names


def test_403_is_a_refusal_too(write_config, tmp_path):
    with _Doctor(doctor_status=403, doctor_body=b'{"detail": "no"}') as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=TOKEN)
    assert report.source.endswith("refused the API token (HTTP 403)")


def test_no_token_never_asks_the_server(write_config, tmp_path):
    with _Doctor() as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=None)
    assert report.source == (
        f"showing the local checks of this CLI: a server listens on port {srv.port}, but no "
        "API token was given (pass --token or set CLAUSTER_API_TOKEN)"
    )
    assert srv.doctor_requests() == []


@pytest.mark.parametrize("token", ["a\r\nX-Evil: 1", "has space", "café"])
def test_a_token_a_header_cannot_carry_is_never_sent(write_config, tmp_path, token):
    with _Doctor() as srv:
        report = run_cli_doctor(_cfg(write_config, tmp_path, srv.port), token=token)
    assert report.source.endswith("characters that an HTTP header cannot carry")
    assert token not in report.source
    assert srv.doctor_requests() == []


# ----- the port row when this deployment's own server holds it --------------


def test_port_row_is_ok_when_the_holder_is_a_healthy_server(write_config, tmp_path):
    with _Doctor() as srv:
        checks, _ok = run_doctor(_cfg(write_config, tmp_path, srv.port))
    by = {c.name: c for c in checks}
    assert by["port"].status == OK
    assert by["port"].detail == f"{srv.port} in use by the running Clauster server"
    assert by["server"].status == OK
    names = [c.name for c in checks]
    assert names.index("port") + 1 == names.index("server")


def test_port_row_still_warns_for_a_listener_that_is_not_a_clauster(write_config, tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        t = threading.Thread(target=lambda: s.accept()[0].close(), daemon=True)
        t.start()
        checks, _ok = run_doctor(_cfg(write_config, tmp_path, port))
        t.join(5)
    by = {c.name: c for c in checks}
    assert by["port"].status == WARN
    assert "server" not in by


# ----- end to end against the real app and its auth gate ---------------------


@pytest.fixture
def real_server(write_config, tmp_path):
    """Serve the real Clauster app, auth enabled with an API token, on a loopback port."""
    raw, token_hash = auth.mint_token()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    cfg = _cfg(
        write_config, tmp_path, port, f"auth:\n  enabled: true\n  api_token_hash: {token_hash}\n"
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(load_config(cfg)), log_level="warning", lifespan="on")
    )
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "the test server did not start"
        time.sleep(0.05)
    try:
        yield cfg, raw
    finally:
        server.should_exit = True
        t.join(20)
        sock.close()


def test_the_real_server_answers_with_its_own_rows(real_server):
    cfg, raw = real_server
    report = run_cli_doctor(cfg, token=raw)
    assert report.source.startswith("showing the results of the running server")
    names = {c.name for c in report.checks}
    assert {"config", "claude", "auth"} <= names
    # The server omits its own port probe, and the CLI adds nothing to its rows.
    assert {"port", "server"}.isdisjoint(names)
    assert all(c.status in (OK, WARN, FAIL) for c in report.checks)


def test_the_real_server_refuses_a_wrong_token(real_server):
    cfg, _raw = real_server
    report = run_cli_doctor(cfg, token="not-the-token")
    assert report.source.endswith("refused the API token (HTTP 401)")
    assert {c.name: c.status for c in report.checks}["server"] == OK
