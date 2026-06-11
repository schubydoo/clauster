from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clauster import supervisor
from clauster.app import create_app
from clauster.claude_cli import ClaudeNotFound
from clauster.config import load_config

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _state(**overrides) -> dict:
    """A realistic state.json payload (captured shape, 2026-06-10, claude 2.1.170)."""
    base = {
        "state": "done",
        "detail": "counted to three as requested",
        "tempo": "idle",
        "inFlight": {"tasks": 0, "queued": 0, "kinds": []},
        "output": {"result": "One. Two. Three. DONE."},
        "children": None,
        "linkScanOffset": 32301,
        "linkScanPath": "/home/u/.claude/projects/-tmp-proj/bd1cea2f-19ef.jsonl",
        "template": "bg",
        "respawnFlags": ["--model", "sonnet"],
        "providerEnv": {},
        "intent": "Count to three slowly, then reply DONE.",
        "sessionId": "bd1cea2f-19ef-4eef-97e9-bdd466105f17",
        "resumeSessionId": "bd1cea2f-19ef-4eef-97e9-bdd466105f17",
        "daemonShort": "bd1cea2f",
        "cliVersion": "2.1.170",
        "cwd": "/tmp/proj",
        "createdAt": "2026-06-11T03:01:22.442Z",
        "updatedAt": "2026-06-11T03:01:34.068Z",
        "backend": "daemon",
        "name": "counting task completion",
        "nameSource": "auto",
    }
    base.update(overrides)
    return base


def _write_job(jobs_dir: Path, job_id: str, payload) -> Path:
    d = jobs_dir / job_id
    d.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (d / "state.json").write_text(text, encoding="utf-8")
    return d


# ----- list_background_jobs: directory handling ---------------------------


def test_missing_jobs_dir_returns_empty(tmp_path: Path):
    assert supervisor.list_background_jobs(tmp_path / "nope", tmp_path / "roster.json") == []


def test_jobs_dir_is_a_file_returns_empty_with_warning(tmp_path: Path, caplog):
    f = tmp_path / "jobs"
    f.write_text("not a dir")
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        assert supervisor.list_background_jobs(f, tmp_path / "r.json") == []
    assert "could not list" in caplog.text


def test_plain_files_like_pins_json_are_ignored(tmp_path: Path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "pins.json").write_text("{}")
    assert supervisor.list_background_jobs(jobs, tmp_path / "r.json") == []


def test_job_dir_without_state_json_skipped_silently(tmp_path: Path, caplog):
    jobs = tmp_path / "jobs"
    (jobs / "halfmade").mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        assert supervisor.list_background_jobs(jobs, tmp_path / "r.json") == []
    assert caplog.text == ""  # half-created dir is normal, not warn-worthy


# ----- list_background_jobs: parsing ---------------------------------------


def test_parses_realistic_state_json(tmp_path: Path):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state())
    [job] = supervisor.list_background_jobs(jobs, tmp_path / "r.json")
    assert job.id == "bd1cea2f"
    assert job.state == "done"
    assert job.detail == "counted to three as requested"
    assert job.tempo == "idle"
    assert job.needs is None
    assert job.result == "One. Two. Three. DONE."
    assert job.intent == "Count to three slowly, then reply DONE."
    assert job.name == "counting task completion"
    assert job.cwd == Path("/tmp/proj")
    # the bare-UUID redaction must NOT eat the structured resume handle
    assert job.session_id == "bd1cea2f-19ef-4eef-97e9-bdd466105f17"
    assert job.transcript_path == Path("/home/u/.claude/projects/-tmp-proj/bd1cea2f-19ef.jsonl")
    assert job.cli_version == "2.1.170"
    assert job.updated_at == "2026-06-11T03:01:34.068Z"
    assert job.bridge_session_id is None
    assert job.worker_alive is False
    assert job.worker_pid is None


def test_bridge_session_id_passthrough(tmp_path: Path):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "a1", _state(bridgeSessionId="cse_01BMQuL2BKaUg1xSpsTNe63Z"))
    [job] = supervisor.list_background_jobs(jobs, tmp_path / "r.json")
    assert job.bridge_session_id == "cse_01BMQuL2BKaUg1xSpsTNe63Z"


