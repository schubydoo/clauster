from __future__ import annotations

import asyncio
import errno
import gc
import inspect
import json
import logging
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from clauster import procutil, supervisor
from clauster.app import create_app
from clauster.claude_cli import ClaudeNotFound
from clauster.config import CEILING_GATED_MODES, ClausterConfig, load_config
from clauster.routes import agents
from conftest import FAKE_CLAUDE


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


# Two parse failures that are not a JSONDecodeError: deeply-nested JSON raises
# RecursionError (not a ValueError at all) and a >4300-digit int literal raises a bare
# ValueError. Either used to escape these readers and fail the whole /api/agents list.
_UNPARSEABLE = [
    pytest.param("[" * 100_000, id="deeply-nested"),
    pytest.param("1" * 5000, id="oversized-int"),
]


@pytest.mark.parametrize("payload", _UNPARSEABLE)
def test_roster_unparseable_degrades_with_warning(tmp_path: Path, caplog, payload):
    roster = tmp_path / "roster.json"
    roster.write_text(payload, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        assert supervisor.load_roster_workers(roster) == {}
    assert "malformed roster" in caplog.text


def test_roster_non_utf8_degrades_with_warning(tmp_path: Path, caplog):
    # The read is its own try, and UnicodeDecodeError is a ValueError, not an OSError.
    roster = tmp_path / "roster.json"
    roster.write_bytes(b"\xff\xfe\x00not utf-8")
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        assert supervisor.load_roster_workers(roster) == {}
    assert "could not read" in caplog.text


@pytest.mark.parametrize("payload", _UNPARSEABLE)
def test_unparseable_state_json_skipped_others_kept(tmp_path: Path, caplog, payload):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bad", payload)
    _write_job(jobs, "good", _state())
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        result = supervisor.list_background_jobs(jobs, tmp_path / "r.json")
    assert [j.id for j in result] == ["good"]
    assert "skipping background job bad" in caplog.text


def test_worker_alive_rejects_garbage_pids():
    assert supervisor.worker_alive(None, "100") is False
    assert supervisor.worker_alive(True, "100") is False
    assert supervisor.worker_alive(-4, "100") is False
    assert supervisor.worker_alive(0, "100") is False


def test_worker_alive_bad_procstart_fails_closed(monkeypatch):
    calls: list = []
    monkeypatch.setattr(supervisor, "is_live_process", lambda *a, **k: calls.append(a) or True)
    assert supervisor.worker_alive(1234, "not-jiffies") is False
    assert supervisor.worker_alive(1234, None) is False
    assert supervisor.worker_alive(1234, 100.5) is False  # a float is not a tick count
    # Neither half comparable (no /proc ticks AND no boot time): never trust the bare pid.
    monkeypatch.setattr(supervisor, "proc_start_ticks", lambda pid: None)
    monkeypatch.setattr(supervisor, "jiffies_to_epoch", lambda j: None)
    assert supervisor.worker_alive(1234, "100") is False
    assert calls == []


def test_worker_alive_hands_procstart_over_as_ticks(monkeypatch):
    # An int procStart is passed as a STRING: is_live_process reads a number as our own
    # epoch create-time, which would compare jiffies against seconds since 1970.
    seen: list = []
    monkeypatch.setattr(
        supervisor, "is_live_process", lambda pid, ps, **k: seen.append((pid, ps, k)) or True
    )
    monkeypatch.setattr(supervisor, "proc_start_ticks", lambda pid: None)
    monkeypatch.setattr(supervisor, "jiffies_to_epoch", lambda j: 1000.0)
    assert supervisor.worker_alive(1234, 770579) is True
    assert seen == [(1234, "770579", {"tolerance": 2.0, "start_ticks": 770579})]


def _fake_worker(monkeypatch, *, ticks: int | None, create_time: float, btime: float) -> None:
    """Fake a live roster worker: psutil sees it, /proc reports ``ticks`` (CLK_TCK 100)."""

    class _Proc:
        def __init__(self, pid):
            pass

        def status(self):
            return procutil.psutil.STATUS_RUNNING

        def cmdline(self):
            return ["claude", "--bg"]

        def create_time(self):
            return create_time

    monkeypatch.setattr(procutil.psutil, "Process", _Proc)
    monkeypatch.setattr(procutil.psutil, "boot_time", lambda: btime)
    monkeypatch.setattr(procutil, "_clk_tck", lambda: 100)
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: ticks)
    monkeypatch.setattr(supervisor, "proc_start_ticks", lambda pid: ticks)


def test_worker_alive_survives_a_clock_step(monkeypatch):
    # TASK-504. psutil's Linux create_time is `starttime/CLK_TCK + btime`, and btime is
    # re-read on every call; an NTP step between the two reads moved one epoch 30s from the
    # other. The old epoch compare (2.0s) read this live worker as gone — and the stop path
    # then refused to signal it. The boot-relative ticks did not move.
    _fake_worker(monkeypatch, ticks=770579, create_time=1_007_705.79, btime=1_000_030.0)
    assert supervisor.worker_alive(4242, "770579") is True


def test_worker_alive_rejects_a_recycled_pid_on_ticks_even_when_epochs_agree(monkeypatch):
    # Same boot, same pid, a different process one tick later: the exact tick match is the
    # PID-reuse defense now, so an epoch inside the old 2.0s bound no longer authenticates it.
    _fake_worker(monkeypatch, ticks=770580, create_time=1_007_705.79, btime=1_000_000.0)
    assert supervisor.worker_alive(4242, "770579") is False


def test_worker_alive_without_ticks_keeps_the_epoch_bound(monkeypatch):
    # macOS / Windows (no /proc): their create-time is an absolute timestamp, so the epoch
    # compare with the 2.0s tolerance stays the identity check there.
    _fake_worker(monkeypatch, ticks=None, create_time=1_007_705.79, btime=1_000_001.0)
    assert supervisor.worker_alive(4242, "770579") is True  # 1.0s apart
    _fake_worker(monkeypatch, ticks=None, create_time=1_007_705.79, btime=1_000_010.0)
    assert supervisor.worker_alive(4242, "770579") is False  # 10s apart: a different process


@pytest.mark.skipif(sys.platform != "linux", reason="boot-relative ticks come from /proc")
def test_worker_alive_real_process_survives_a_btime_step(monkeypatch):
    # The real /proc and psutil path, on this test process: only the btime the epoch half
    # reads is stepped. psutil's own create_time keeps the unstepped value.
    pid = os.getpid()
    ticks = procutil.proc_start_ticks(pid)
    assert ticks is not None
    assert supervisor.worker_alive(pid, str(ticks)) is True
    real_btime = procutil.psutil.boot_time()
    monkeypatch.setattr(procutil.psutil, "boot_time", lambda: real_btime + 30)
    assert supervisor.worker_alive(pid, str(ticks)) is True
    assert supervisor.worker_alive(pid, str(ticks + 1)) is False


# TASK-534. Off Linux and Windows the CLI writes procStart as the trimmed output of
# `LC_ALL=C TZ=UTC ps -o lstart= -p <pid>`, not ticks (inferred from the dead non-Linux
# branch of the Linux CLI 2.1.319 bundle). Sun Nov 8 2026 19:08:12 UTC:
_LSTART = "Sun Nov  8 19:08:12 2026"
_LSTART_EPOCH = 1_794_164_892


@pytest.fixture
def on_macos(monkeypatch):
    """Pin darwin: the lstart branch is gated off on Linux, where create_time drifts."""
    monkeypatch.setattr(sys, "platform", "darwin")


def _lstart_text(epoch: int) -> str:
    """Format ``epoch`` the way `LC_ALL=C TZ=UTC ps -o lstart=` prints it, on any locale."""
    from datetime import UTC, datetime

    t = datetime.fromtimestamp(epoch, tz=UTC)
    day = procutil._LSTART_DAYS[t.weekday()]
    month = procutil._LSTART_MONTHS[t.month - 1]
    return f"{day} {month} {t.day:2d} {t:%H:%M:%S} {t.year}"


def test_lstart_to_epoch_reads_the_cli_format():
    assert _lstart_text(_LSTART_EPOCH) == _LSTART
    assert procutil.lstart_to_epoch(_LSTART) == _LSTART_EPOCH
    assert procutil.lstart_to_epoch(f"  {_LSTART}\n") == _LSTART_EPOCH  # untrimmed ps output
    assert procutil.lstart_to_epoch("Tue Nov 10 01:02:03 2026") == 1_794_272_523  # 2-digit day


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX-only")
def test_lstart_to_epoch_ignores_the_host_time_zone(monkeypatch):
    # The text is UTC (the CLI sets TZ=UTC), so the host zone must not shift the parse. CI
    # hosts run in UTC, where a naive local-time parse would also pass. A POSIX zone string, not
    # a tzdata name: the Alpine smoke-test image has no tzdata, so a name falls back to UTC.
    monkeypatch.setenv("TZ", "EST5")
    time.tzset()
    try:
        assert time.localtime(_LSTART_EPOCH).tm_hour != 19  # the zone really is not UTC
        assert procutil.lstart_to_epoch(_LSTART) == _LSTART_EPOCH
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "770579",  # Linux ticks
        "Mon Nov  8 19:08:12 2026",  # weekday does not match the date
        "Sun Feb 30 19:08:12 2026",  # no such day
        "Sun Nov  8 24:08:12 2026",  # no such hour
        "Sun Nov  8 19:08:12",  # no year
        "dim. nov.  8 19:08:12 2026",  # a localized ps, not LC_ALL=C
        "Sun Nov  8 19:08:12 2026 UTC",
        "Sun Nov  8 19:08:12.5 2026",
    ],
)
def test_lstart_to_epoch_rejects_anything_else(text):
    assert procutil.lstart_to_epoch(text) is None


@pytest.mark.parametrize("offset", [0.0, 0.73, 0.999999])
def test_worker_alive_reads_a_macos_lstart_procstart(monkeypatch, on_macos, offset):
    # The old code parsed procStart with int() and read every macOS worker as not alive.
    # The real proc_create_time path runs here, over a faked psutil.Process. Any instant
    # inside the named second matches: ps prints only the whole second.
    _fake_worker(monkeypatch, ticks=None, create_time=_LSTART_EPOCH + offset, btime=0.0)
    assert supervisor.worker_alive(4242, _LSTART) is True


