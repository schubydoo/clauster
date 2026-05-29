"""Feature 5 — URL display (deep link + env link) and QR endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from clauster.app import create_app
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner


def _running(name: str, **kw) -> RemoteControlInstance:
    return RemoteControlInstance(project=name, label=name, status=InstanceStatus.RUNNING, **kw)


def _client_with(runner_config, instance: RemoteControlInstance | None) -> TestClient:
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    if instance is not None:
        runner._instances[instance.project] = instance
    return TestClient(create_app(config, runner=runner))


def test_session_url_computed_field():
    i = _running("x", starter_session_id="session_01ABCDEF")
    assert i.session_url == "https://claude.ai/code/session_01ABCDEF?from=cli"
    assert _running("x").session_url is None


def test_session_url_serialized_in_api(runner_config):
    inst = _running("alpha", starter_session_id="session_01ABCDEF",
                    url="https://claude.ai/code?environment=env_01ZZZ")
    with _client_with(runner_config, inst) as client:
        body = client.get("/api/instances/alpha").json()
        assert body["session_url"] == "https://claude.ai/code/session_01ABCDEF?from=cli"
        assert body["url"].endswith("environment=env_01ZZZ")


def test_qr_returns_svg(runner_config):
    inst = _running("alpha", starter_session_id="session_01ABCDEF")
    with _client_with(runner_config, inst) as client:
        r = client.get("/api/instances/alpha/qr")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in r.text


def test_qr_unknown_instance_404(runner_config):
    with _client_with(runner_config, None) as client:
        assert client.get("/api/instances/ghost/qr").status_code == 404


def test_qr_409_when_no_url(runner_config):
    inst = _running("alpha")  # no starter_session_id, no url
    with _client_with(runner_config, inst) as client:
        assert client.get("/api/instances/alpha/qr").status_code == 409