def test_malformed_state_json_skipped_with_warning_others_kept(tmp_path: Path, caplog):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bad", "{not json")
    _write_job(jobs, "good", _state())
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        result = supervisor.list_background_jobs(jobs, tmp_path / "r.json")
    assert [j.id for j in result] == ["good"]
    assert "skipping background job bad" in caplog.text


def test_non_object_state_json_skipped_with_warning(tmp_path: Path, caplog):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "scalar", "[1, 2, 3]")
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        assert supervisor.list_background_jobs(jobs, tmp_path / "r.json") == []
    assert "not an object" in caplog.text


def test_wrong_typed_fields_coerce_not_crash(tmp_path: Path):
    jobs = tmp_path / "jobs"
    _write_job(
        jobs,
        "odd",
        _state(state=5, detail=None, output="not-a-dict", cwd=12, sessionId=7, name=""),
    )
    [job] = supervisor.list_background_jobs(jobs, tmp_path / "r.json")
    assert job.state == ""
    assert job.detail == ""
    assert job.result is None
    assert job.cwd is None
    assert job.session_id is None
    assert job.name is None


def test_sorted_newest_updated_first(tmp_path: Path):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "older", _state(updatedAt="2026-06-11T01:00:00.000Z"))
    _write_job(jobs, "newer", _state(updatedAt="2026-06-11T02:00:00.000Z"))
    result = supervisor.list_background_jobs(jobs, tmp_path / "r.json")
    assert [j.id for j in result] == ["newer", "older"]


# ----- redaction ------------------------------------------------------------


def test_free_text_is_redacted_and_result_truncated(tmp_path: Path):
    jobs = tmp_path / "jobs"
    secret = "token sk-abcdefghijklmnopqrstuv and id cse_01G7Z3qpge1vjDv3dkqeKrue"
    _write_job(jobs, "leaky", _state(detail=secret, output={"result": "x" * 5000}))
    [job] = supervisor.list_background_jobs(jobs, tmp_path / "r.json")
    assert "sk-abcdefghijklmnopqrstuv" not in job.detail
    assert "cse_01G7Z3qpge1vjDv3dkqeKrue" not in job.detail
    assert job.result is not None
    assert len(job.result) <= supervisor._RESULT_MAX_CHARS + len(" …<truncated>")
    assert job.result.endswith("…<truncated>")


# ----- roster + worker liveness ---------------------------------------------


def _roster(tmp_path: Path, workers: dict) -> Path:
    p = tmp_path / "roster.json"
    p.write_text(json.dumps({"proto": 1, "supervisorPid": 1, "workers": workers}))
    return p


def test_roster_unreadable_warns(tmp_path: Path, caplog):
    unreadable = tmp_path / "roster-as-dir"
    unreadable.mkdir()  # read_text on a directory raises IsADirectoryError (an OSError)
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        assert supervisor.load_roster_workers(unreadable) == {}
    assert "could not read" in caplog.text


def test_roster_missing_corrupt_or_wrong_shape(tmp_path: Path, caplog):
    assert supervisor.load_roster_workers(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        assert supervisor.load_roster_workers(bad) == {}
    assert "malformed roster" in caplog.text
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"workers": [1, 2]}))
    assert supervisor.load_roster_workers(wrong) == {}
    scalar = tmp_path / "scalar.json"
    scalar.write_text("3")
    assert supervisor.load_roster_workers(scalar) == {}


def test_worker_alive_rejects_garbage_pids():
    assert supervisor.worker_alive(None, "100") is False
    assert supervisor.worker_alive(True, "100") is False
    assert supervisor.worker_alive(-4, "100") is False
    assert supervisor.worker_alive(0, "100") is False