@pytest.mark.parametrize(
    "offset",
    [
        pytest.param(1.0, id="the-next-second"),
        pytest.param(1.02, id="just-after-the-next-second"),
        pytest.param(3600.2, id="an-hour-later"),
        pytest.param(-0.01, id="just-before-the-second"),
        pytest.param(-0.98, id="early-in-the-previous-second"),
    ],
)
def test_worker_alive_rejects_a_recycled_macos_pid(monkeypatch, on_macos, offset):
    # Same pid, a process that started in another second: not the recorded worker.
    _fake_worker(monkeypatch, ticks=None, create_time=_LSTART_EPOCH + offset, btime=0.0)
    assert supervisor.worker_alive(4242, _LSTART) is False


def test_worker_alive_lstart_fails_closed_on_a_dead_pid(monkeypatch, on_macos):
    def _gone(pid):
        raise procutil.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(procutil.psutil, "Process", _gone)
    assert supervisor.worker_alive(4242, _LSTART) is False


def test_worker_alive_lstart_fails_closed_on_unreadable_text(monkeypatch, on_macos):
    # The pid is alive, but a start time that does not parse is no evidence of identity.
    _fake_worker(monkeypatch, ticks=None, create_time=_LSTART_EPOCH + 0.5, btime=0.0)
    assert supervisor.worker_alive(4242, "Sun Nov  8 19:08:12") is False
    assert supervisor.worker_alive(4242, "") is False


def test_worker_alive_lstart_fails_closed_on_a_zombie(monkeypatch, on_macos):
    class _Zombie:
        def __init__(self, pid):
            pass

        def status(self):
            return procutil.psutil.STATUS_ZOMBIE

        def create_time(self):
            return _LSTART_EPOCH + 0.5

    monkeypatch.setattr(procutil.psutil, "Process", _Zombie)
    assert supervisor.worker_alive(4242, _LSTART) is False


def test_worker_alive_lstart_never_reaches_the_tick_path(monkeypatch, on_macos):
    # The text is not handed to is_live_process, which reads an unparsable start time as
    # "no start time" and would then answer on alive alone.
    calls: list = []
    monkeypatch.setattr(supervisor, "is_live_process", lambda *a, **k: calls.append(a) or True)
    _fake_worker(monkeypatch, ticks=None, create_time=_LSTART_EPOCH + 9.0, btime=0.0)
    assert supervisor.worker_alive(4242, _LSTART) is False
    assert calls == []


def test_worker_alive_padded_ticks_stay_on_the_tick_path(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        supervisor, "is_live_process", lambda pid, ps, **k: seen.append((pid, ps, k)) or True
    )
    monkeypatch.setattr(supervisor, "proc_start_ticks", lambda pid: 770579)
    assert supervisor.worker_alive(1234, " 770579\n") is True
    assert seen == [(1234, "770579", {"tolerance": 2.0, "start_ticks": 770579})]


def test_worker_alive_ignores_lstart_text_on_linux(monkeypatch):
    # Linux create_time drifts with NTP, so frozen text must not be compared with it. The CLI
    # never writes lstart there; the text reads as not alive, as it did before TASK-534.
    monkeypatch.setattr(sys, "platform", "linux")
    _fake_worker(monkeypatch, ticks=None, create_time=_LSTART_EPOCH + 0.5, btime=0.0)
    assert supervisor.worker_alive(4242, _LSTART) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="the CLI writes lstart text on macOS")
def test_worker_alive_real_macos_ps_lstart():
    # The real thing on the macOS CI leg: the exact command the CLI runs, on this process.
    import shutil
    import subprocess

    ps = shutil.which("ps")
    assert ps is not None
    pid = os.getpid()
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    out = subprocess.run(  # noqa: S603 - absolute ps, list argv
        [ps, "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, env=env, check=True
    )
    text = out.stdout.strip()
    started = procutil.lstart_to_epoch(text)
    assert started is not None, text
    assert supervisor.worker_alive(pid, text) is True
    assert supervisor.worker_alive(pid, _lstart_text(started + 1)) is False
    assert supervisor.worker_alive(pid, _lstart_text(started - 1)) is False


def test_live_worker_joined_onto_job(tmp_path: Path, monkeypatch):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state())
    roster = _roster(tmp_path, {"bd1cea2f": {"pid": 4242, "procStart": "770579"}})
    _fake_worker(monkeypatch, ticks=770579, create_time=1_007_705.79, btime=1_000_000.0)
    [job] = supervisor.list_background_jobs(jobs, roster)
    assert job.worker_alive is True
    assert job.worker_pid == 4242


def test_stale_roster_worker_not_joined(tmp_path: Path, monkeypatch):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state())
    roster = _roster(tmp_path, {"bd1cea2f": {"pid": 4242, "procStart": "100"}})
    monkeypatch.setattr(supervisor, "is_live_process", lambda *a, **k: False)  # pid is gone
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
    assert supervisor.build_dispatch_argv(
        "/abs/claude", permission_mode="inherit", remote_control=False
    ) == [
        "/abs/claude",
        "--bg",
    ]
    assert supervisor.build_dispatch_argv(
        "/abs/claude",
        rc_name="proj-rc",
        model="opus",
        permission_mode="auto",
        remote_control=False,
        prompt="go",
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


def test_build_dispatch_argv_omits_permission_mode_for_inherit():
    # #1231: the dashboard's per-project picker feeds both the bridge launch and this
    # background dispatch, so the sentinel must drop the flag here too rather than hand
    # `claude --bg` a mode string it does not know.
    argv = supervisor.build_dispatch_argv(
        "/abs/claude", permission_mode="inherit", remote_control=False, prompt="go"
    )
    assert argv == ["/abs/claude", "--bg", "--", "go"]


@pytest.mark.parametrize(
    "fn",
    [supervisor.build_dispatch_argv, supervisor.dispatch_background_job],
    ids=lambda f: f.__name__,
)
def test_dispatch_permission_mode_is_keyword_required_with_no_default(fn):
    # TASK-481: a None default built the no-flag argv, which is `inherit` without the
    # caller's ceiling check. A missed call site must now be a type error, not that argv.
    param = inspect.signature(fn).parameters["permission_mode"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="permission_mode"):
        fn("/abs/claude")


def test_dispatch_rejects_a_none_permission_mode_before_trust(tmp_path, monkeypatch):
    # The runtime half of TASK-481: an untyped caller passing None fails closed instead of
    # dispatching the inherit argv.
    trusted, seen = _patch_dispatch(monkeypatch)
    with pytest.raises(supervisor.DispatchError, match="invalid permission mode: None"):
        supervisor.dispatch_background_job(
            tmp_path, prompt="hi", permission_mode=None, remote_control=False, before_spawn=None
        )
    assert trusted == []
    assert seen == {}


def test_build_dispatch_argv_dash_prompt_is_positional_after_separator():
    # a prompt that looks like a flag lands after `--`, so claude can't parse it as one
    assert supervisor.build_dispatch_argv(
        "/abs/claude", permission_mode="inherit", remote_control=False, prompt="--dangerously-skip"
    ) == [
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
        tmp_path,
        prompt="hi",
        rc_name="proj-rc",
        permission_mode="inherit",
        remote_control=False,
        binary="claude",
        claude_json=cj,
        before_spawn=None,
    )
    assert job_id == "29e8026f"
    assert trusted == [(tmp_path, cj)]  # cwd pre-trusted with the given claude.json
    assert seen["argv"] == ["/abs/claude", "--bg", "--rc", "proj-rc", "--", "hi"]
    assert seen["cwd"] == str(tmp_path)


def test_dispatch_default_claude_json_path(tmp_path, monkeypatch):
    trusted, _ = _patch_dispatch(monkeypatch)
    supervisor.dispatch_background_job(
        tmp_path, permission_mode="default", remote_control=False, before_spawn=None
    )
    assert trusted == [(tmp_path,)]  # no claude_json -> trust_directory called with cwd only


def test_dispatch_rejects_nondir_cwd(tmp_path, monkeypatch):
    _patch_dispatch(monkeypatch)
    with pytest.raises(supervisor.DispatchError, match="not a directory"):
        supervisor.dispatch_background_job(
            tmp_path / "nope", permission_mode="default", remote_control=False, before_spawn=None
        )


@pytest.mark.parametrize("field", ["rc_name", "model", "permission_mode"])
@pytest.mark.parametrize("bad", ["-x", 5, ["-x"]])
def test_dispatch_rejects_flag_like_or_nonstring_values(tmp_path, monkeypatch, field, bad):
    trusted, _ = _patch_dispatch(monkeypatch)
    with pytest.raises(supervisor.DispatchError, match="invalid"):
        supervisor.dispatch_background_job(
            tmp_path,
            **{"permission_mode": "default", "remote_control": False, field: bad},
            before_spawn=None,
        )
    assert trusted == []  # rejected before the trust write — no side effect


def test_dispatch_rejects_nonstring_prompt_before_trust(tmp_path, monkeypatch):
    trusted, _ = _patch_dispatch(monkeypatch)
    with pytest.raises(supervisor.DispatchError, match="invalid prompt"):
        supervisor.dispatch_background_job(
            tmp_path,
            prompt=["not", "a", "string"],
            permission_mode="default",
            remote_control=False,
            before_spawn=None,
        )
    assert trusted == []  # a malformed request must not trust the cwd then 500


def test_dispatch_nonzero_exit_raises_with_redacted_detail(tmp_path, monkeypatch):
    _patch_dispatch(monkeypatch, proc=_FakeProc(returncode=1, stderr="boom"))
    with pytest.raises(supervisor.DispatchError, match="exit 1.*boom"):
        supervisor.dispatch_background_job(
            tmp_path, permission_mode="default", remote_control=False, before_spawn=None
        )


def test_dispatch_no_id_raises(tmp_path, monkeypatch):
    _patch_dispatch(monkeypatch, proc=_FakeProc(stdout="all done, nothing here"))
    with pytest.raises(supervisor.DispatchError, match="no job id"):
        supervisor.dispatch_background_job(
            tmp_path, permission_mode="default", remote_control=False, before_spawn=None
        )


def test_dispatch_timeout_raises(tmp_path, monkeypatch):
    def boom(argv, **kw):
        raise supervisor.subprocess.TimeoutExpired(argv, kw.get("timeout"))

    _patch_dispatch(monkeypatch, run=boom)
    with pytest.raises(supervisor.DispatchError, match="timed out"):
        supervisor.dispatch_background_job(
            tmp_path, permission_mode="default", remote_control=False, timeout=5, before_spawn=None
        )


def test_dispatch_claude_not_found_propagates(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "resolve_binary", _raise_not_found)
    with pytest.raises(ClaudeNotFound):
        supervisor.dispatch_background_job(
            tmp_path, permission_mode="default", remote_control=False, before_spawn=None
        )


def _raise_not_found(_binary):
    raise ClaudeNotFound("nope")


def test_dispatch_unparseable_claude_json_raises_without_spawning(tmp_path, monkeypatch):
    # #1600: the real trust writer, on a truncated ~/.claude.json. It refuses to write, the
    # dispatch fails with the reason, the file is byte-identical and nothing is spawned.
    monkeypatch.setattr(supervisor, "resolve_binary", lambda b: "/abs/claude")
    spawned: list = []
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *a, **k: spawned.append(a))
    cj = tmp_path / "claude.json"
    content = b'{"projects": {"/keep": {"hasTrustDialogAccepted": true}}, "oauth'
    cj.write_bytes(content)
    cwd = tmp_path / "proj"
    cwd.mkdir()

    with pytest.raises(supervisor.DispatchError, match="could not trust .*not a valid JSON"):
        supervisor.dispatch_background_job(
            cwd,
            prompt="hi",
            permission_mode="default",
            remote_control=False,
            claude_json=cj,
            before_spawn=None,
        )

    assert cj.read_bytes() == content
    assert spawned == []


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

    supervisor.dispatch_background_job(
        tmp_path, permission_mode="default", remote_control=False, before_spawn=None
    )

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


