from __future__ import annotations

import json
import logging
import signal
from pathlib import Path
from types import SimpleNamespace

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


def test_dispatch_trusts_before_spawning_subprocess(tmp_path, monkeypatch):
    # Ordering invariant (regression guard): the detached `claude --bg` cannot
    # answer the one-time trust dialog, so the cwd MUST be trusted BEFORE the
    # subprocess spawns — otherwise the worker wedges on the startup dialog. A
    # refactor that reorders these would silently reintroduce that wedge, so we
    # pin the relative order, not just that both happen.
    calls: list[str] = []
    monkeypatch.setattr(supervisor, "resolve_binary", lambda b: "/abs/claude")
    monkeypatch.setattr(supervisor, "trust_directory", lambda *a: calls.append("trust"))

    def _run(argv, **kw):
        calls.append("spawn")
        return _FakeProc(stdout=_BANNER)

    monkeypatch.setattr(supervisor.subprocess, "run", _run)

    supervisor.dispatch_background_job(tmp_path)

    assert calls == ["trust", "spawn"]  # trust strictly precedes the subprocess spawn


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


# --- stop (BG-3) -----------------------------------------------------------

_JID = "2045a6c1"


def test_valid_job_id():
    assert supervisor.valid_job_id("2045a6c1")
    assert supervisor.valid_job_id("16771706")  # all-digit is still 8-hex
    assert not supervisor.valid_job_id("DEADBEEF")  # uppercase rejected
    assert not supervisor.valid_job_id("short")
    assert not supervisor.valid_job_id("2045a6c1x")  # 9 chars
    assert not supervisor.valid_job_id("../etc/pw")
    assert not supervisor.valid_job_id(123)


def test_live_session_pid_fails_closed_on_nonint_pid(monkeypatch):
    # worker_alive (mocked True) can't make a non-int pid signalable
    monkeypatch.setattr(supervisor, "worker_alive", lambda p, ps, **k: True)
    assert supervisor._live_session_pid(_JID, {_JID: {"pid": "nope", "procStart": 1}}) is None


def _stop_setup(monkeypatch, tmp_path, *, alive_seq, rm=None, pid=4242, proc_start=999):
    """Wire stop_background_job's side-effecting deps; return (roster_path, kills)."""
    monkeypatch.setattr(supervisor, "resolve_binary", lambda b: "/abs/claude")
    roster = _roster(tmp_path, {_JID: {"pid": pid, "procStart": proc_start}})
    it = iter(alive_seq)
    monkeypatch.setattr(supervisor, "worker_alive", lambda p, ps, **k: next(it, False))
    kills: list = []
    monkeypatch.setattr(supervisor.os, "kill", lambda p, s: kills.append((p, s)))
    monkeypatch.setattr(supervisor.time, "sleep", lambda *_: None)
    proc = rm if rm is not None else _FakeProc(stdout="removed 2045a6c1")
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *a, **k: proc)
    return roster, kills


def test_stop_happy_double_sigint_polls_then_rm(tmp_path, monkeypatch):
    # validation True, re-validate True (-> 2nd SIGINT), one poll still alive
    # (exercises the settle sleep), then exited
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, True, True, False])
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert kills == [(4242, signal.SIGINT), (4242, signal.SIGINT)]  # double-SIGINT
    assert res == {"id": _JID, "settled": True, "removed": True, "detail": "removed 2045a6c1"}


def test_stop_no_live_worker_is_unconfirmed_not_clean(tmp_path, monkeypatch, caplog):
    # No validated-live worker: the cloud-deregistering double-SIGINT never runs, so we
    # CANNOT confirm the cloud session was deregistered. settled is False (not a false
    # clean stop), the job is still rm'd, the fail-open is logged, and detail says so.
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[False])
    with caplog.at_level("WARNING", logger="clauster.supervisor"):
        res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert kills == []  # nothing validated-live to signal
    assert res["settled"] is False and res["removed"] is True
    assert "stop not confirmed" in res["detail"]
    assert "cloud deregistration not confirmed" in caplog.text


def test_stop_job_absent_from_roster_just_rm(tmp_path, monkeypatch):
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[True])
    res = supervisor.stop_background_job("deadbeef", roster_json=roster)  # not in roster
    assert kills == []
    assert res["removed"] is True
    assert res["settled"] is False  # never tracked here → unconfirmed, not a clean stop