def test_worker_alive_dead_pid_or_bad_procstart(monkeypatch):
    monkeypatch.setattr(supervisor, "proc_create_time", lambda pid: None)
    assert supervisor.worker_alive(1234, "100") is False
    monkeypatch.setattr(supervisor, "proc_create_time", lambda pid: 1000.0)
    assert supervisor.worker_alive(1234, "not-jiffies") is False
    assert supervisor.worker_alive(1234, None) is False
    monkeypatch.setattr(supervisor, "jiffies_to_epoch", lambda j: None)
    assert supervisor.worker_alive(1234, "100") is False


def test_worker_alive_requires_start_time_match(monkeypatch):
    monkeypatch.setattr(supervisor, "proc_create_time", lambda pid: 1000.0)
    monkeypatch.setattr(supervisor, "jiffies_to_epoch", lambda j: 1001.0)
    assert supervisor.worker_alive(1234, "100") is True  # within 2.0s tolerance
    monkeypatch.setattr(supervisor, "jiffies_to_epoch", lambda j: 1010.0)
    assert supervisor.worker_alive(1234, "100") is False  # recycled pid


def test_live_worker_joined_onto_job(tmp_path: Path, monkeypatch):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state())
    roster = _roster(tmp_path, {"bd1cea2f": {"pid": 4242, "procStart": "100"}})
    monkeypatch.setattr(supervisor, "proc_create_time", lambda pid: 1000.0)
    monkeypatch.setattr(supervisor, "jiffies_to_epoch", lambda j: 1000.5)
    [job] = supervisor.list_background_jobs(jobs, roster)
    assert job.worker_alive is True
    assert job.worker_pid == 4242


def test_stale_roster_worker_not_joined(tmp_path: Path, monkeypatch):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state())
    roster = _roster(tmp_path, {"bd1cea2f": {"pid": 4242, "procStart": "100"}})
    monkeypatch.setattr(supervisor, "proc_create_time", lambda pid: None)  # pid is gone
    [job] = supervisor.list_background_jobs(jobs, roster)
    assert job.worker_alive is False
    assert job.worker_pid is None


# ----- /api/agents endpoint --------------------------------------------------


def _client(write_config, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            load_config(
                write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n")
            )
        )
    )


def test_api_agents_lists_jobs(write_config, tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state())
    monkeypatch.setattr(supervisor, "JOBS_DIR", jobs)
    monkeypatch.setattr(supervisor, "ROSTER_JSON", tmp_path / "r.json")
    r = _client(write_config, tmp_path).get("/api/agents")
    assert r.status_code == 200
    [item] = r.json()
    assert item["id"] == "bd1cea2f"
    assert item["state"] == "done"
    assert item["worker_alive"] is False


def test_api_agents_empty_when_unused(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "JOBS_DIR", tmp_path / "absent")
    monkeypatch.setattr(supervisor, "ROSTER_JSON", tmp_path / "r.json")
    r = _client(write_config, tmp_path).get("/api/agents")
    assert r.status_code == 200
    assert r.json() == []


# --- dispatch (BG-2) -------------------------------------------------------

# The real `claude --bg` success banner (captured 2026-06-11); id == sessionId[:8].
_BANNER = "Starting background service…\nbackgrounded · 29e8026f\n  claude agents   list\n"


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_build_dispatch_argv_bare_and_full():
    assert supervisor.build_dispatch_argv("/abs/claude") == ["/abs/claude", "--bg"]
    assert supervisor.build_dispatch_argv(
        "/abs/claude", rc_name="proj-rc", model="opus", permission_mode="auto", prompt="go"
    ) == [
        "/abs/claude",
        "--bg",
        "--rc",
        "proj-rc",
        "--model",
        "opus",
        "--permission-mode",
        "auto",
        "--",
        "go",
    ]


def test_build_dispatch_argv_dash_prompt_is_positional_after_separator():
    # a prompt that looks like a flag lands after `--`, so claude can't parse it as one
    assert supervisor.build_dispatch_argv("/abs/claude", prompt="--dangerously-skip") == [
        "/abs/claude",
        "--bg",
        "--",
        "--dangerously-skip",
    ]