def test_api_dispatch_agent_requires_prompt_when_unregistered(write_config, tmp_path, monkeypatch):
    # #1033: no prompt + no rc_name = a session that parks at "send a prompt to
    # start" forever (the bg card has no composer) -> 422, dispatch never reached
    called: list = []
    monkeypatch.setattr(supervisor, "dispatch_background_job", lambda *a, **k: called.append(1))
    for body in (
        {"project": "alpha"},
        {"project": "alpha", "prompt": ""},
        # whitespace-only must not bypass the gate (the dashboard trims; so must the API)
        {"project": "alpha", "prompt": "   "},
        # U+FEFF: JS trim() strips it, Python strip() does not — parity required
        {"project": "alpha", "prompt": "\ufeff \ufeff"},
    ):
        r = _client(write_config, tmp_path).post("/api/agents", json=body)
        assert r.status_code == 422
        assert "prompt is required" in r.json()["detail"]
    assert called == []


def test_api_dispatch_agent_blank_prompt_ok_when_cloud_registered(
    write_config, tmp_path, monkeypatch
):
    # with rc_name the session is conversational from claude.ai, so an empty
    # prompt legitimately means "no prompt" -> dropped to None, dispatch proceeds
    seen: dict = {}

    def fake(*a, **k):
        seen.update(k)
        return "deadbeef"

    monkeypatch.setattr(supervisor, "dispatch_background_job", fake)
    r = _client(write_config, tmp_path).post(
        "/api/agents", json={"project": "alpha", "prompt": "", "rc_name": "alpha-rc"}
    )
    assert r.status_code == 201
    assert seen["prompt"] is None


def test_api_dispatch_agent_maps_dispatch_error(write_config, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise supervisor.DispatchError("nope")

    monkeypatch.setattr(supervisor, "dispatch_background_job", boom)
    r = _client(write_config, tmp_path).post(
        "/api/agents", json={"project": "alpha", "prompt": "hi"}
    )
    assert r.status_code == 502


def test_api_dispatch_agent_maps_claude_not_found(write_config, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ClaudeNotFound("no claude on PATH")

    monkeypatch.setattr(supervisor, "dispatch_background_job", boom)
    r = _client(write_config, tmp_path).post(
        "/api/agents", json={"project": "alpha", "prompt": "hi"}
    )
    assert r.status_code == 503


# --- stop (BG-3) -----------------------------------------------------------

_JID = "2045a6c1"
_REAL_WORKER_ALIVE = supervisor.worker_alive  # _stop_setup replaces the module attribute


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
    # Pin POSIX so `settled` reflects the orderly double-SIGINT stop deterministically on any
    # host (on Windows the double-SIGINT hard-kills → settled=False; see the win32 test). (#914)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
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


def test_stop_signals_a_live_macos_worker(tmp_path, monkeypatch):
    # TASK-534: the real worker_alive, over a macOS lstart procStart. The worker is alive
    # until the second SIGINT and gone after it. Before the fix no SIGINT was ever sent.
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[], proc_start=_LSTART)
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(supervisor, "worker_alive", _REAL_WORKER_ALIVE)

    class _Worker:
        def __init__(self, pid):
            assert pid == 4242
            if len(kills) >= 2:
                raise procutil.psutil.NoSuchProcess(pid)

        def status(self):
            return procutil.psutil.STATUS_SLEEPING

        def create_time(self):
            return _LSTART_EPOCH + 0.5

    monkeypatch.setattr(procutil.psutil, "Process", _Worker)
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert kills == [(4242, signal.SIGINT), (4242, signal.SIGINT)]
    assert res["settled"] is True and res["removed"] is True


def test_stop_never_signals_a_recycled_macos_pid(tmp_path, monkeypatch):
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[], proc_start=_LSTART)
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(supervisor, "worker_alive", _REAL_WORKER_ALIVE)
    _fake_worker(monkeypatch, ticks=None, create_time=_LSTART_EPOCH + 7.0, btime=0.0)
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert kills == []
    assert res["settled"] is False


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


def test_stop_on_windows_is_hard_kill_not_a_clean_stop(tmp_path, monkeypatch):
    # On Windows os.kill(SIGINT) is a hard TerminateProcess (SIGINT isn't a console event), so
    # the worker is force-killed and the cloud session isn't deregistered — report settled=False
    # with an honest note, never a false clean stop (#914).
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[True, False])
    monkeypatch.setattr(supervisor.sys, "platform", "win32")  # override the setup's POSIX pin
    res = supervisor.stop_background_job(_JID, roster_json=roster)
    assert res["settled"] is False  # hard-killed, not an orderly cloud-deregistering stop
    assert res["removed"] is True
    assert "hard-killed on Windows" in res["detail"]


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


def test_stop_dead_worker_rm_hard_error_does_not_force_remove(tmp_path, monkeypatch):
    # A dead worker whose `claude rm` fails for a NON-soft-fail reason (hard error or
    # timeout — where the supervisor may still be alive and mid-remove) must NOT have its
    # dir force-dropped: the fallback is narrowed to the supervisor-down soft-fail so we
    # never race a still-working supervisor that could rewrite the dir (#485).
    jobs = tmp_path / "jobs"
    job_dir = _write_job(jobs, _JID, _state())
    rm = _FakeProc(returncode=1, stderr="boom: unexpected rm failure")  # no soft-fail phrase
    roster, kills = _stop_setup(monkeypatch, tmp_path, alive_seq=[False], rm=rm)
    res = supervisor.stop_background_job(_JID, roster_json=roster, jobs_dir=jobs)
    assert kills == []  # no live worker → never signalled
    assert res["removed"] is False  # not a soft-fail → force-forget fallback gated off
    assert job_dir.exists()  # record left intact (don't race a possibly-live supervisor)


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
    assert supervisor.build_dispatch_argv(
        "/abs/claude", permission_mode="inherit", remote_control=False, resume=_UUID
    ) == [
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
        supervisor.dispatch_background_job(
            tmp_path,
            permission_mode="default",
            remote_control=False,
            resume="--evil",
            before_spawn=None,
        )


def _fake_job(**kw):
    base = {
        "id": "29e8026f",
        "session_id": _UUID,
        "cwd": Path("/proj"),
        "worker_alive": False,
        "live": False,
        "bridge_session_id": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_resume_background_job_dispatches(tmp_path, monkeypatch):
    captured: dict = {}

    def _fake_dispatch(cwd, **kw):
        captured["cwd"], captured["kw"] = cwd, kw
        return "newjob01"  # resume mints a NEW 8-hex id

    job = _fake_job(cwd=tmp_path)  # a folder that exists (TASK-559)
    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: [job])
    monkeypatch.setattr(supervisor, "dispatch_background_job", _fake_dispatch)
    seen: list[Path] = []

    def _allow(cwd):
        seen.append(cwd)
        return None

    new_id = supervisor.resume_background_job(
        "29e8026f",
        permission_mode="plan",
        refuse_cwd=_allow,
        binary="/abs/claude",
        gate=supervisor.ResumeGate(),
    )
    assert new_id == "newjob01"
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["kw"]["resume"] == _UUID  # the FULL uuid, not the 8-hex id
    # TASK-335: the mode is passed explicitly (no flag would be the inherit argv), and the
    # caller's ceiling check saw the job's cwd before the dispatch.
    assert captured["kw"]["permission_mode"] == "plan"
    assert seen == [tmp_path.resolve()]


@pytest.mark.skipif(sys.platform == "win32", reason="os.symlink needs privilege on Windows")
def test_resume_dispatches_in_the_cwd_it_checked(tmp_path, monkeypatch):
    # TASK-481: the ceiling check resolved the recorded cwd, but the dispatch ran in the
    # recorded (symlinked) path, so a link swapped after the check moved the job into a
    # directory the check never saw. Both now get the one path resolved before the check.
    checked = tmp_path / "checked"
    swapped = tmp_path / "swapped"
    checked.mkdir()
    swapped.mkdir()
    link = tmp_path / "link"
    link.symlink_to(checked, target_is_directory=True)
    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: [_fake_job(cwd=link)])
    dispatched: list[Path] = []
    monkeypatch.setattr(
        supervisor,
        "dispatch_background_job",
        lambda cwd, **k: dispatched.append(cwd) or "newjob01",
    )

    def _check_then_swap(cwd):
        # The check is handed the already-resolved path, not the link: a check that resolved
        # the link itself could see a different target than the dispatch.
        assert cwd == checked.resolve()
        link.unlink()
        link.symlink_to(swapped, target_is_directory=True)  # swapped after the check
        return None

    supervisor.resume_background_job(
        "29e8026f",
        permission_mode="default",
        refuse_cwd=_check_then_swap,
        gate=supervisor.ResumeGate(),
    )
    assert dispatched == [checked.resolve()]


