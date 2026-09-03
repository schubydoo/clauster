"""Endpoint + WebSocket wiring for the hosted channel (CL-4b).

The HostedManager/HostedSession engine is unit-tested in ``test_hosted.py`` against
a real fake daemon. Here the manager and daemon are stubbed so the focus is the
app boundary: channel dispatch in ``POST /api/instances``, the message/stop
endpoints, and the ``/ws/hosted`` stream — no real socket or cross-loop client.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import clauster.app as app_module
from clauster import claude_cli
from clauster.app import create_app
from clauster.claustrum_client import ClaustrumClient, ClaustrumError
from clauster.config import load_config
from clauster.hosted import HostedSessionError
from clauster.models import InstanceStatus, RemoteControlInstance

_HID = "hid-1"


class _StubSession:
    """A hosted session that replays a fixed ring on subscribe."""

    def __init__(self) -> None:
        self.events = [{"event_seq": 1, "type": "frame", "frame": {"type": "system"}}]
        self.unsubscribed = False

    def subscribe(self, after_seq: int = 0) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        for event in self.events:
            if event["event_seq"] > after_seq:
                queue.put_nowait(event)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.unsubscribed = True


class _StubManager:
    """Records boundary calls; no real daemon involved."""

    def __init__(self) -> None:
        self.instances: dict[str, RemoteControlInstance] = {}
        self.sessions: dict[str, _StubSession] = {}
        self.spawn_calls: list[dict] = []
        self.sent: list[tuple[str, str]] = []
        self.responded: list[tuple[str, str, dict]] = []
        self.stopped: list[str] = []
        self.killed_orphans: list[str] = []
        self.resumed: list[dict] = []
        self.spawn_error: Exception | None = None
        self.send_error: Exception | None = None
        self.respond_error: Exception | None = None
        self.resume_error: Exception | None = None
        self.kill_orphan_error: Exception | None = None
        self.forgotten: list[str] = []
        self.forget_error: Exception | None = None
        self.history_for = None

    def seed(self) -> RemoteControlInstance:
        inst = RemoteControlInstance(
            project="alpha",
            label="hosted:alpha",
            channel="hosted",
            claustrum_process_id=_HID,
            status=InstanceStatus.RUNNING,
        )
        self.instances[_HID] = inst
        self.sessions[_HID] = _StubSession()
        return inst

    async def spawn(
        self, client, *, project, label, cwd, claude_binary, permission_mode, resume_uuid=None
    ):
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawn_calls.append(
            {"project": project, "cwd": cwd, "binary": claude_binary, "pm": permission_mode}
        )
        return self.seed()

    def get_instance(self, hosted_id: str):
        return self.instances.get(hosted_id)

    def session(self, hosted_id: str):
        return self.sessions.get(hosted_id)

    def list_instances(self):
        return list(self.instances.values())

    async def reattach_all(self, client, *, history_for=None):
        # Capture the app's transcript resolver (#1045) so a test can drive it
        # directly — it's a create_app closure with no other handle.
        self.history_for = history_for
        return list(self.instances.values())

    async def persist(self):
        pass

    async def send(self, hosted_id: str, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        if hosted_id not in self.sessions:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        self.sent.append((hosted_id, text))

    async def respond(self, hosted_id: str, request_id: str, response: dict) -> None:
        if self.respond_error is not None:
            raise self.respond_error
        if hosted_id not in self.sessions:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        self.responded.append((hosted_id, request_id, response))

    async def stop(self, hosted_id: str) -> RemoteControlInstance:
        self.stopped.append(hosted_id)
        inst = self.instances[hosted_id]
        inst.status = InstanceStatus.STOPPED
        return inst

    async def kill_orphan(self, hosted_id: str) -> RemoteControlInstance:
        if self.kill_orphan_error is not None:
            raise self.kill_orphan_error
        self.killed_orphans.append(hosted_id)
        inst = self.instances[hosted_id]
        inst.status = InstanceStatus.STOPPED
        inst.is_orphan = False
        return inst

    async def forget(self, hosted_id: str) -> None:
        if self.forget_error is not None:
            raise self.forget_error
        # Mirror the production contract: an unknown id raises (the endpoint maps it
        # to 404), so the stub can't mask a contract regression by silently no-opping.
        if hosted_id not in self.instances:
            raise HostedSessionError(f"no such hosted session: {hosted_id}")
        self.forgotten.append(hosted_id)
        self.instances.pop(hosted_id, None)
        self.sessions.pop(hosted_id, None)

    async def resume(self, client, hosted_id, *, cwd, claude_binary):
        if self.resume_error is not None:
            raise self.resume_error
        self.resumed.append({"id": hosted_id, "cwd": cwd, "binary": claude_binary})
        # Mirror the production contract: the dead row is retired, a fresh one is live.
        self.instances.pop(hosted_id, None)
        new = RemoteControlInstance(
            project="alpha",
            label="hosted:alpha",
            channel="hosted",
            claustrum_process_id="hid-2",
            status=InstanceStatus.RUNNING,
        )
        self.instances["hid-2"] = new
        return new

    async def aclose(self) -> None:
        pass


class _StubDaemon:
    def __init__(self, client: object | None = None) -> None:
        self._client = client if client is not None else object()

    @property
    def client(self):
        return self._client

    async def aclose(self) -> None:
        pass


def _app(write_config, *, manager: _StubManager | None = None, daemon: _StubDaemon | None = None):
    config = load_config(write_config(""))
    app = create_app(config)
    app.state.hosted = manager if manager is not None else _StubManager()
    app.state.claustrum_daemon = daemon
    return app


# -- spawn dispatch --------------------------------------------------------


def test_spawn_hosted_dispatches_to_manager(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 201
    assert r.json()["channel"] == "hosted"
    assert manager.spawn_calls[0]["project"] == "alpha"
    assert manager.spawn_calls[0]["binary"] == "/usr/bin/claude"
    # Full path (resolve() normalizes separators) — basename alone would pass a wrong path.
    assert Path(manager.spawn_calls[0]["cwd"]).resolve() == (projects_root / "alpha").resolve()


def test_spawn_hosted_without_daemon_is_503(write_config, projects_root):
    app = _app(write_config, daemon=None)  # claustrum disabled → no daemon
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 503


def test_spawn_hosted_untrusted_is_409(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: False)
    app = _app(write_config, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 409


def test_spawn_unknown_channel_is_422(write_config, projects_root):
    app = _app(write_config)
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "bogus"})
    assert r.status_code == 422


def test_spawn_hosted_unknown_permission_mode_is_422(write_config, projects_root):
    # Parity with the bridge channel (the runner rejects an unknown mode pre-argv,
    # #734): an unknown permission_mode is a client error → a clean 422, not a 502 from
    # a downstream daemon spawn. Validated before any daemon/trust work, so no daemon is
    # needed for this to resolve.
    app = _app(write_config, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(
            "/api/instances",
            json={"project": "alpha", "channel": "hosted", "permission_mode": "bogusMode"},
        )
    assert r.status_code == 422
    assert "permission_mode" in r.json()["detail"]


# -- forget (POST /api/instances/{id}/forget) ------------------------------


def test_forget_hosted_dispatches_to_manager(write_config):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/forget")
    assert r.status_code == 200
    assert r.json() == {"id": _HID, "forgotten": True}
    assert manager.forgotten == [_HID]  # routed to the hosted manager, not the bridge runner


def test_forget_hosted_live_is_409(write_config):
    # A known hosted id that's still live: the manager refuses (HostedSessionError) -> 409.
    manager = _StubManager()
    manager.seed()
    manager.forget_error = HostedSessionError("still running — Stop it first")
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/forget")
    assert r.status_code == 409
    assert "Stop it first" in r.json()["detail"]


# -- list (GET /api/hosted) ------------------------------------------------


def test_api_hosted_lists_sessions(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.get("/api/hosted")
    assert r.status_code == 200
    body = r.json()
    assert [h["claustrum_process_id"] for h in body] == [_HID]
    assert body[0]["channel"] == "hosted" and body[0]["project"] == "alpha"


def test_api_hosted_empty_when_no_sessions(write_config, projects_root):
    app = _app(write_config)  # fresh _StubManager, nothing seeded
    with TestClient(app) as client:
        r = client.get("/api/hosted")
    assert r.status_code == 200
    assert r.json() == []


def test_api_hosted_serializes_claude_session_uuid_usable(write_config, projects_root):
    """#1419: the mirror the dashboard gates Resume on must carry the shape-checked signal.

    `api_hosted` returns the model, serialized to JSON, so the `claude_session_uuid_usable`
    computed field is the single signal the dashboard reads. A valid session-shaped uuid is
    usable; an off-shape one is KEPT on the record (for repair) but reported unusable, so the
    Resume gate hides consistently before and after a restart.
    """
    manager = _StubManager()
    manager.instances["ok"] = RemoteControlInstance(
        project="alpha",
        label="hosted:alpha",
        channel="hosted",
        claustrum_process_id="ok",
        status=InstanceStatus.CRASHED,
        claude_session_uuid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    )
    manager.instances["bad"] = RemoteControlInstance(
        project="alpha",
        label="hosted:alpha",
        channel="hosted",
        claustrum_process_id="bad",
        status=InstanceStatus.CRASHED,
        claude_session_uuid="--dangerously-skip-permissions",
    )
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        body = {h["claustrum_process_id"]: h for h in client.get("/api/hosted").json()}
    assert body["ok"]["claude_session_uuid_usable"] is True
    # Kept verbatim on the wire, but flagged unusable so the front-end gate can hide Resume.
    assert body["bad"]["claude_session_uuid"] == "--dangerously-skip-permissions"
    assert body["bad"]["claude_session_uuid_usable"] is False


# -- dashboard panel gating (claustrum.enabled) ----------------------------


class _NoopDaemon:
    """Stand-in for ClaustrumDaemon so the enabled-config lifespan spawns nothing."""

    def __init__(self, _config: object) -> None:
        self.client = object()

    async def ensure(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def test_hosted_history_resolver_reads_the_on_disk_transcript(
    write_config, projects_root, monkeypatch, tmp_path
):
    # #1045: the resolver the lifespan hands reattach_all, which a reattached Direct
    # session rehydrates its view from. It's a create_app closure, so the lifespan is
    # the only handle on it — the stub manager captures it.
    from clauster import usage as usage_mod
    from clauster.pointers import sanitize_cwd

    claude_dir = tmp_path / "claude_projects"
    real_resolve = usage_mod.resolve_session_transcript
    # resolve_session_transcript defaults to ~/.claude/projects — the live account
    # dir. Rebind it onto the tmp dir, as the transcript-route tests do.
    monkeypatch.setattr(
        usage_mod,
        "resolve_session_transcript",
        lambda project_path, session, claude_projects_dir=claude_dir: real_resolve(
            project_path, session, claude_projects_dir
        ),
    )
    tdir = claude_dir / sanitize_cwd(projects_root / "alpha")
    tdir.mkdir(parents=True)
    record = {"message": {"role": "user", "content": "hello"}, "timestamp": "t1"}
    (tdir / "sess-1.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    monkeypatch.setattr(app_module, "ClaustrumDaemon", _NoopDaemon)
    config = load_config(write_config("claustrum:\n  enabled: true\n"))
    app = create_app(config)
    manager = _StubManager()
    app.state.hosted = manager
    with TestClient(app):
        pass  # the lifespan's reattach_all hands the resolver to the stub
    history_for = manager.history_for
    assert history_for is not None

    def _row(uuid, project="alpha"):
        return RemoteControlInstance(
            project=project, label="hosted", channel="hosted", claude_session_uuid=uuid
        )

    assert history_for(_row("sess-1")) == [
        {"role": "user", "content": "hello", "model": None, "timestamp": "t1"}
    ]
    # Every "nothing to restore" case answers with [], never an error — rehydration
    # must not be able to fail a reattach.
    assert history_for(_row(None)) == []  # no session uuid captured yet
    assert history_for(_row("sess-1", project="bad.name")) == []  # unsafe project name
    assert history_for(_row("sess-1", project="")) == []  # no project recorded
    assert history_for(_row("no-such-session")) == []  # no transcript on disk

    # An oversized transcript is TAIL-read, not parsed whole: this runs in the startup
    # lifespan, so an unbounded parse could exhaust memory before the app serves.
    monkeypatch.setattr(app_module, "_HOSTED_HISTORY_MAX_BYTES", 512)
    lines = [
        json.dumps({"message": {"role": "user", "content": f"turn {n}"}, "timestamp": "t"})
        for n in range(200)
    ]
    (tdir / "big.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    tail = history_for(_row("big"))
    assert 0 < len(tail) < 200  # only the tail was read
    assert tail[-1]["content"] == "turn 199"  # ...and it is the NEWEST end of the file


def test_dashboard_hides_hosted_panel_when_disabled(write_config, projects_root):
    app = _app(write_config)  # claustrum disabled by default
    with TestClient(app) as client:
        body = client.get("/").text
    assert "const CLAUSTRUM_ENABLED = false" in body
    # The claustrum-gated hosted-session row (and its inline permission controls)
    # is never rendered when the channel is off; the row macro is only expanded
    # inside the `{% if claustrum_enabled %}` blocks of the unified Active list.
    assert "respondHosted(h.claustrum_process_id" not in body


def test_dashboard_shows_hosted_panel_when_enabled(write_config, projects_root, monkeypatch):
    # Redesign: hosted is the "Here in the browser" launch option plus a row in the
    # unified Active list (with inline permission Allow/Deny). When claustrum is
    # enabled, the hosted JS + browser launch mode + the gated row markup all ship.
    monkeypatch.setattr(app_module, "ClaustrumDaemon", _NoopDaemon)
    config = load_config(write_config("claustrum:\n  enabled: true\n"))
    app = create_app(config)
    app.state.hosted = _StubManager()
    with TestClient(app) as client:
        body = client.get("/").text
    assert "const CLAUSTRUM_ENABLED = true" in body
    assert "Here in the browser" in body  # the browser launch mode
    assert "_launchHosted" in body and "startHosted" in body  # hosted launch JS
    assert "respondHosted(h.claustrum_process_id" in body  # gated hosted-row controls
    assert ':class="hostedStatusDot(h.status)"' in body  # the hosted row wires the status-dot


def test_hosted_resume_gate_says_the_same_thing_in_all_five_places(
    write_config, projects_root, monkeypatch
):
    """Five front-end mirrors of one backend branch, each pinned, each written the same way.

    `reattach_all` names Resume in a row's `error_detail` only when BOTH halves survive.
    The mirrors are the Resume button, the orphan badge's tooltip, the "resume unavailable"
    chip, `hasResumable` (the Recent group label), and `_hostedEndedReason` (the View
    panel's ended banner). #1381 aligned the project half; #1392 widened the set of rows
    that lose the *uuid* half well past the empty string; #1419 then KEEPS an off-shape uuid
    on the record and moves the display gate to `claude_session_uuid_usable` (the model's
    shape-checked computed field), so every one of them has to test both -- and all five
    spell the gate `h.claude_session_uuid_usable && h.project`, in that order, so a reader
    comparing them is comparing identical text.

    Substring pins in the style of `resumeBlockedReason` above: brittle by design, so a
    reflow forces a re-read instead of passing silently.
    """
    monkeypatch.setattr(app_module, "ClaustrumDaemon", _NoopDaemon)
    config = load_config(write_config("claustrum:\n  enabled: true\n"))
    app = create_app(config)
    app.state.hosted = _StubManager()
    with TestClient(app) as client:
        body = client.get("/").text
    # 1. the Resume button. The pin carries its status list because the bare gate is also a
    # SUBSTRING of the chip's negation, of `hasResumable` and of `_hostedEndedReason` -- on
    # its own it would stay green with the Resume `x-if` deleted outright.
    assert (
        "['crashed', 'stopped', 'error'].includes(h.status) && h.claude_session_uuid_usable"
        " && h.project" in body
    )
    # 2. the orphan badge's tooltip, 3. the "resume unavailable" chip.
    assert "h.claude_session_uuid_usable && h.project ?" in body
    assert "!(h.claude_session_uuid_usable && h.project)" in body
    # 4. `hasResumable`, the Recent group label. Mutation-verified as the one mirror with no
    # pin: dropping its `&& h.project` left the whole suite green.
    assert "endedHosted().some((h) => h.claude_session_uuid_usable && h.project)" in body
    # The chip's reason is VISIBLE text, not a tooltip: a title never fires on touch, and
    # this is a phone-first product. Same contract as the bridge rows' `resume-blocked`.
    assert 'data-test="hosted-resume-blocked"' in body
    assert "resume unavailable — conversation id unknown" in body
    # ...and the case where BOTH halves are gone. `_degraded_row` salvages per field, so one
    # tampered record can drop the uuid and the project independently; falling back to the
    # project-only wording would have the operator repair one field and hit the same wall.
    assert "resume unavailable — project and conversation id unknown" in body
    assert "resume unavailable — project unknown" in body
    # 5. the View panel's ended banner: a project-ful, uuid-less row used to fall through
    # every branch and explain nothing -- the "fails opaquely" failure one layer down.
    assert "no usable conversation id was saved for it" in body
    # Pinned on the banner's OWN wording, not the shared phrase: the chip's `:title` also
    # says "neither a project nor a usable conversation id saved", so the shorter substring
    # passed with this branch deleted (caught by mutating it). "It cannot be resumed: it
    # has ..." is contiguous in `_hostedEndedReason` and appears nowhere else.
    assert "It cannot be resumed: it has neither a project nor a usable conversation" in body


def test_hosted_status_badge_colors_match_bridge(write_config, projects_root, monkeypatch):
    # #430: the hosted badge map must speak the same colour-to-meaning language as
    # the bridge STATUS_BADGE in the shared Active list. The three divergent
    # overrides (starting=blue, stopping=yellow, crashed=red) are unified onto the
    # bridge's vocabulary (azure / orange / amber); crashed=amber also matches the
    # hosted dot (bg-yellow), so the badge and dot agree within the row.
    monkeypatch.setattr(app_module, "ClaustrumDaemon", _NoopDaemon)
    config = load_config(write_config("claustrum:\n  enabled: true\n"))
    app = create_app(config)
    app.state.hosted = _StubManager()
    with TestClient(app) as client:
        body = client.get("/").text
    badge = body.split("hostedStatusBadge(status)", 1)[1].split("},", 1)[0]
    assert 'starting: "bg-azure-lt"' in badge
    assert 'stopping: "bg-orange-lt"' in badge
    assert 'crashed: "bg-yellow-lt"' in badge
    # The old divergent overrides are gone from the hosted badge map.
    for stale in ('starting: "bg-blue-lt"', 'stopping: "bg-yellow-lt"', 'crashed: "bg-red-lt"'):
        assert stale not in badge


# -- message ---------------------------------------------------------------


def test_message_routes_to_manager(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/message", json={"text": "hello"})
    assert r.status_code == 202
    assert manager.sent == [(_HID, "hello")]


def test_message_empty_text_is_422(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/message", json={"text": ""})
    assert r.status_code == 422


def test_message_unknown_instance_is_404(write_config, projects_root):
    app = _app(write_config)
    with TestClient(app) as client:
        r = client.post("/api/instances/nope/message", json={"text": "hi"})
    assert r.status_code == 404


# -- permissions (CL-5) ----------------------------------------------------


def test_permission_allow_routes_to_manager(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/permissions/perm-1", json={"decision": "allow"})
    assert r.status_code == 202
    assert manager.responded == [(_HID, "perm-1", {"behavior": "allow"})]


def test_permission_deny_carries_message(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(
            f"/api/instances/{_HID}/permissions/perm-1",
            json={"decision": "deny", "message": "nope"},
        )
    assert r.status_code == 202
    assert manager.responded == [(_HID, "perm-1", {"behavior": "deny", "message": "nope"})]


def test_permission_deny_defaults_message(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/permissions/perm-1", json={"decision": "deny"})
    assert r.status_code == 202
    assert manager.responded[0][2] == {"behavior": "deny", "message": "Denied by operator"}


def test_permission_bad_decision_is_422(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/permissions/perm-1", json={"decision": "maybe"})
    assert r.status_code == 422
    assert manager.responded == []


def test_permission_unknown_instance_is_404(write_config, projects_root):
    app = _app(write_config)
    with TestClient(app) as client:
        r = client.post("/api/instances/nope/permissions/perm-1", json={"decision": "allow"})
    assert r.status_code == 404


def test_permission_unparked_request_is_409(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    manager.respond_error = HostedSessionError("no parked control request 'perm-1'")
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/permissions/perm-1", json={"decision": "allow"})
    assert r.status_code == 409


# -- resume (CL-7) ---------------------------------------------------------


def test_resume_routes_hosted_to_manager(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/resume")
    assert r.status_code == 200
    assert manager.resumed[0]["id"] == _HID
    assert manager.resumed[0]["binary"] == "/usr/bin/claude"
    assert r.json()["claustrum_process_id"] == "hid-2"  # the fresh resumed instance
    assert _HID not in manager.instances  # dead row retired, not left as a duplicate


def test_resume_hosted_without_daemon_is_503(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager, daemon=None)  # claustrum disabled
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/resume")
    assert r.status_code == 503
    assert manager.resumed == []


def test_resume_hosted_session_error_is_409(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    manager.seed()
    manager.resume_error = HostedSessionError("no captured session uuid to resume from")
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/resume")
    assert r.status_code == 409


def test_resume_hosted_with_a_degraded_project_is_409_not_404(
    write_config, projects_root, monkeypatch
):
    # #1381. A record that degraded on `project` carries `project=""`. The route resolves the
    # project path BEFORE calling the engine, so an empty name used to 404 as "project ''
    # not found" -- which reads as "that project is gone" and sends the operator looking in
    # the wrong place. The check is hoisted above `_hosted_prereqs` so the answer is a 409
    # that says what actually happened, and the engine keeps its own guard for a non-route
    # caller. The dashboard hides Resume on the same condition, so this is the API contract
    # for a client that asks anyway.
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    manager.seed().project = ""  # what `_degraded_row` leaves behind
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/resume")

    assert r.status_code == 409
    assert "project is unknown" in r.json()["detail"]
    assert manager.resumed == []  # refused before reaching the engine at all


def test_resume_hosted_daemon_error_is_502(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    manager.seed()
    manager.resume_error = ClaustrumError("daemon went away")
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/resume")
    assert r.status_code == 502


# -- stop dispatch ---------------------------------------------------------


def test_delete_routes_hosted_to_manager(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.delete(f"/api/instances/{_HID}")
    assert r.status_code == 200
    assert manager.stopped == [_HID]
    assert r.json()["status"] == "stopped"


def test_delete_orphan_routes_to_kill(write_config, projects_root):
    # No live session (an orphan that survived a daemon restart) → DELETE kills it
    # via kill_orphan, not stop (which would require a session).
    manager = _StubManager()
    manager.seed()
    del manager.sessions[_HID]  # orphan: instance row present, no live session
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.delete(f"/api/instances/{_HID}")
    assert r.status_code == 200
    assert manager.killed_orphans == [_HID]
    assert manager.stopped == []  # took the kill path, not stop


def test_delete_hosted_race_returns_404(write_config, projects_root):
    # The row vanishes (concurrent stop/reattach) between the existence check and the
    # awaited kill — the endpoint maps HostedSessionError to 404, not an unhandled 500.
    manager = _StubManager()
    manager.seed()
    del manager.sessions[_HID]  # no live session → kill_orphan path
    manager.kill_orphan_error = HostedSessionError("no such hosted session: gone")
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.delete(f"/api/instances/{_HID}")
    assert r.status_code == 404


# -- websocket -------------------------------------------------------------


def test_ws_hosted_streams_then_replays(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with TestClient(app) as client, client.websocket_connect(f"/ws/hosted/{_HID}") as ws:
        event = ws.receive_json()
        assert event["type"] == "frame" and event["event_seq"] == 1


def test_ws_hosted_unknown_instance_closes(write_config, projects_root):
    app = _app(write_config)
    with TestClient(app) as client:
        with pytest.raises(Exception):  # noqa: B017 - server closes (1008) right after accept
            with client.websocket_connect("/ws/hosted/nope") as ws:
                ws.receive_json()


# -- spawn / message error branches ----------------------------------------


def test_spawn_hosted_binary_missing_is_503(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)

    def _missing(_binary):
        raise claude_cli.ClaudeNotFound("claude not found on PATH")

    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", _missing)
    app = _app(write_config, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 503


def test_spawn_hosted_daemon_error_is_502(write_config, projects_root, monkeypatch):
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    manager.spawn_error = ClaustrumError("daemon went away")
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 502


def test_spawn_hosted_session_error_is_409_not_a_daemon_502(
    write_config, projects_root, monkeypatch
):
    # `HostedSessionError` subclasses `ClaustrumError`, so without its own arm above the
    # 502 one it would render as "hosted spawn failed" — which reads as the daemon being
    # down and sends the operator to the wrong place. `build_hosted_argv` raising on a
    # refused resume session id (#1392) is a second way `start()` can produce this class.
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    manager = _StubManager()
    manager.spawn_error = HostedSessionError("refusing an unusable resume session id: int")
    app = _app(write_config, manager=manager, daemon=_StubDaemon())
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
    assert r.status_code == 409
    assert "unusable resume session id" in r.json()["detail"]
    assert "hosted spawn failed" not in r.json()["detail"]


def test_message_wrong_state_is_409(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    manager.send_error = HostedSessionError("cannot send to a stopped hosted session")
    app = _app(write_config, manager=manager)
    with TestClient(app) as client:
        r = client.post(f"/api/instances/{_HID}/message", json={"text": "hi"})
    assert r.status_code == 409


def test_ws_hosted_invalid_after_defaults_to_zero(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    app = _app(write_config, manager=manager)
    with (
        TestClient(app) as client,
        client.websocket_connect(f"/ws/hosted/{_HID}?after=oops") as ws,
    ):
        event = ws.receive_json()
        assert event["event_seq"] == 1  # bad cursor → replay from the start


def test_ws_hosted_unauthorized_closed(write_config, projects_root):
    config = load_config(write_config(""))
    config.auth.enabled = True  # WS must reject without a session (validate before accept)
    app = create_app(config)
    manager = _StubManager()
    manager.seed()
    app.state.hosted = manager
    with TestClient(app) as client:
        with pytest.raises(Exception):  # noqa: B017 - rejected handshake (1008)
            with client.websocket_connect(f"/ws/hosted/{_HID}") as ws:
                ws.receive_json()


class _BrokenQueue:
    """Yields one event, then raises — exercises the WS disconnect/teardown handler."""

    def __init__(self) -> None:
        self._n = 0

    async def get(self) -> dict:
        self._n += 1
        if self._n == 1:
            return {"event_seq": 1, "type": "frame", "frame": {}}
        raise RuntimeError("stream torn down")


class _BrokenSession:
    def subscribe(self, after_seq: int = 0) -> _BrokenQueue:
        return _BrokenQueue()

    def unsubscribe(self, queue: object) -> None:
        pass


def test_ws_hosted_handles_stream_teardown(write_config, projects_root):
    manager = _StubManager()
    manager.seed()
    manager.sessions[_HID] = _BrokenSession()  # second get() raises → handler returns cleanly
    app = _app(write_config, manager=manager)
    with TestClient(app) as client, client.websocket_connect(f"/ws/hosted/{_HID}") as ws:
        assert ws.receive_json()["event_seq"] == 1


# -- live-daemon lifespan + real-manager integration -----------------------
#
# The tests above stub the manager/daemon. These drive the REAL HostedManager
# against a REAL in-process fake claustrum daemon, through the app's lifespan and
# routes — so the route↔manager wiring and the lifespan reattach_all/aclose path
# are exercised end-to-end, not just at the stub boundary.

# The hosted channel itself is NOT POSIX-only (Windows dials a named pipe, #902) — these
# live-daemon e2e tests skip on Windows only because the `_LiveFakeDaemon` harness below serves
# an AF_UNIX `.sock`. The Windows named-pipe transport is covered by test_claustrum_client (dials
# the fake pipe on win32) + test_claustrum_daemon (`_simulate_win32` spawn). (#914)
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="_LiveFakeDaemon harness serves an AF_UNIX socket (see note)"
)


class _LiveFakeDaemon:
    """A ClaustrumDaemon stand-in backed by a real fake daemon + real client.

    ``ensure`` (run inside the app lifespan, on the app's event loop) starts an
    in-process :class:`FakeClaustrum` and a connected :class:`ClaustrumClient`, so
    ``reattach_all`` and hosted spawns hit a real socket. ``aclose`` tears both down.
    """

    instances: list[_LiveFakeDaemon] = []

    def __init__(self, _config: object) -> None:
        """Capture config (unused); nothing listens until :meth:`ensure`."""
        from fake_claustrum import FakeClaustrum

        self._sock_dir = Path(tempfile.mkdtemp(prefix="fc-app-"))
        self._fake = FakeClaustrum(str(self._sock_dir / "d.sock"), "tok", support_want_pid=True)
        self._client: ClaustrumClient | None = None
        self.ensured = False
        self.closed = False
        _LiveFakeDaemon.instances.append(self)

    @property
    def client(self) -> ClaustrumClient | None:
        """The connected client (None until :meth:`ensure`)."""
        return self._client

    @property
    def fake(self):
        """The underlying fake daemon (for test-side frame injection)."""
        return self._fake

    async def ensure(self) -> None:
        """Start the fake daemon and connect a real client on the running loop."""
        await self._fake.start()
        self._client = ClaustrumClient(self._fake.socket_path, self._fake.token)
        await self._client.connect()
        self.ensured = True

    async def aclose(self) -> None:
        """Close the client and stop the fake daemon."""
        self.closed = True
        try:
            if self._client is not None:
                await self._client.close()
        finally:
            try:
                # Always stop the fake daemon even if the client close raises, so a failed
                # close can't leak the daemon/socket and make later integration tests flaky.
                await self._fake.stop()
            finally:
                # Remove the mkdtemp socket dir so repeated runs don't accumulate /tmp dirs.
                shutil.rmtree(self._sock_dir, ignore_errors=True)


class _RaisingDaemon(_LiveFakeDaemon):
    """A live daemon whose ``aclose`` raises — to prove the lifespan still tears down."""

    async def aclose(self) -> None:
        """Tear down the real resources, then surface a shutdown fault."""
        await super().aclose()
        raise ClaustrumError("daemon aclose blew up during shutdown")


def _live_app(write_config, monkeypatch, tmp_path, *, daemon_cls=_LiveFakeDaemon):
    monkeypatch.setattr(app_module, "ClaustrumDaemon", daemon_cls)
    monkeypatch.setattr(app_module, "is_trusted", lambda *a, **k: True)
    monkeypatch.setattr(app_module.claude_cli, "resolve_binary", lambda b: "/usr/bin/claude")
    # Pin state_dir to the test tmp dir — the default is the SHARED ~/.clauster, and a
    # real HostedManager would read/persist hosted_state.json there, leaking state
    # across tests (and touching the live account's state dir). Isolate it.
    state_dir = tmp_path / "live-state"
    config = load_config(write_config(f"state_dir: {state_dir}\nclaustrum:\n  enabled: true\n"))
    app = create_app(config)
    return app


@_POSIX_ONLY
def test_lifespan_reattaches_and_closes_with_live_client(
    write_config, projects_root, monkeypatch, tmp_path
):
    # Audit #6: the app lifespan drives HostedManager.reattach_all on startup and
    # aclose on shutdown against a LIVE daemon client (real fake claustrum), not a
    # stub. With nothing persisted, reattach_all returns an empty registry; the
    # daemon connects on startup and closes on shutdown — both run without error.
    _LiveFakeDaemon.instances.clear()
    app = _live_app(write_config, monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/hosted").json() == []  # real manager, no sessions
    daemon = _LiveFakeDaemon.instances[-1]
    assert daemon.ensured and daemon.closed  # startup + shutdown both ran


@_POSIX_ONLY
def test_lifespan_tears_down_even_when_shutdown_raises(
    write_config, projects_root, monkeypatch, tmp_path
):
    # Audit #6 (fail-loud teardown): if the daemon's aclose RAISES on shutdown, the
    # lifespan must still surface it rather than swallow it into a misleading clean
    # exit — and the real resources are torn down first. The hosted manager's aclose
    # runs before the daemon's, so a daemon-shutdown fault never strands sessions.
    _RaisingDaemon.instances.clear()
    app = _live_app(write_config, monkeypatch, tmp_path, daemon_cls=_RaisingDaemon)
    with pytest.raises(ClaustrumError, match="aclose blew up"):
        with TestClient(app) as client:
            client.get("/api/hosted")
    daemon = _RaisingDaemon.instances[-1]
    assert daemon.closed  # real teardown happened before the fault surfaced


@_POSIX_ONLY
def test_spawn_hosted_drives_real_manager_end_to_end(
    write_config, projects_root, monkeypatch, tmp_path
):
    # Audit #29: a hosted spawn route driven against the REAL HostedManager (with a
    # fake claustrum underneath), not the stub — so route→_spawn_hosted→manager.spawn
    # →HostedSession.start→daemon process.spawn is covered as one wired path.
    _LiveFakeDaemon.instances.clear()
    app = _live_app(write_config, monkeypatch, tmp_path)
    with TestClient(app) as client:
        r = client.post("/api/instances", json={"project": "alpha", "channel": "hosted"})
        assert r.status_code == 201
        body = r.json()
        assert body["channel"] == "hosted"
        pid = body["claustrum_process_id"]
        # The real manager registered a live session and it surfaces on the list route.
        listed = client.get("/api/hosted").json()
        assert [h["claustrum_process_id"] for h in listed] == [pid]
        # The fake daemon actually received the stream-json spawn for this process.
        daemon = _LiveFakeDaemon.instances[-1]
        assert any(s["id"] == pid for s in daemon.fake.spawned)
        spawned = next(s for s in daemon.fake.spawned if s["id"] == pid)
        assert "--output-format" in spawned["args"] and "stream-json" in spawned["args"]
        # A message routes through the real manager to the real session's stdin.
        assert client.post(f"/api/instances/{pid}/message", json={"text": "hi"}).status_code == 202