def test_parse_job_id_banner_ack_noise_and_fallback():
    assert supervisor.parse_job_id(_BANNER) == "29e8026f"
    # the dispatcher's internal retry line still carries the right id
    assert supervisor.parse_job_id("bg: ack-timeout for 8662a9aa, retrying (1/2)") == "8662a9aa"
    # no banner -> bare-hex fallback
    assert supervisor.parse_job_id("queued deadbeef ok") == "deadbeef"
    assert supervisor.parse_job_id("nothing id-shaped here") is None


def _patch_dispatch(monkeypatch, *, proc=None, run=None):
    """Stub out the three side-effecting deps of dispatch_background_job."""
    monkeypatch.setattr(supervisor, "resolve_binary", lambda b: "/abs/claude")
    trusted: list = []
    monkeypatch.setattr(supervisor, "trust_directory", lambda *a: trusted.append(a))
    seen: dict = {}

    def default_run(argv, **kw):
        seen["argv"] = argv
        seen["cwd"] = kw.get("cwd")
        return proc if proc is not None else _FakeProc(stdout=_BANNER)

    monkeypatch.setattr(supervisor.subprocess, "run", run or default_run)
    return trusted, seen


def test_dispatch_happy_pretrusts_and_returns_id(tmp_path, monkeypatch):
    trusted, seen = _patch_dispatch(monkeypatch)
    cj = tmp_path / "claude.json"
    job_id = supervisor.dispatch_background_job(
        tmp_path, prompt="hi", rc_name="proj-rc", binary="claude", claude_json=cj
    )
    assert job_id == "29e8026f"
    assert trusted == [(tmp_path, cj)]  # cwd pre-trusted with the given claude.json
    assert seen["argv"] == ["/abs/claude", "--bg", "--rc", "proj-rc", "--", "hi"]
    assert seen["cwd"] == str(tmp_path)


def test_dispatch_default_claude_json_path(tmp_path, monkeypatch):
    trusted, _ = _patch_dispatch(monkeypatch)
    supervisor.dispatch_background_job(tmp_path)
    assert trusted == [(tmp_path,)]  # no claude_json -> trust_directory called with cwd only


def test_dispatch_rejects_nondir_cwd(tmp_path, monkeypatch):
    _patch_dispatch(monkeypatch)
    with pytest.raises(supervisor.DispatchError, match="not a directory"):
        supervisor.dispatch_background_job(tmp_path / "nope")


@pytest.mark.parametrize("field", ["rc_name", "model", "permission_mode"])
@pytest.mark.parametrize("bad", ["-x", 5, ["-x"]])
def test_dispatch_rejects_flag_like_or_nonstring_values(tmp_path, monkeypatch, field, bad):
    trusted, _ = _patch_dispatch(monkeypatch)
    with pytest.raises(supervisor.DispatchError, match="invalid"):
        supervisor.dispatch_background_job(tmp_path, **{field: bad})
    assert trusted == []  # rejected before the trust write — no side effect


def test_dispatch_rejects_nonstring_prompt_before_trust(tmp_path, monkeypatch):
    trusted, _ = _patch_dispatch(monkeypatch)
    with pytest.raises(supervisor.DispatchError, match="invalid prompt"):
        supervisor.dispatch_background_job(tmp_path, prompt=["not", "a", "string"])
    assert trusted == []  # a malformed request must not trust the cwd then 500


def test_dispatch_nonzero_exit_raises_with_redacted_detail(tmp_path, monkeypatch):
    _patch_dispatch(monkeypatch, proc=_FakeProc(returncode=1, stderr="boom"))
    with pytest.raises(supervisor.DispatchError, match="exit 1.*boom"):
        supervisor.dispatch_background_job(tmp_path)


def test_dispatch_no_id_raises(tmp_path, monkeypatch):
    _patch_dispatch(monkeypatch, proc=_FakeProc(stdout="all done, nothing here"))
    with pytest.raises(supervisor.DispatchError, match="no job id"):
        supervisor.dispatch_background_job(tmp_path)