def test_resume_refuses_a_nul_byte_cwd_before_the_check(monkeypatch):
    # Refused on every OS: POSIX resolve() raises on a NUL, Windows returns it unresolved.
    job = _fake_job(cwd=Path("/proj/bad\x00name"))
    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: [job])
    checked: list = []
    with pytest.raises(supervisor.ResumeError, match="invalid working directory"):
        supervisor.resume_background_job(
            "29e8026f",
            permission_mode="default",
            refuse_cwd=checked.append,
            gate=supervisor.ResumeGate(),
        )
    assert checked == []


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_resume_refuses_a_cwd_that_is_no_longer_a_folder(tmp_path, monkeypatch, kind):
    # TASK-559: the non-strict resolve() accepts a missing path, so the dispatch raised
    # DispatchError (502, naming the path). It is a job that cannot be resumed: 409.
    cwd = tmp_path / "moved-away"
    if kind == "file":
        cwd.write_text("x")
    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: [_fake_job(cwd=cwd)])
    dispatched: list = []
    monkeypatch.setattr(
        supervisor, "dispatch_background_job", lambda *a, **k: dispatched.append(a) or "x"
    )
    with pytest.raises(supervisor.ResumeError) as info:
        supervisor.resume_background_job(
            "29e8026f",
            permission_mode="default",
            refuse_cwd=lambda cwd: None,
            gate=supervisor.ResumeGate(),
        )
    assert str(info.value) == "job '29e8026f' working directory no longer exists as a folder"
    assert not isinstance(info.value, supervisor.ResumeNotAllowed)
    assert dispatched == []


def test_resume_refuses_a_cwd_it_cannot_check(tmp_path, monkeypatch):
    # 3.11's is_dir() raises on an error other than "missing" (EACCES): the same 409 shape.
    monkeypatch.setattr(
        supervisor, "list_background_jobs", lambda *a, **k: [_fake_job(cwd=tmp_path)]
    )

    def _denied(self):
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr(Path, "is_dir", _denied)
    with pytest.raises(supervisor.ResumeError, match="working directory cannot be checked$"):
        supervisor.resume_background_job(
            "29e8026f",
            permission_mode="default",
            refuse_cwd=lambda cwd: None,
            gate=supervisor.ResumeGate(),
        )


def test_api_resume_agent_missing_cwd_is_409(write_config, tmp_path, projects_root, monkeypatch):
    # The route maps it to 409 with a detail that names neither the path nor the dispatch.
    gone = projects_root / "alpha" / "gone"
    r, captured = _resume_via_route(write_config, tmp_path, monkeypatch, cwd=gone)
    assert r.status_code == 409
    assert r.json() == {"detail": "job '29e8026f' working directory no longer exists as a folder"}
    assert captured == []


def test_resume_background_job_refused_cwd_never_dispatches(monkeypatch):
    dispatched: list = []
    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: [_fake_job()])
    monkeypatch.setattr(
        supervisor, "dispatch_background_job", lambda *a, **k: dispatched.append(a) or "x"
    )
    with pytest.raises(supervisor.ResumeNotAllowed, match="not enabled"):
        supervisor.resume_background_job(
            "29e8026f",
            permission_mode="inherit",
            refuse_cwd=lambda cwd: "not enabled",
            gate=supervisor.ResumeGate(),
        )
    assert dispatched == []


@pytest.mark.parametrize(
    "job,match",
    [
        (None, "no background job"),
        (_fake_job(worker_alive=True, live=True), "still live"),  # API mirrors the UI's gate
        (_fake_job(session_id=None), "no resumable session id"),
        (_fake_job(session_id="29e8026f"), "no resumable session id"),  # 8-hex is not a uuid
        (_fake_job(cwd=None), "no recorded working directory"),
    ],
)
def test_resume_background_job_errors(job, match, monkeypatch):
    jobs = [] if job is None else [job]
    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: jobs)
    with pytest.raises(supervisor.ResumeError, match=match):
        supervisor.resume_background_job(
            "29e8026f",
            permission_mode="default",
            refuse_cwd=lambda cwd: None,
            gate=supervisor.ResumeGate(),
        )


def test_api_resume_agent(write_config, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "resume_background_job", lambda *a, **k: "newjob01")
    r = _client(write_config, tmp_path).post("/api/agents/29e8026f/resume")
    assert r.status_code == 201
    assert r.json() == {"id": "newjob01"}


# TASK-335: a bg resume used to pass no --permission-mode at all, which is the inherit argv
# (a bypassPermissions default in user/managed settings takes effect). It now runs in the
# configured default mode, gated by the ceiling of the project holding the job's cwd.
def _resume_via_route(write_config, tmp_path, monkeypatch, *, cwd: Path, extra: str = ""):
    captured: list[dict] = []

    def _fake_dispatch(cwd, **kw):
        captured.append({"cwd": cwd, **kw})
        return "newjob01"

    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: [_fake_job(cwd=cwd)])
    monkeypatch.setattr(supervisor, "dispatch_background_job", _fake_dispatch)
    cfg = write_config(f"state_dir: {tmp_path}/.s\n{extra}")
    with TestClient(create_app(load_config(cfg))) as client:
        r = client.post("/api/agents/29e8026f/resume")
    return r, captured


_INHERIT_DEFAULT = "instance_defaults:\n  permission_mode: inherit\n"
_ALPHA_CEILING = "projects:\n  alpha:\n    allow_bypass_permissions: true\n"


def test_api_resume_agent_passes_the_default_mode(
    write_config, tmp_path, projects_root, monkeypatch
):
    r, captured = _resume_via_route(
        write_config, tmp_path, monkeypatch, cwd=projects_root / "alpha"
    )
    assert r.status_code == 201
    assert captured[0]["permission_mode"] == "default"  # an explicit flag, never "none"


@pytest.mark.parametrize(
    "extra",
    [_INHERIT_DEFAULT, "instance_defaults:\n  permission_mode: bypassPermissions\n"],
)
def test_api_resume_agent_gated_default_without_ceiling_is_403(
    write_config, tmp_path, projects_root, monkeypatch, extra
):
    r, captured = _resume_via_route(
        write_config, tmp_path, monkeypatch, cwd=projects_root / "alpha", extra=extra
    )
    assert r.status_code == 403
    assert "allow_bypass_permissions" in r.json()["detail"]
    assert captured == []  # refused before any dispatch


def test_api_resume_agent_inherit_with_ceiling_resumes(
    write_config, tmp_path, projects_root, monkeypatch
):
    # Positive control: the ceiling is the only thing the 403 above depends on. A job in a
    # subdirectory of the project (a worktree, say) is covered by that project's ceiling.
    (projects_root / "alpha" / "sub").mkdir()
    r, captured = _resume_via_route(
        write_config,
        tmp_path,
        monkeypatch,
        cwd=projects_root / "alpha" / "sub",
        extra=_INHERIT_DEFAULT + _ALPHA_CEILING,
    )
    assert r.status_code == 201
    assert captured[0]["permission_mode"] == "inherit"


def _outside(tmp_path: Path) -> Path:
    # projects_root IS tmp_path here, so "outside" is a sibling of it.
    return tmp_path.parent / f"elsewhere-{tmp_path.name}"


@pytest.mark.parametrize("mode", sorted(CEILING_GATED_MODES))
@pytest.mark.parametrize("where", ["outside", "root", "dotdot", "dotdot-missing", "sibling"])
def test_api_resume_agent_gated_mode_outside_the_ceiling_project_is_403(
    write_config, tmp_path, projects_root, monkeypatch, mode, where
):
    # Only alpha has the ceiling. A job that is not really in alpha must not borrow it:
    # not by its directory name, not at projects_root itself, and not by a `..` that
    # leads out of alpha (existing or not) — the mapping compares resolved paths.
    cwd = {
        "outside": _outside(tmp_path) / "alpha",
        "root": projects_root,
        "dotdot": projects_root / "alpha" / ".." / "beta",
        "dotdot-missing": projects_root / "alpha" / ".." / ".." / "gone" / "x",
        "sibling": projects_root / "beta",
    }[where]
    extra = f"instance_defaults:\n  permission_mode: {mode}\n" + _ALPHA_CEILING
    r, captured = _resume_via_route(write_config, tmp_path, monkeypatch, cwd=cwd, extra=extra)
    assert r.status_code == 403
    assert "allow_bypass_permissions: true" in r.json()["detail"]
    assert captured == []


def test_api_resume_agent_prefix_sibling_does_not_borrow(
    write_config, tmp_path, projects_root, monkeypatch
):
    # Containment is by path parts, not a string prefix: alpha-x is not inside alpha.
    (projects_root / "alpha-x").mkdir()
    r, captured = _resume_via_route(
        write_config,
        tmp_path,
        monkeypatch,
        cwd=projects_root / "alpha-x",
        extra=_INHERIT_DEFAULT + _ALPHA_CEILING,
    )
    assert r.status_code == 403
    assert captured == []


def test_api_resume_agent_nul_byte_cwd_is_409(write_config, tmp_path, projects_root, monkeypatch):
    # A NUL-byte cwd is refused before resolve() on every OS: never a 500. Since TASK-481
    # the resume resolves the cwd itself before the ceiling check, so it is a malformed
    # job (409) rather than a ceiling refusal (403).
    r, captured = _resume_via_route(
        write_config,
        tmp_path,
        monkeypatch,
        cwd=projects_root / "alpha" / "bad\x00name",
        extra=_INHERIT_DEFAULT + _ALPHA_CEILING,
    )
    assert r.status_code == 409
    assert captured == []


def test_projects_holding_refuses_nul_even_when_resolve_does_not_raise(
    write_config, tmp_path, projects_root, monkeypatch
):
    # Windows' non-strict resolve() returns a NUL path unresolved instead of raising, so
    # emulate that here: the guard, not the ValueError catch, must refuse it.
    from clauster.routes import agents as agents_module

    config = load_config(write_config(_ALPHA_CEILING))
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)
    assert agents_module._projects_holding(config, projects_root / "alpha" / "bad\x00name") == []


_A_ON = "  a:\n    allow_bypass_permissions: true\n"
_B_ON = "  b:\n    allow_bypass_permissions: true\n"
_B_OFF = "  b:\n    allow_bypass_permissions: false\n"


@pytest.mark.skipif(sys.platform == "win32", reason="os.symlink needs privilege on Windows")
@pytest.mark.parametrize(
    ("projects_yaml", "expected"),
    [
        # b (no ceiling) listed after / before a (ceiling): the order must not matter.
        (
            _A_ON + _B_OFF,
            403,
        ),
        (
            _B_OFF + _A_ON,
            403,
        ),
        # b with no config entry at all is a project with no ceiling, too.
        ("  a:\n    allow_bypass_permissions: true\n", 403),
        # Positive control: both ceilings on -> the overlap is allowed.
        (
            _A_ON + _B_ON,
            201,
        ),
    ],
)
def test_api_resume_agent_overlapping_projects_need_every_ceiling(
    write_config, tmp_path, projects_root, monkeypatch, projects_yaml, expected
):
    # projects_root/a is a symlink to projects_root/b/sub, so a job in b/sub/x is in both
    # projects: every one of them must allow the gated mode.
    (projects_root / "b" / "sub" / "x").mkdir(parents=True)
    (projects_root / "a").symlink_to(projects_root / "b" / "sub", target_is_directory=True)
    r, captured = _resume_via_route(
        write_config,
        tmp_path,
        monkeypatch,
        cwd=projects_root / "b" / "sub" / "x",
        extra=_INHERIT_DEFAULT + "projects:\n" + projects_yaml,
    )
    assert r.status_code == expected
    assert (captured == []) is (expected == 403)