def test_stop_single_sigint_when_settles_during_gap(tmp_path, monkeypatch):
    # validation True, then re-validate False (exited in the gap) -> NO second kill
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, False])
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert kills == [(4242, signal.SIGINT)]  # second SIGINT skipped — pid may be recycled
    assert res["settled"] is True


def test_stop_not_settled_raises_without_force_kill(tmp_path, monkeypatch):
    # never exits: validation, re-validate (still alive -> 2nd kill), await still alive
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, True, True])
    with pytest.raises(supervisor.StopError, match="did not settle"):
        supervisor.stop_background_job(_JID, roster_json=roster, settle_timeout=0)
    assert kills == [(4242, signal.SIGINT), (4242, signal.SIGINT)]


def test_stop_process_already_exited_between_checks(tmp_path, monkeypatch):
    roster, _ = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, False])

    def gone(_p, _s):
        raise ProcessLookupError

    monkeypatch.setattr(supervisor.os, "kill", gone)
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert res["settled"] is True  # vanished == the goal


def test_stop_signal_oserror_raises(tmp_path, monkeypatch):
    roster, _ = _stop_setup(monkeypatch, tmp_path, alive_seq=[True])

    def denied(_p, _s):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(supervisor.os, "kill", denied)
    with pytest.raises(supervisor.StopError, match="could not signal"):
        supervisor.stop_background_job(_JID, roster_json=roster)


def test_stop_rm_soft_fail_reported_not_raised(tmp_path, monkeypatch):
    rm = _FakeProc(
        returncode=1, stderr="Couldn't confirm stopped — background service may be restarting"
    )
    roster, _ = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, False], rm=rm)
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert res["settled"] is True
    assert res["removed"] is False
    assert "couldn't confirm stopped" in res["detail"].lower()


def test_stop_rm_hard_error_reported(tmp_path, monkeypatch):
    rm = _FakeProc(returncode=2, stderr="boom")
    roster, _ = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, False], rm=rm)
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert res["removed"] is False and "boom" in res["detail"]


def test_stop_rm_timeout_reported(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise supervisor.subprocess.TimeoutExpired(a[0], 30)

    roster, _ = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, False])
    monkeypatch.setattr(supervisor.subprocess, "run", boom)
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert res["removed"] is False and "timed out" in res["detail"]


@pytest.mark.parametrize("bad", ["ghijklmn", "short", "DEADBEEF", "../x"])
def test_stop_invalid_job_id_raises(bad):
    with pytest.raises(supervisor.StopError, match="invalid job id"):
        supervisor.stop_background_job(bad)


# ----- stuck-orphan forget fallback (#485) -----------------------------------


def test_stop_dead_worker_rm_soft_fail_drops_local_job_dir(tmp_path, monkeypatch):
    # An ended (no-live-worker) agent whose `claude rm` soft-fails: clauster drops the
    # orphaned job dir itself so the row clears instead of sticking forever.
    jobs = tmp_path / "jobs"
    job_dir = _write_job(jobs, _JID, _state())
    rm = _FakeProc(returncode=1, stderr="Couldn't confirm stopped — service may be restarting")
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[False], rm=rm)
    res = supervisor.stop_background_job(_JID, roster_json=roster, jobs_dir=jobs)
    assert kills == []  # no live worker → never signalled
    assert res["removed"] is True  # the local fallback dropped the record
    assert res["settled"] is False  # cloud dereg still unconfirmed — surfaced, not hidden
    assert not job_dir.exists()
    assert "stop not confirmed" in res["detail"]  # cloud-orphan caveat preserved


def test_stop_live_worker_rm_soft_fail_does_not_force_remove(tmp_path, monkeypatch):
    # A LIVE worker that settles but whose `claude rm` soft-fails must NOT have its dir
    # yanked out from under it — the force-forget fallback is gated on a dead worker.
    jobs = tmp_path / "jobs"
    job_dir = _write_job(jobs, _JID, _state())
    rm = _FakeProc(returncode=1, stderr="Couldn't confirm stopped — service may be restarting")
    # alive_seq: validate live, re-validate exited (single SIGINT), then await-exit True.
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, False], rm=rm)
    res = supervisor.stop_background_job(_JID, roster_json=roster, jobs_dir=jobs)
    assert kills == [(4242, signal.SIGINT)]
    assert res["settled"] is True
    assert res["removed"] is False  # rm soft-failed and the fallback is gated off
    assert job_dir.exists()  # the live worker's record is left intact