def test_dispatch_timeout_raises(tmp_path, monkeypatch):
    def boom(argv, **kw):
        raise supervisor.subprocess.TimeoutExpired(argv, kw.get("timeout"))

    _patch_dispatch(monkeypatch, run=boom)
    with pytest.raises(supervisor.DispatchError, match="timed out"):
        supervisor.dispatch_background_job(tmp_path, timeout=5)


def test_dispatch_claude_not_found_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "resolve_binary", _raise_not_found)
    with pytest.raises(ClaudeNotFound):
        supervisor.dispatch_background_job(tmp_path)


def _raise_not_found(_binary):
    raise ClaudeNotFound("nope")


def test_api_dispatch_agent_dispatches(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "dispatch_background_job", lambda *a, **k: "deadbeef")
    r = _client(write_config, tmp_path).post(
        "/api/agents", json={"project": "alpha", "prompt": "hi", "rc_name": "alpha-rc"}
    )
    assert r.status_code == 201
    assert r.json() == {"id": "deadbeef"}


def test_api_dispatch_agent_invalid_name(write_config, tmp_path):
    r = _client(write_config, tmp_path).post("/api/agents", json={"project": "bad name!"})
    assert r.status_code == 422


def test_api_dispatch_agent_unknown_project(write_config, tmp_path):
    r = _client(write_config, tmp_path).post("/api/agents", json={"project": "ghost"})
    assert r.status_code == 404


def test_api_dispatch_agent_rejects_nonstring_field(write_config, tmp_path, monkeypatch):
    # a non-string body field is a clean 422 at the boundary, never a 500 mid-spawn
    called: list = []
    monkeypatch.setattr(supervisor, "dispatch_background_job", lambda *a, **k: called.append(1))
    r = _client(write_config, tmp_path).post(
        "/api/agents", json={"project": "alpha", "rc_name": 5}
    )
    assert r.status_code == 422
    assert called == []  # dispatch never reached


def test_api_dispatch_agent_rejects_nonstring_project(write_config, tmp_path, monkeypatch):
    # a non-string `project` must 422, not coerce (123 -> "123") into a 404/dispatch
    called: list = []
    monkeypatch.setattr(supervisor, "dispatch_background_job", lambda *a, **k: called.append(1))
    r = _client(write_config, tmp_path).post("/api/agents", json={"project": 123})
    assert r.status_code == 422
    assert called == []


@pytest.mark.parametrize("field", ["rc_name", "model", "permission_mode"])
def test_api_dispatch_agent_rejects_present_empty_option(
    write_config, tmp_path, monkeypatch, field
):
    # an explicit empty rc_name/model/permission_mode is a caller mistake -> 422,
    # not silently dropped (which would dispatch a different session than intended)
    called: list = []
    monkeypatch.setattr(supervisor, "dispatch_background_job", lambda *a, **k: called.append(1))
    r = _client(write_config, tmp_path).post("/api/agents", json={"project": "alpha", field: ""})
    assert r.status_code == 422
    assert called == []


def test_api_dispatch_agent_empty_prompt_is_allowed(write_config, tmp_path, monkeypatch):
    # an empty prompt legitimately means "no prompt" -> dropped to None, dispatch proceeds
    seen: dict = {}

    def fake(*a, **k):
        seen.update(k)
        return "deadbeef"

    monkeypatch.setattr(supervisor, "dispatch_background_job", fake)
    r = _client(write_config, tmp_path).post(
        "/api/agents", json={"project": "alpha", "prompt": ""}
    )
    assert r.status_code == 201
    assert seen["prompt"] is None


def test_api_dispatch_agent_maps_dispatch_error(write_config, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise supervisor.DispatchError("nope")

    monkeypatch.setattr(supervisor, "dispatch_background_job", boom)
    r = _client(write_config, tmp_path).post("/api/agents", json={"project": "alpha"})
    assert r.status_code == 502


def test_api_dispatch_agent_maps_claude_not_found(write_config, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ClaudeNotFound("no claude on PATH")

    monkeypatch.setattr(supervisor, "dispatch_background_job", boom)
    r = _client(write_config, tmp_path).post("/api/agents", json={"project": "alpha"})
    assert r.status_code == 503