def test_api_resume_agent_project_with_ceiling_off_names_the_project(
    write_config, tmp_path, projects_root, monkeypatch
):
    extra = _INHERIT_DEFAULT + "projects:\n  alpha:\n    allow_bypass_permissions: false\n"
    r, captured = _resume_via_route(
        write_config, tmp_path, monkeypatch, cwd=projects_root / "alpha", extra=extra
    )
    assert r.status_code == 403
    assert r.json() == {
        "detail": ClausterConfig.bypass_denied_detail("alpha", permission_mode="inherit")
    }
    assert captured == []


def test_api_resume_agent_ignores_a_traversal_project_key(
    write_config, tmp_path, projects_root, monkeypatch
):
    # A `projects:` key that is not a valid project name (here "..") must not make the
    # parent of projects_root a project whose ceiling covers everything under it.
    extra = _INHERIT_DEFAULT + "projects:\n  '..':\n    allow_bypass_permissions: true\n"
    r, captured = _resume_via_route(
        write_config, tmp_path, monkeypatch, cwd=_outside(tmp_path), extra=extra
    )
    assert r.status_code == 403
    assert captured == []


@pytest.mark.skipif(sys.platform == "win32", reason="os.symlink needs privilege on Windows")
def test_api_resume_agent_symlink_out_of_the_project_is_403(
    write_config, tmp_path, tmp_path_factory, projects_root, monkeypatch
):
    # A link inside alpha that points outside: the job really runs outside alpha.
    target = tmp_path_factory.mktemp("outside335")
    (projects_root / "alpha" / "link").symlink_to(target, target_is_directory=True)
    r, captured = _resume_via_route(
        write_config,
        tmp_path,
        monkeypatch,
        cwd=projects_root / "alpha" / "link",
        extra=_INHERIT_DEFAULT + _ALPHA_CEILING,
    )
    assert r.status_code == 403
    assert captured == []


@pytest.mark.skipif(sys.platform == "win32", reason="os.symlink needs privilege on Windows")
def test_api_resume_agent_symlinked_project_keeps_its_ceiling(
    write_config, tmp_path, tmp_path_factory, projects_root, monkeypatch
):
    # Positive control for the resolved comparison: a project whose directory is itself
    # a symlink still matches its own ceiling (both sides resolve to the same place).
    real = tmp_path_factory.mktemp("delta-real335")
    (projects_root / "delta").symlink_to(real, target_is_directory=True)
    extra = _INHERIT_DEFAULT + "projects:\n  delta:\n    allow_bypass_permissions: true\n"
    r, captured = _resume_via_route(
        write_config, tmp_path, monkeypatch, cwd=projects_root / "delta", extra=extra
    )
    assert r.status_code == 201
    assert captured[0]["permission_mode"] == "inherit"


@pytest.mark.skipif(sys.platform == "win32", reason="os.symlink needs privilege on Windows")
def test_api_resume_agent_prefix_of_a_symlinked_project_does_not_borrow(
    write_config, tmp_path, tmp_path_factory, projects_root, monkeypatch
):
    # Outside projects_root only the configured match decides, so pin that containment
    # is by path parts: <real>-2 shares <real>'s string prefix but is not inside it.
    real = tmp_path_factory.mktemp("delta-real335")
    sibling = real.parent / f"{real.name}-2"
    sibling.mkdir()
    (projects_root / "delta").symlink_to(real, target_is_directory=True)
    extra = _INHERIT_DEFAULT + "projects:\n  delta:\n    allow_bypass_permissions: true\n"
    r, captured = _resume_via_route(write_config, tmp_path, monkeypatch, cwd=sibling, extra=extra)
    assert r.status_code == 403
    assert captured == []


_SYMLINK_SKIP = pytest.mark.skipif(
    sys.platform == "win32", reason="os.symlink needs privilege on Windows"
)


@_SYMLINK_SKIP
@pytest.mark.parametrize("mode", sorted(CEILING_GATED_MODES))
@pytest.mark.parametrize("via", ["alias", "real", "alias-sub", "real-sub"])
def test_api_resume_agent_unconfigured_symlink_alias_denies(
    write_config, tmp_path, projects_root, monkeypatch, mode, via
):
    # projects_root/omega is an alias of alpha with no config entry, so no ceiling. A job
    # in that directory, or below it, is in omega too, whichever name its cwd uses.
    (projects_root / "alpha" / "sub").mkdir()
    (projects_root / "omega").symlink_to(projects_root / "alpha", target_is_directory=True)
    cwd = projects_root / ("omega" if via.startswith("alias") else "alpha")
    if via.endswith("-sub"):
        cwd = cwd / "sub"
    extra = f"instance_defaults:\n  permission_mode: {mode}\n" + _ALPHA_CEILING
    r, captured = _resume_via_route(write_config, tmp_path, monkeypatch, cwd=cwd, extra=extra)
    assert r.status_code == 403
    assert r.json() == {
        "detail": ClausterConfig.bypass_denied_detail("omega", permission_mode=mode)
    }
    assert captured == []


@_SYMLINK_SKIP
def test_api_resume_agent_configured_symlink_alias_with_ceiling_resumes(
    write_config, tmp_path, projects_root, monkeypatch
):
    # Positive control: give the alias its own ceiling and the same job resumes.
    (projects_root / "omega").symlink_to(projects_root / "alpha", target_is_directory=True)
    extra = _INHERIT_DEFAULT + _ALPHA_CEILING + "  omega:\n    allow_bypass_permissions: true\n"
    r, captured = _resume_via_route(
        write_config, tmp_path, monkeypatch, cwd=projects_root / "omega", extra=extra
    )
    assert r.status_code == 201
    assert captured[0]["permission_mode"] == "inherit"


@_SYMLINK_SKIP
@pytest.mark.parametrize("kind", ["loop", "dangling"])
def test_api_resume_agent_loop_or_dangling_link_cwd_is_refused(
    write_config, tmp_path, projects_root, monkeypatch, kind
):
    link = projects_root / "zeta"
    link.symlink_to(link if kind == "loop" else projects_root / "no-such-dir")
    r, captured = _resume_via_route(
        write_config, tmp_path, monkeypatch, cwd=link, extra=_INHERIT_DEFAULT + _ALPHA_CEILING
    )
    # 3.11/3.12 raise on a loop while resolving the recorded cwd, which is the 409 "cannot
    # be resolved" refusal; 3.13+ resolve it to a path that no ceiling covers (403).
    loop_raises = kind == "loop" and sys.version_info < (3, 13)
    assert r.status_code == (409 if loop_raises else 403)
    assert captured == []


def test_projects_holding_skips_a_looping_entry(write_config, projects_root, monkeypatch):
    # A looping entry under projects_root leads to no directory, so it cannot hold a job:
    # a job in alpha still maps to alpha alone. Path.resolve is forced to raise for the
    # entry, as 3.11/3.12 do on a loop, so no real symlink is needed (runs on Windows).
    from clauster.routes import agents as agents_module

    config = load_config(write_config(_ALPHA_CEILING))
    (projects_root / "zeta").mkdir()
    # Only a link entry is resolved (TASK-487): make zeta read as one.
    monkeypatch.setattr(agents_module, "_is_link_entry", lambda entry: entry.name == "zeta")
    real_resolve = Path.resolve

    def _resolve(self, strict=False):
        if self.name == "zeta":
            raise RuntimeError("Symlink loop")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert agents_module._projects_holding(config, projects_root / "alpha") == ["alpha"]


def _unlistable(monkeypatch, projects_root: Path) -> None:
    # Listing projects_root fails as an unreadable directory does, for the ceiling check
    # only: the app's own startup also lists this directory.
    from clauster.routes import agents as agents_module

    def _scandir(path):
        assert Path(path) == projects_root
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(agents_module, "os", SimpleNamespace(name=os.name, scandir=_scandir))


def test_projects_holding_unreadable_projects_root_raises(
    write_config, projects_root, monkeypatch
):
    # A listing failure is not "in no project" (TASK-487): it raises so the refusal names it.
    from clauster.routes import agents as agents_module

    config = load_config(write_config(_ALPHA_CEILING))
    _unlistable(monkeypatch, projects_root)
    with pytest.raises(
        agents_module._ProjectsRootUnlistable, match=r"^PermissionError: Permission denied$"
    ):
        agents_module._projects_holding(config, projects_root / "alpha")


def test_projects_holding_other_link_resolve_error_matches_nothing(
    write_config, projects_root, monkeypatch
):
    # Any resolve error on a link entry other than a loop refuses the resume (fail closed).
    from clauster.routes import agents as agents_module

    config = load_config(write_config(_ALPHA_CEILING))
    (projects_root / "zeta").mkdir()
    monkeypatch.setattr(agents_module, "_is_link_entry", lambda entry: entry.name == "zeta")
    real_resolve = Path.resolve

    def _resolve(self, strict=False):
        if self.name == "zeta":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert agents_module._projects_holding(config, projects_root / "alpha") == []


def test_api_resume_agent_unlistable_projects_root_names_the_error(
    write_config, tmp_path, projects_root, monkeypatch
):
    # Still refused (fail closed), but the 403 names the listing error, not "in no project",
    # and not the path: str(OSError) would have put it in the detail.
    _unlistable(monkeypatch, projects_root)
    r, captured = _resume_via_route(
        write_config,
        tmp_path,
        monkeypatch,
        cwd=projects_root / "alpha",
        extra=_INHERIT_DEFAULT + _ALPHA_CEILING,
    )
    assert r.status_code == 403
    assert r.json() == {
        "detail": "inherit is not enabled for this background job: Clauster could not list "
        "projects_root to find the project that holds its directory (PermissionError: "
        "Permission denied)."
    }
    assert captured == []


def test_projects_holding_never_resolves_a_plain_directory_entry(
    write_config, projects_root, monkeypatch
):
    # TASK-487: resolving a plain directory touches it, and a hung network mount there would
    # block the check. Its resolved path is <root>/<name>, which the first-part check covers.
    from clauster.routes import agents as agents_module

    config = load_config(write_config(_ALPHA_CEILING))
    resolved_names: list[str] = []
    real_resolve = Path.resolve

    def _resolve(self, strict=False):
        resolved_names.append(self.name)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert agents_module._projects_holding(config, projects_root / "alpha" / "x") == ["alpha"]
    # alpha is resolved once, as a config.projects key; beta and gamma are never touched.
    assert "beta" not in resolved_names
    assert "gamma" not in resolved_names
    assert resolved_names.count("alpha") == 1