def test_stop_dead_worker_rm_soft_fail_and_dir_fallback_also_fails(tmp_path, monkeypatch):
    # Belt-and-suspenders: if the local dir fallback ALSO can't drop the record, the stop
    # stays removed=False (surfaced, not silently flipped) so the caveat still reaches the UI.
    jobs = tmp_path / "jobs"
    _write_job(jobs, _JID, _state())
    rm = _FakeProc(returncode=1, stderr="Couldn't confirm stopped")
    roster, _ = _stop_setup(monkeypatch, tmp_path, alive_seq=[False], rm=rm)
    monkeypatch.setattr(supervisor, "_force_remove_job_dir", lambda *a: (False, "denied"))
    res = supervisor.stop_background_job(_JID, roster_json=roster, jobs_dir=jobs)
    assert res["removed"] is False and res["settled"] is False


def test_force_remove_job_dir_already_gone_reads_removed(tmp_path):
    removed, detail = supervisor._force_remove_job_dir(_JID, tmp_path / "jobs")
    assert removed is True and "already gone" in detail


def test_force_remove_job_dir_rejects_bad_id(tmp_path):
    removed, detail = supervisor._force_remove_job_dir("../etc", tmp_path)
    assert removed is False and detail == "invalid job id"


def test_force_remove_job_dir_oserror_reported_not_swallowed(tmp_path, monkeypatch, caplog):
    jobs = tmp_path / "jobs"
    _write_job(jobs, _JID, _state())

    def boom(_target):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(supervisor.shutil, "rmtree", boom)
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        removed, detail = supervisor._force_remove_job_dir(_JID, jobs)
    assert removed is False
    assert "could not drop local job record" in detail
    assert "could not force-remove orphaned job dir" in caplog.text


def test_force_remove_job_dir_defaults_to_module_jobs_dir(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    job_dir = _write_job(jobs, _JID, _state())
    monkeypatch.setattr(supervisor, "JOBS_DIR", jobs)
    removed, detail = supervisor._force_remove_job_dir(_JID, None)  # None → module constant
    assert removed is True and not job_dir.exists()
    assert "dropped orphaned local job record" in detail


def test_api_stop_agent_happy(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "stop_background_job",
        lambda jid, **k: {"id": jid, "settled": True, "removed": True, "detail": "removed"},
    )
    r = _client(write_config, tmp_path).delete(f"/api/agents/{_JID}")
    assert r.status_code == 200
    assert r.json() == {"id": _JID, "settled": True, "removed": True, "detail": "removed"}


def test_api_stop_agent_unconfirmed_is_200(write_config, tmp_path, monkeypatch):
    # A no-live-worker stop returns settled=False — served as 200 (not coerced to an
    # error), so the body's caveat reaches the UI instead of masking the fail-open.
    monkeypatch.setattr(
        supervisor,
        "stop_background_job",
        lambda jid, **k: {
            "id": jid,
            "settled": False,
            "removed": True,
            "detail": "stop not confirmed",
        },
    )
    r = _client(write_config, tmp_path).delete(f"/api/agents/{_JID}")
    assert r.status_code == 200
    assert r.json()["settled"] is False


def test_api_stop_agent_invalid_id(write_config, tmp_path):
    r = _client(write_config, tmp_path).delete("/api/agents/ghijklmn")
    assert r.status_code == 422


def test_api_stop_agent_not_settled_is_409(write_config, tmp_path, monkeypatch):
    def boom(jid, **k):
        raise supervisor.StopError("did not settle")

    monkeypatch.setattr(supervisor, "stop_background_job", boom)
    r = _client(write_config, tmp_path).delete(f"/api/agents/{_JID}")
    assert r.status_code == 409


def test_api_stop_agent_claude_not_found_is_503(write_config, tmp_path, monkeypatch):
    def boom(jid, **k):
        raise ClaudeNotFound("no claude")

    monkeypatch.setattr(supervisor, "stop_background_job", boom)
    r = _client(write_config, tmp_path).delete(f"/api/agents/{_JID}")
    assert r.status_code == 503


# ----- resume (BG-4, #336) ---------------------------------------------------

_UUID = "29e8026f-1234-4abc-8def-0123456789ab"  # a full RFC-4122 session UUID


def test_build_dispatch_argv_resume():
    assert supervisor.build_dispatch_argv("/abs/claude", resume=_UUID) == [
        "/abs/claude",
        "--bg",
        "--resume",
        _UUID,
    ]


def test_valid_session_id():
    assert supervisor.valid_session_id(_UUID)
    assert supervisor.valid_session_id(_UUID.upper())  # hex case-insensitive
    assert not supervisor.valid_session_id("29e8026f")  # the 8-hex job id is NOT a session id
    assert not supervisor.valid_session_id("--rc evil")  # argv-injection shape rejected
    assert not supervisor.valid_session_id(_UUID + "x")
    assert not supervisor.valid_session_id(None)
    assert not supervisor.valid_session_id(123)


def test_dispatch_rejects_invalid_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "resolve_binary", lambda b: "/abs/claude")
    monkeypatch.setattr(supervisor, "trust_directory", lambda *a, **k: None)
    with pytest.raises(supervisor.DispatchError, match="resume session id"):
        supervisor.dispatch_background_job(tmp_path, resume="--evil")