def test_is_link_entry_reads_a_windows_reparse_point(monkeypatch):
    # A junction is not a symlink to is_symlink(), so on Windows the reparse-point attribute
    # from the listing decides. Emulated here so the branch runs on every OS.
    from clauster.routes import agents as agents_module

    def _entry(attributes: int) -> SimpleNamespace:
        info = SimpleNamespace(st_file_attributes=attributes)
        return SimpleNamespace(is_symlink=lambda: False, stat=lambda follow_symlinks: info)

    monkeypatch.setattr(agents_module, "os", SimpleNamespace(name="nt"))
    assert agents_module._is_link_entry(_entry(stat.FILE_ATTRIBUTE_REPARSE_POINT))
    assert not agents_module._is_link_entry(_entry(stat.FILE_ATTRIBUTE_DIRECTORY))


@pytest.mark.skipif(sys.platform != "win32", reason="a junction is a Windows reparse point")
def test_api_resume_agent_unconfigured_junction_alias_denies(
    write_config, tmp_path, projects_root, monkeypatch
):
    # The Windows twin of the symlink-alias test above: projects_root/omega is a junction to
    # alpha with no ceiling, so a job in alpha is in omega too and is refused.
    import _winapi  # type: ignore[import-not-found]

    _winapi.CreateJunction(str(projects_root / "alpha"), str(projects_root / "omega"))
    r, captured = _resume_via_route(
        write_config,
        tmp_path,
        monkeypatch,
        cwd=projects_root / "alpha",
        extra=_INHERIT_DEFAULT + _ALPHA_CEILING,
    )
    assert r.status_code == 403
    assert r.json() == {
        "detail": ClausterConfig.bypass_denied_detail("omega", permission_mode="inherit")
    }
    assert captured == []


def test_api_resume_agent_unresolvable_cwd_is_409(
    write_config, tmp_path, projects_root, monkeypatch
):
    # Python 3.11/3.12 raise RuntimeError on a symlink loop; any resolve failure fails closed.
    # The resume resolves the cwd before the ceiling check (TASK-481): a malformed job, 409.
    class _LoopPath(type(Path())):
        def resolve(self, strict=False):
            raise RuntimeError("Symlink loop")

    r, captured = _resume_via_route(
        write_config,
        tmp_path,
        monkeypatch,
        cwd=_LoopPath(projects_root / "alpha"),
        extra=_INHERIT_DEFAULT + _ALPHA_CEILING,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "job '29e8026f' working directory cannot be resolved"
    assert captured == []


def test_api_resume_agent_outside_projects_explicit_mode_resumes(
    write_config, tmp_path, monkeypatch
):
    # A non-gated default forces its own flag, so the ceiling does not apply to it.
    _outside(tmp_path).mkdir()
    r, captured = _resume_via_route(write_config, tmp_path, monkeypatch, cwd=_outside(tmp_path))
    assert r.status_code == 201
    assert captured[0]["permission_mode"] == "default"


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


# TASK-487: the whole resume runs under one time limit. A worker thread blocked on a hung
# mount cannot be stopped, so the request returns a 504 and the thread, when it wakes up,
# must not dispatch. Events, not sleeps, decide every order below: a 0.05 s limit only ever
# passes while the worker is blocked on an event that the test releases later.


def test_resume_gate_first_decision_wins():
    undecided = supervisor.ResumeGate()
    undecided.check()  # a check decides nothing
    committed = supervisor.ResumeGate()
    committed.commit()
    committed.check()
    assert committed.abandon() is False  # the dispatch started: the request must wait
    committed.commit()  # the same decision again is not an error
    abandoned = supervisor.ResumeGate()
    assert abandoned.abandon() is True
    assert abandoned.abandon() is True
    with pytest.raises(supervisor.ResumeError, match="gave up"):
        abandoned.check()
    with pytest.raises(supervisor.ResumeError, match="gave up"):
        abandoned.commit()


def test_dispatch_runs_before_spawn_after_the_trust_write(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(supervisor, "resolve_binary", lambda b: "/abs/claude")
    monkeypatch.setattr(supervisor, "trust_directory", lambda *a: calls.append("trust"))
    monkeypatch.setattr(
        supervisor.subprocess,
        "run",
        lambda argv, **kw: calls.append("spawn") or _FakeProc(stdout=_BANNER),
    )
    supervisor.dispatch_background_job(
        tmp_path,
        permission_mode="default",
        remote_control=False,
        before_spawn=lambda: calls.append("gate"),
    )
    assert calls == ["trust", "gate", "spawn"]


def test_resume_into_an_abandoned_gate_never_trusts_or_spawns(tmp_path, monkeypatch):
    trusted, seen = _patch_dispatch(monkeypatch)
    monkeypatch.setattr(
        supervisor, "list_background_jobs", lambda *a, **k: [_fake_job(cwd=tmp_path)]
    )
    gate = supervisor.ResumeGate()
    assert gate.abandon()
    with pytest.raises(supervisor.ResumeError, match="gave up"):
        supervisor.resume_background_job(
            "29e8026f", permission_mode="default", refuse_cwd=lambda cwd: None, gate=gate
        )
    assert trusted == []  # stopped before the trust write
    assert seen == {}  # and started no process


def _bound_resume(monkeypatch, cwd: Path, *, run=None):
    """Route a resume of one job in ``cwd`` through the real dispatch, recording each spawn.

    Returns the trust writes, the spawned argvs, an event set when the worker's resume
    call returns or raises, and the list that receives the worker's error.
    """
    spawned: list[list[str]] = []

    def _run(argv, **kw):
        spawned.append(argv)
        return _FakeProc(stdout=_BANNER)

    # Only the dispatch reaches the fake. subprocess is one module, so on Windows the
    # state writes' `icacls` call (atomicio) went through it too and read as a spawn.
    real_run = supervisor.subprocess.run
    dispatch = run or _run

    def _only_dispatch(argv, **kw):
        if not argv or argv[0] != "/abs/claude":
            return real_run(argv, **kw)
        return dispatch(argv, **kw)

    trusted, _ = _patch_dispatch(monkeypatch, run=_only_dispatch)
    monkeypatch.setattr(supervisor, "list_background_jobs", lambda *a, **k: [_fake_job(cwd=cwd)])
    finished = threading.Event()
    errors: list[BaseException] = []
    real_resume = supervisor.resume_background_job

    def _resume(*args, **kwargs):
        try:
            return real_resume(*args, **kwargs)
        except BaseException as exc:
            errors.append(exc)
            raise
        finally:
            finished.set()

    monkeypatch.setattr(supervisor, "resume_background_job", _resume)
    return trusted, spawned, finished, errors


@pytest.mark.parametrize("hang_in", ["ceiling check", "trust write"])
def test_api_resume_agent_past_the_time_limit_is_504_and_never_dispatches(
    write_config, tmp_path, projects_root, monkeypatch, hang_in
):
    # Both places stand in for a resolve() on a hung network mount. A hang before the trust
    # write stops at the gate check; a hang in it stops at the claim before the spawn.
    # The limit can pass before the worker reaches the hang on a slow runner, so the
    # request's abandon waits until the worker is blocked there.
    hung = threading.Event()
    release = threading.Event()
    real_abandon = supervisor.ResumeGate.abandon

    def _abandon(self):
        assert hung.wait(30)
        return real_abandon(self)

    monkeypatch.setattr(supervisor.ResumeGate, "abandon", _abandon)
    monkeypatch.setattr(agents, "_RESUME_TIME_LIMIT", 0.05)
    trusted, spawned, finished, errors = _bound_resume(monkeypatch, projects_root / "alpha")
    if hang_in == "ceiling check":

        def _hung_check(config, cwd, permission_mode):
            hung.set()
            release.wait(30)
            return None

        monkeypatch.setattr(agents, "_resume_ceiling_refusal", _hung_check)
    else:

        def _hung_trust(*args):
            hung.set()
            release.wait(30)
            trusted.append(args)

        monkeypatch.setattr(supervisor, "trust_directory", _hung_trust)
    try:
        # No lifespan: its poll loop runs `claude agents --json` through the patched run().
        r = _client(write_config, tmp_path).post("/api/agents/29e8026f/resume")
    finally:
        release.set()  # the blocked step now finishes, after the request gave up
    assert r.status_code == 504
    assert r.json() == {
        "detail": (
            "job '29e8026f' was not resumed: the checks of the job and its working directory "
            "took longer than 0.05 s. A network mount may be hung. Try again later."
        )
    }
    assert finished.wait(30)
    assert spawned == []
    assert len(trusted) == (0 if hang_in == "ceiling check" else 1)
    assert len(errors) == 1
    assert isinstance(errors[0], supervisor.ResumeError)
    assert str(errors[0]) == "the request gave up this resume (time limit or cancel)"


def test_api_resume_agent_waits_for_a_dispatch_that_started_before_the_limit(
    write_config, tmp_path, projects_root, monkeypatch
):
    # The thread claimed the gate first, so the process is starting: the request cannot say
    # "not resumed". It waits for the dispatch. The limit may pass before the claim on a
    # slow runner, so the request's abandon waits until the claim happened.
    committed = threading.Event()
    abandon_tried = threading.Event()
    real_commit = supervisor.ResumeGate.commit
    real_abandon = supervisor.ResumeGate.abandon

    def _commit(self):
        real_commit(self)
        committed.set()

    def _abandon(self):
        assert committed.wait(30)
        try:
            return real_abandon(self)
        finally:
            abandon_tried.set()

    monkeypatch.setattr(supervisor.ResumeGate, "commit", _commit)
    monkeypatch.setattr(supervisor.ResumeGate, "abandon", _abandon)
    spawned: list[list[str]] = []

    def _slow_run(argv, **kw):
        assert abandon_tried.wait(30)  # the limit passes while the process runs
        spawned.append(argv)
        return _FakeProc(stdout=_BANNER)

    monkeypatch.setattr(agents, "_RESUME_TIME_LIMIT", 0.05)
    _bound_resume(monkeypatch, projects_root / "alpha", run=_slow_run)
    # No lifespan: its poll loop runs `claude agents --json` through the same patched run().
    r = _client(write_config, tmp_path).post("/api/agents/29e8026f/resume")
    assert r.status_code == 201
    assert r.json() == {"id": "29e8026f"}
    assert len(spawned) == 1


def test_api_resume_agent_timeout_error_from_the_work_is_not_the_limit(
    write_config, tmp_path, monkeypatch
):
    # An OSError with ETIMEDOUT is a TimeoutError too. It is not the limit, so it must not
    # turn into the 504 that says the resume was abandoned.
    def _boom(*a, **k):
        raise TimeoutError(errno.ETIMEDOUT, "Connection timed out")

    monkeypatch.setattr(supervisor, "resume_background_job", _boom)
    with pytest.raises(TimeoutError, match="Connection timed out"):
        _client(write_config, tmp_path).post("/api/agents/29e8026f/resume")


def _worker(job_id: str) -> threading.Thread:
    return next(t for t in threading.enumerate() if t.name == f"clauster-resume-{job_id}")


def test_resume_within_limit_thread_that_outlives_its_event_loop_is_logged(caplog, monkeypatch):
    monkeypatch.setattr(agents, "_RESUME_TIME_LIMIT", 0.05)
    release = threading.Event()

    def _work(gate):
        release.wait(30)
        gate.commit()  # raises: the request abandoned the gate
        return "never"

    try:
        with pytest.raises(HTTPException) as info:
            asyncio.run(agents._resume_within_limit(_work, job_id="0000d00d"))
        assert info.value.status_code == 504
        worker = _worker("0000d00d")
        assert worker.daemon  # a hung one must not hold up the process exit
        with caplog.at_level(logging.DEBUG, logger="clauster.routes.agents"):
            release.set()
            worker.join(30)
    finally:
        release.set()
    assert not worker.is_alive()
    assert "0000d00d finished after its event loop closed" in caplog.text


async def test_resume_within_limit_drops_a_late_result_while_the_loop_runs(monkeypatch):
    # In the server the event loop outlives the request. The late thread's hand-back then
    # reaches the outcome the request cancelled, and must not raise in the loop.
    monkeypatch.setattr(agents, "_RESUME_TIME_LIMIT", 0.05)
    loop_errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: loop_errors.append(ctx))
    release = threading.Event()

    def _work(gate):
        release.wait(30)
        gate.commit()  # raises: the request abandoned the gate
        return "never"

    try:
        with pytest.raises(HTTPException) as info:
            await agents._resume_within_limit(_work, job_id="0000d00e")
        assert info.value.status_code == 504
        del info  # its traceback would keep a never-retrieved error alive past gc
        worker = _worker("0000d00e")  # found while it is surely still blocked
    finally:
        release.set()
    await asyncio.to_thread(worker.join, 30)
    for _ in range(3):
        await asyncio.sleep(0)  # run the hand-back the thread scheduled
    gc.collect()  # a future whose error nobody read reports it when it is collected
    assert loop_errors == []


async def test_resume_within_limit_cancelled_request_never_dispatches_later():
    # A request cancelled before the limit (a shutdown, say) must abandon the gate too: the
    # thread that wakes up later must not resume a job that no request reports.
    loop_errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: loop_errors.append(ctx))
    started = threading.Event()
    release = threading.Event()
    dispatched: list[str] = []
    errors: list[Exception] = []

    def _work(gate):
        started.set()
        release.wait(30)
        try:
            gate.commit()
        except supervisor.ResumeError as exc:
            errors.append(exc)
            raise
        dispatched.append("spawn")
        return "newjob01"

    task = asyncio.create_task(agents._resume_within_limit(_work, job_id="0000d00f"))
    try:
        assert await asyncio.to_thread(started.wait, 30)
        worker = _worker("0000d00f")  # found while it is surely still blocked
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    await asyncio.to_thread(worker.join, 30)
    assert dispatched == []
    assert [str(e) for e in errors] == ["the request gave up this resume (time limit or cancel)"]
    errors.clear()  # the list would keep the error alive past gc
    for _ in range(3):
        await asyncio.sleep(0)  # run the hand-back the thread scheduled
    gc.collect()  # the cancelled outcome drops the late error instead of leaving it unread
    assert loop_errors == []