def _fake_job(**kw):
    base = {"id": "29e8026f", "session_id": _UUID, "cwd": Path("/proj"), "worker_alive": False}
    base.update(kw)
    return SimpleNamespace(**base)


def test_resume_background_job_dispatches(monkeypatch):
    captured: dict = {}

    def _fake_dispatch(cwd, **kw):
        captured["cwd"], captured["kw"] = cwd, kw
        return "newjob01"  # resume mints a NEW 8-hex id

    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: [_fake_job()])
    monkeypatch.setattr(supervisor, "dispatch_background_job", _fake_dispatch)
    new_id = supervisor.resume_background_job("29e8026f", binary="/abs/claude")
    assert new_id == "newjob01"
    assert captured["cwd"] == Path("/proj")
    assert captured["kw"]["resume"] == _UUID  # the FULL uuid, not the 8-hex id


@pytest.mark.parametrize(
    "job,match",
    [
        (None, "no background job"),
        (_fake_job(worker_alive=True), "still live"),  # API mirrors the UI's live gate
        (_fake_job(session_id=None), "no resumable session id"),
        (_fake_job(session_id="29e8026f"), "no resumable session id"),  # 8-hex is not a uuid
        (_fake_job(cwd=None), "no recorded working directory"),
    ],
)
def test_resume_background_job_errors(job, match, monkeypatch):
    jobs = [] if job is None else [job]
    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: jobs)
    with pytest.raises(supervisor.ResumeError, match=match):
        supervisor.resume_background_job("29e8026f")


def test_api_resume_agent(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "resume_background_job", lambda *a, **k: "newjob01")
    r = _client(write_config, tmp_path).post("/api/agents/29e8026f/resume")
    assert r.status_code == 201
    assert r.json() == {"id": "newjob01"}


def test_api_resume_agent_invalid_job_id(write_config, tmp_path):
    r = _client(write_config, tmp_path).post("/api/agents/BADID!/resume")
    assert r.status_code == 422


def test_api_resume_agent_not_resumable(write_config, tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise supervisor.ResumeError("no resumable session id")

    monkeypatch.setattr(supervisor, "resume_background_job", _boom)
    r = _client(write_config, tmp_path).post("/api/agents/29e8026f/resume")
    assert r.status_code == 409


def test_api_resume_agent_dispatch_error(write_config, tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise supervisor.DispatchError("bg failed")

    monkeypatch.setattr(supervisor, "resume_background_job", _boom)
    r = _client(write_config, tmp_path).post("/api/agents/29e8026f/resume")
    assert r.status_code == 502


def test_api_resume_agent_claude_not_found(write_config, tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise ClaudeNotFound("nope")

    monkeypatch.setattr(supervisor, "resume_background_job", _boom)
    r = _client(write_config, tmp_path).post("/api/agents/29e8026f/resume")
    assert r.status_code == 503