async def test_resume_within_limit_cancel_after_the_claim_logs_the_dispatch_error(caplog):
    # The dispatch already started when the request was cancelled: it runs on, and its error
    # is logged by class (its text can name a path), so it is not lost or left unread.
    loop_errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: loop_errors.append(ctx))
    committed = threading.Event()
    release = threading.Event()

    def _work(gate):
        gate.commit()
        committed.set()
        release.wait(30)
        raise supervisor.DispatchError("`claude --bg` failed (exit 1): /secret/path")

    task = asyncio.create_task(agents._resume_within_limit(_work, job_id="0000d010"))
    with caplog.at_level(logging.WARNING, logger="clauster.routes.agents"):
        try:
            assert await asyncio.to_thread(committed.wait, 30)
            worker = _worker("0000d010")  # found while it is surely still blocked
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.to_thread(worker.join, 30)
        for _ in range(3):
            await asyncio.sleep(0)  # run the hand-back the thread scheduled
    gc.collect()
    assert loop_errors == []
    assert "0000d010 failed after its request ended: DispatchError" in caplog.text
    assert "/secret/path" not in caplog.text


async def test_log_unreported_failure_logs_only_a_failure(caplog):
    loop = asyncio.get_running_loop()
    callback = agents._log_unreported_failure("0000d011")
    ok, cancelled, failed = loop.create_future(), loop.create_future(), loop.create_future()
    ok.set_result("newjob01")
    cancelled.cancel()
    failed.set_exception(supervisor.DispatchError("boom"))
    with caplog.at_level(logging.WARNING, logger="clauster.routes.agents"):
        callback(ok)
        callback(cancelled)
        assert caplog.text == ""  # a dispatch that worked, or was dropped, is not a failure
        callback(failed)
    assert "0000d011 failed after its request ended: DispatchError" in caplog.text


class _Abort(BaseException):
    """A BaseException that is not SystemExit or KeyboardInterrupt."""


@pytest.mark.parametrize(
    "raised", [_Abort("stop"), SystemExit(3), KeyboardInterrupt(), asyncio.CancelledError()]
)
async def test_resume_within_limit_hands_back_a_base_exception_as_an_error(raised):
    # The request waits for a dispatch that claimed the gate with no limit of its own, so the
    # thread must hand back any exception, or the request would wait forever. One that is
    # not an Exception goes back as a RuntimeError: SystemExit or KeyboardInterrupt would
    # stop the event loop, and a CancelledError would read as a cancel of the request.
    def _work(gate):
        gate.commit()
        raise raised

    async with asyncio.timeout(30):  # only reached when the hand-back is missing
        with pytest.raises(RuntimeError) as info:
            await agents._resume_within_limit(_work, job_id="0000d012")
    assert str(info.value) == f"background resume thread raised {type(raised).__name__}"
    assert info.value.__cause__ is raised


async def test_resume_within_limit_cancel_while_waiting_for_a_claimed_dispatch_logs_it(
    caplog, monkeypatch
):
    # The limit passed after the dispatch claimed the gate, so the request waits for it. A
    # cancel then (a shutdown, say) leaves the dispatch running, and its failure is logged.
    monkeypatch.setattr(agents, "_RESUME_TIME_LIMIT", 0.05)
    loop_errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: loop_errors.append(ctx))
    committed = threading.Event()
    waiting = threading.Event()
    release = threading.Event()
    real_abandon = supervisor.ResumeGate.abandon

    def _abandon(self):
        assert committed.wait(30)
        try:
            return real_abandon(self)  # False: the dispatch claimed the gate first
        finally:
            waiting.set()  # the request now goes straight on to wait for the dispatch

    monkeypatch.setattr(supervisor.ResumeGate, "abandon", _abandon)

    def _work(gate):
        gate.commit()
        committed.set()
        release.wait(30)
        raise supervisor.DispatchError("`claude --bg` failed (exit 1): /secret/path")

    task = asyncio.create_task(agents._resume_within_limit(_work, job_id="0000d013"))
    with caplog.at_level(logging.WARNING, logger="clauster.routes.agents"):
        try:
            assert await asyncio.to_thread(waiting.wait, 30)
            worker = _worker("0000d013")  # found while it is surely still blocked
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.to_thread(worker.join, 30)
        for _ in range(3):
            await asyncio.sleep(0)  # run the hand-back the thread scheduled
    gc.collect()
    assert loop_errors == []
    assert "0000d013 failed after its request ended: DispatchError" in caplog.text
    assert "/secret/path" not in caplog.text


# ----- TASK-588: liveness of an ended worker, Resume with Remote Control -------------------
# Issue 1958. A worker that ended left `state: blocked` on disk, and the dashboard rule
# `worker_alive || state in (working, blocked)` kept the row live: only Stop showed, and
# Stop removes the job. The rule now lives in `BackgroundJob.live`.


@pytest.mark.parametrize("state", ["blocked", "working"])
def test_ended_worker_with_a_live_state_is_not_live(tmp_path: Path, state):
    # The shape seen in a sandbox with claude 2.1.332 (2026-11-25): the transient supervisor
    # exited and left a roster with no workers; the job kept its last state.
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state(state=state))
    roster = _roster(tmp_path, {})
    [job] = supervisor.list_background_jobs(jobs, roster)
    assert job.worker_alive is False
    assert job.worker_checked is True
    assert job.live is False


def test_dead_roster_pid_with_state_blocked_is_not_live(tmp_path: Path, monkeypatch):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state(state="blocked"))
    roster = _roster(tmp_path, {"bd1cea2f": {"pid": 4242, "procStart": "100"}})
    monkeypatch.setattr(supervisor, "is_live_process", lambda *a, **k: False)  # pid is gone
    [job] = supervisor.list_background_jobs(jobs, roster)
    assert (job.worker_checked, job.live) == (True, False)


def test_recycled_pid_with_state_working_is_not_live(tmp_path: Path, monkeypatch):
    # Same pid, another process one tick later: the pid+ticks match fails, so the worker is
    # gone, and the last `working` state does not bring the job back to life.
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state(state="working"))
    roster = _roster(tmp_path, {"bd1cea2f": {"pid": 4242, "procStart": "770579"}})
    _fake_worker(monkeypatch, ticks=770580, create_time=1_007_705.79, btime=1_000_000.0)
    [job] = supervisor.list_background_jobs(jobs, roster)
    assert (job.worker_alive, job.live) == (False, False)


def test_live_worker_with_a_stale_done_state_is_live(tmp_path: Path, monkeypatch):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state(state="done"))
    roster = _roster(tmp_path, {"bd1cea2f": {"pid": 4242, "procStart": "770579"}})
    _fake_worker(monkeypatch, ticks=770579, create_time=1_007_705.79, btime=1_000_000.0)
    [job] = supervisor.list_background_jobs(jobs, roster)
    assert (job.worker_alive, job.worker_checked, job.live) == (True, True, True)


@pytest.mark.parametrize(
    "roster_text",
    [None, "{not json", '{"proto": 1}', '{"workers": []}', "[]"],
    ids=["missing", "malformed", "no-workers-key", "workers-not-a-map", "not-an-object"],
)
@pytest.mark.parametrize("state,live", [("working", True), ("blocked", True), ("done", False)])
def test_without_a_roster_the_last_state_still_counts(tmp_path: Path, roster_text, state, live):
    # The case the old state clause protected: with no roster to check the worker against,
    # the last working/blocked state is the only sign that a worker runs.
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state(state=state))
    roster = tmp_path / "roster.json"
    if roster_text is not None:
        roster.write_text(roster_text, encoding="utf-8")
    [job] = supervisor.list_background_jobs(jobs, roster)
    assert job.worker_checked is False
    assert job.live is live


def test_read_roster_workers_tells_an_empty_roster_from_none(tmp_path: Path):
    assert supervisor.read_roster_workers(tmp_path / "absent.json") is None
    assert supervisor.read_roster_workers(_roster(tmp_path, {})) == {}
    # load_roster_workers keeps its old answer for both.
    assert supervisor.load_roster_workers(tmp_path / "absent.json") == {}
    assert supervisor.load_roster_workers(_roster(tmp_path, {})) == {}


def test_api_agents_serializes_live_and_worker_checked(write_config, tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    _write_job(jobs, "bd1cea2f", _state(state="blocked"))
    monkeypatch.setattr(supervisor, "JOBS_DIR", jobs)
    monkeypatch.setattr(supervisor, "ROSTER_JSON", _roster(tmp_path, {}))
    [item] = _client(write_config, tmp_path).get("/api/agents").json()
    assert (item["state"], item["worker_checked"], item["live"]) == ("blocked", True, False)


_TEMPLATES = Path(supervisor.__file__).parent / "templates"


def test_dashboard_reads_the_server_live_rule():
    # The dashboard has no JS test harness. Pin that each liveness use for a background row
    # reads `live`, and that no copy of the old state clause is left for agents.
    script = (_TEMPLATES / "_dashboard_script.html").read_text(encoding="utf-8")
    page = (_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert "agentLive(j) { return j.live === true; }" in script
    rail = script[script.index("agentRailClass(j) {") :]
    rail = rail[: rail.index("},")]
    assert 'if (!this.agentLive(j)) return "sess-dead";' in rail
    assert "includes(j.state)" not in script
    assert "includes(j.state)" not in page
    assert 'x-show="j.detail && agentLive(j)"' in page
    assert 'x-show="!agentLive(j) && j.session_id"' in page  # Resume only for an ended job


_SID = "bd1cea2f-19ef-4eef-97e9-bdd466105f17"  # the sessionId in _state()


def _resume_world(tmp_path: Path, *, state: dict, roster: dict | None) -> tuple[Path, Path]:
    jobs = tmp_path / "jobs"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_job(jobs, "bd1cea2f", _state(cwd=str(cwd), **state))
    roster_path = _roster(tmp_path, roster) if roster is not None else tmp_path / "absent.json"
    return jobs, roster_path


def _run_resume(monkeypatch, jobs, roster, *, stdout=_BANNER, stderr=""):
    _, seen = _patch_dispatch(monkeypatch, proc=_FakeProc(stdout=stdout, stderr=stderr))
    new_id = supervisor.resume_background_job(
        "bd1cea2f",
        permission_mode="plan",
        refuse_cwd=lambda cwd: None,
        gate=supervisor.ResumeGate(),
        binary="claude",
        jobs_dir=jobs,
        roster_json=roster,
    )
    return new_id, seen["argv"]


def test_resume_of_a_claude_ai_job_adds_remote_control(tmp_path: Path, monkeypatch):
    jobs, roster = _resume_world(
        tmp_path, state={"state": "blocked", "bridgeSessionId": "cse_01abc"}, roster={}
    )
    new_id, argv = _run_resume(monkeypatch, jobs, roster)
    assert new_id == "29e8026f"
    assert argv == [
        "/abs/claude",
        "--bg",
        "--resume",
        _SID,
        "--remote-control",
        "--permission-mode",
        "plan",
    ]


@pytest.mark.parametrize("bridge", [None, "", 5], ids=["absent", "empty", "not-a-string"])
def test_resume_of_a_local_job_stays_local(tmp_path: Path, monkeypatch, bridge):
    state: dict = {"state": "done"}
    if bridge is not None:
        state["bridgeSessionId"] = bridge
    jobs, roster = _resume_world(tmp_path, state=state, roster={})
    _, argv = _run_resume(monkeypatch, jobs, roster)
    assert argv == ["/abs/claude", "--bg", "--resume", _SID, "--permission-mode", "plan"]


def test_resume_without_a_roster_refuses_a_job_that_reads_blocked(tmp_path: Path, monkeypatch):
    # No roster to check: the last `blocked` state counts as live, in the API as in the UI.
    jobs, roster = _resume_world(tmp_path, state={"state": "blocked"}, roster=None)
    trusted, seen = _patch_dispatch(monkeypatch)
    with pytest.raises(supervisor.ResumeError, match="still live"):
        supervisor.resume_background_job(
            "bd1cea2f",
            permission_mode="plan",
            refuse_cwd=lambda cwd: None,
            gate=supervisor.ResumeGate(),
            jobs_dir=jobs,
            roster_json=roster,
        )
    assert seen == {}
    assert trusted == []


def test_resume_of_an_ended_job_without_a_session_id_still_refuses(tmp_path: Path, monkeypatch):
    jobs, roster = _resume_world(
        tmp_path, state={"state": "blocked", "sessionId": None}, roster={}
    )
    with pytest.raises(supervisor.ResumeError, match="no resumable session id"):
        _run_resume(monkeypatch, jobs, roster)


# What claude 2.1.332 printed for a resume of a session it read as running (sandbox,
# 2026-11-25): the note on stderr, the banner with the copy's id on stdout.
_COPY_NOTE = (
    "Starting background service…\n"
    "note: session bd1cea2f is already running in the background, so this started a copy "
    "as c59c2bec. `claude attach bd1cea2f` opens the original.\n"
)
_COPY_BANNER = "backgrounded · c59c2bec\n  claude agents             list sessions\n"


def test_resume_returns_the_copy_id_claude_started(tmp_path: Path, monkeypatch, caplog):
    jobs, roster = _resume_world(tmp_path, state={"state": "done"}, roster={})
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        new_id, _ = _run_resume(monkeypatch, jobs, roster, stdout=_COPY_BANNER, stderr=_COPY_NOTE)
    assert new_id == "c59c2bec"
    assert "started a copy as c59c2bec" in caplog.text


def test_resume_continuing_under_the_same_id_returns_it(tmp_path: Path, monkeypatch, caplog):
    same = "Starting background service…\nbackgrounded · bd1cea2f\n"
    jobs, roster = _resume_world(tmp_path, state={"state": "done"}, roster={})
    with caplog.at_level(logging.WARNING, logger="clauster.supervisor"):
        new_id, _ = _run_resume(monkeypatch, jobs, roster, stdout=same)
    assert new_id == "bd1cea2f"
    assert "copy" not in caplog.text


# The other copy note claude 2.1.332 printed in the same sandbox: a resume whose flags differ
# from the session's saved options (clauster passes --permission-mode, --remote-control).
_FLAGS_COPY_NOTE = (
    "note: background session bd1cea2f keeps its own saved options, so the flags you passed "
    "started a copy as f64d2d7a. Without flags, the same command continues bd1cea2f itself.\n"
)


def test_parse_job_id_reads_the_copy_id_not_the_original():
    # Without the banner, the bare fallback found the ORIGINAL id first in the copy note.
    assert supervisor.parse_job_id(_COPY_NOTE) == "c59c2bec"
    assert supervisor.parse_job_id(_FLAGS_COPY_NOTE) == "f64d2d7a"
    assert supervisor.parse_job_id(_COPY_NOTE + _COPY_BANNER) == "c59c2bec"
    assert supervisor.parse_job_id("started a copy as c59c2bec") == "c59c2bec"


@pytest.mark.parametrize(
    "note,expected", [(_COPY_NOTE, "c59c2bec"), (_FLAGS_COPY_NOTE, "f64d2d7a")]
)
def test_dispatch_reads_a_copy_note_on_stderr_when_stdout_is_empty(
    tmp_path, monkeypatch, note, expected
):
    _patch_dispatch(monkeypatch, proc=_FakeProc(stdout="", stderr=note))
    job_id = supervisor.dispatch_background_job(
        tmp_path, permission_mode="default", remote_control=False, before_spawn=None
    )
    assert job_id == expected


@pytest.mark.parametrize(
    "stderr",
    ["", "note: session bd1cea2f is already running in the background", "error deadbeef"],
)
def test_dispatch_never_takes_a_bare_id_from_stderr(tmp_path, monkeypatch, stderr):
    # Claude printing nothing on stdout, or an id-shaped token on stderr without the copy
    # note, fails closed: the token there can be the original job's id.
    _patch_dispatch(monkeypatch, proc=_FakeProc(stdout="", stderr=stderr))
    with pytest.raises(supervisor.DispatchError, match="printed no job id"):
        supervisor.dispatch_background_job(
            tmp_path, permission_mode="default", remote_control=False, before_spawn=None
        )


def test_build_dispatch_argv_remote_control():
    assert supervisor.build_dispatch_argv(
        "/abs/claude", permission_mode="inherit", remote_control=True, resume=_UUID
    ) == ["/abs/claude", "--bg", "--resume", _UUID, "--remote-control"]
    # A named --rc already opens the claude.ai door: no second flag.
    assert supervisor.build_dispatch_argv(
        "/abs/claude", rc_name="proj-rc", permission_mode="inherit", remote_control=True
    ) == ["/abs/claude", "--bg", "--rc", "proj-rc"]


@pytest.mark.parametrize(
    "fn", [supervisor.build_dispatch_argv, supervisor.dispatch_background_job]
)
def test_remote_control_is_keyword_required_with_no_default(fn):
    param = inspect.signature(fn).parameters["remote_control"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_api_dispatch_passes_no_remote_control(write_config, tmp_path, monkeypatch):
    seen: dict = {}

    def _fake(cwd, **kw):
        seen.update(kw)
        return "29e8026f"

    monkeypatch.setattr(supervisor, "dispatch_background_job", _fake)
    r = _client(write_config, tmp_path).post(
        "/api/agents", json={"project": "alpha", "prompt": "hi"}
    )
    assert r.status_code == 201, r.text
    assert seen["remote_control"] is False
