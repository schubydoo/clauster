from __future__ import annotations

import os
import subprocess
import sys

import psutil
import pytest

from clauster import procutil


def test_is_bridge_cmdline_matches_real_bridge():
    assert procutil.is_bridge_cmdline(["claude", "remote-control", "--name", "x"]) is True
    assert procutil.is_bridge_cmdline(["python3", "/p/claude", "remote-control"]) is True


def test_is_bridge_cmdline_rejects_non_bridge():
    assert procutil.is_bridge_cmdline([]) is False
    assert procutil.is_bridge_cmdline(["claude", "agents", "--json"]) is False
    assert procutil.is_bridge_cmdline(["python3", "pytest"]) is False


def test_dead_pid_is_not_live():
    # A PID that almost certainly doesn't exist.
    assert procutil.is_live_bridge(2_000_000_000, None) is False


def test_current_process_is_not_a_bridge():
    # Alive, but cmdline is pytest/python — must fail the cmdline gate.
    assert procutil.is_live_bridge(os.getpid(), None) is False


def test_proc_create_time_of_self_is_float():
    ct = procutil.proc_create_time(os.getpid())
    assert isinstance(ct, float) and ct > 0


def test_jiffies_to_epoch_uses_boot_time():
    epoch = procutil.jiffies_to_epoch(0)
    assert epoch is not None
    assert abs(epoch - psutil.boot_time()) < 1.0


def test_zombie_status_treated_as_dead(monkeypatch):
    class FakeProc:
        def __init__(self, pid):
            pass

        def status(self):
            return psutil.STATUS_ZOMBIE

        def cmdline(self):
            return ["claude", "remote-control"]

        def create_time(self):
            return 123.0

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    assert procutil.is_live_bridge(1234, None) is False


def test_create_time_mismatch_rejected(monkeypatch):
    class FakeProc:
        def __init__(self, pid):
            pass

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return ["claude", "remote-control"]

        def create_time(self):
            return 1000.0

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    # A float proc_start is our OWN exact create_time, so it must match the same
    # process near-exactly (tight bound). A gap means the PID was recycled.
    assert procutil.is_live_bridge(1234, 5000.0) is False  # far off -> reuse
    assert procutil.is_live_bridge(1234, 1000.5) is False  # 0.5s off -> reuse (was True)
    assert procutil.is_live_bridge(1234, 1000.0) is True  # same measurement
    assert procutil.is_live_bridge(1234, 1000.02) is True  # hair of float jitter is fine


def test_jiffies_pointer_keeps_loose_tolerance(monkeypatch):
    # A pointer's jiffies epoch is derived independently of the live process's
    # create_time, so a genuine same-process match can be a touch off -> keep the
    # looser 2.0s tolerance (the tight exact-float bound would false-negative it).
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(ct=1000.0))
    monkeypatch.setattr(procutil, "jiffies_to_epoch", lambda j: 1001.5)
    assert procutil.is_live_bridge(1234, "500") is True  # 1.5s off, within 2.0s
    monkeypatch.setattr(procutil, "jiffies_to_epoch", lambda j: 1003.0)
    assert procutil.is_live_bridge(1234, "500") is False  # 3.0s off, beyond 2.0s


def _fake_proc(status=psutil.STATUS_RUNNING, cmdline=("claude", "remote-control"), ct=1000.0):
    class FakeProc:
        def __init__(self, pid):
            pass

        def status(self):
            return status

        def cmdline(self):
            return list(cmdline)

        def create_time(self):
            return ct

    return FakeProc


def test_is_live_bridge_skips_start_check_when_none(monkeypatch):
    # Bridge cmdline + alive + no comparable start time -> trusted (line 91).
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc())
    assert procutil.is_live_bridge(1234, None) is True


def test_clk_tck_falls_back_on_error(monkeypatch):
    # raising=False: os.sysconf doesn't exist on Windows, so patch it in regardless.
    monkeypatch.setattr(
        procutil.os, "sysconf", lambda _name: (_ for _ in ()).throw(OSError()), raising=False
    )
    assert procutil._clk_tck() == 100


def test_jiffies_to_epoch_none_when_boot_time_unavailable(monkeypatch):
    monkeypatch.setattr(procutil.psutil, "boot_time", lambda: (_ for _ in ()).throw(OSError()))
    assert procutil.jiffies_to_epoch(500) is None


def test_proc_create_time_zombie_is_none(monkeypatch):
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(status=psutil.STATUS_ZOMBIE))
    assert procutil.proc_create_time(1234) is None


def test_proc_create_time_missing_pid_is_none():
    assert procutil.proc_create_time(2_000_000_000) is None


def test_expected_epoch_normalizations():
    assert procutil._expected_epoch(None) is None
    assert procutil._expected_epoch(1234.5) == 1234.5  # already an epoch
    assert procutil._expected_epoch("abc") is None  # non-numeric -> skip
    assert procutil._expected_epoch(True) is None  # bool -> int("True") fails -> None
    jiffies = procutil._expected_epoch("0")  # jiffies string -> epoch
    assert jiffies is not None and abs(jiffies - psutil.boot_time()) < 1.0


def test_reap_if_exited_swallows_non_child():
    # Neither a bogus PID nor our own (not a child) should raise.
    procutil.reap_if_exited(2_000_000_000)
    procutil.reap_if_exited(os.getpid())


def test_force_kill_tree_kills_process():
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert psutil.pid_exists(proc.pid)
        procutil.force_kill_tree(proc.pid)
        proc.wait(timeout=5)
        assert proc.poll() is not None  # actually dead
    finally:
        if proc.poll() is None:
            proc.kill()


def test_force_kill_tree_safe_on_dead_pid():
    procutil.force_kill_tree(2_000_000_000)  # absent PID -> no raise


def test_force_kill_tree_swallows_kill_race(monkeypatch):
    # A target that dies between enumeration and kill (NoSuchProcess on .kill())
    # must be swallowed per-process, not abort the whole tree-kill.
    class Racy:
        def kill(self):
            raise psutil.NoSuchProcess(1234)

    class FakeProc:
        def __init__(self, pid):
            pass

        def children(self, recursive=False):
            return [Racy()]

        def kill(self):
            raise psutil.NoSuchProcess(1234)

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    procutil.force_kill_tree(1234)  # both children and parent race -> no raise


# -- hosted cmdline + generic liveness + match-gated kill (CL-8) -------------


def test_is_hosted_cmdline_matches_stream_json_agent():
    assert procutil.is_hosted_cmdline(["claude", "--output-format", "stream-json"]) is True


def test_is_hosted_cmdline_rejects_bridge_and_empty():
    assert procutil.is_hosted_cmdline(["claude", "remote-control"]) is False
    assert procutil.is_hosted_cmdline([]) is False


def test_is_live_process_no_cmdline_gate(monkeypatch):
    # Without a cmdline predicate, alive + matching create_time is enough.
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(cmdline=("anything",), ct=1000.0))
    assert procutil.is_live_process(1234, 1000.0) is True
    assert procutil.is_live_process(1234, 5000.0) is False  # create_time mismatch → reuse


def test_is_live_process_hosted_cmdline_gate(monkeypatch):
    monkeypatch.setattr(
        procutil.psutil, "Process", _fake_proc(cmdline=("claude", "stream-json"), ct=1000.0)
    )
    assert (
        procutil.is_live_process(1234, 1000.0, require_cmdline=procutil.is_hosted_cmdline) is True
    )
    # A bridge cmdline fails the hosted gate even though the process is alive + matches.
    monkeypatch.setattr(
        procutil.psutil, "Process", _fake_proc(cmdline=("claude", "remote-control"), ct=1000.0)
    )
    assert (
        procutil.is_live_process(1234, 1000.0, require_cmdline=procutil.is_hosted_cmdline) is False
    )


def test_is_live_bridge_still_gated_on_bridge_cmdline(monkeypatch):
    # Regression: the wrapper keeps the bridge cmdline gate after the extraction.
    monkeypatch.setattr(
        procutil.psutil, "Process", _fake_proc(cmdline=("claude", "stream-json"), ct=1000.0)
    )
    assert procutil.is_live_bridge(1234, 1000.0) is False  # hosted cmdline ≠ bridge


# -- standard-subcommand discriminator (adoption gate, #330) -----------------


def test_is_standard_bridge_cmdline_matches_subcommand():
    assert procutil.is_standard_bridge_cmdline(["claude", "remote-control", "--name", "x"]) is True
    # binary resolved to an absolute path still carries the standalone subcommand token
    assert procutil.is_standard_bridge_cmdline(["python3", "/p/claude", "remote-control"]) is True


def test_is_standard_bridge_cmdline_rejects_flag_form_and_non_bridge():
    # The pty/true-resume form is the --remote-control / --rc FLAG, not the subcommand;
    # is_bridge_cmdline matches it (substring), so the stricter gate must reject it.
    assert procutil.is_bridge_cmdline(["claude", "--remote-control", "--resume", "u"]) is True
    assert (
        procutil.is_standard_bridge_cmdline(["claude", "--remote-control", "--resume", "u"])
        is False
    )
    assert procutil.is_standard_bridge_cmdline(["claude", "--rc", "name"]) is False
    assert procutil.is_standard_bridge_cmdline([]) is False
    assert procutil.is_standard_bridge_cmdline(["claude", "agents", "--json"]) is False
    assert procutil.is_standard_bridge_cmdline(["python3", "pytest"]) is False  # no claude


def test_is_standard_bridge_cmdline_rejects_flag_form_with_remote_control_named_project():
    # A project literally named "remote-control" passed as the pty flag's positional puts
    # the bare token into argv; the explicit flag-rejection must still classify it pty.
    assert (
        procutil.is_standard_bridge_cmdline(["claude", "--remote-control", "remote-control"])
        is False
    )
    # joined-flag form (--remote-control=name) is rejected too
    assert procutil.is_standard_bridge_cmdline(["claude", "--remote-control=foo"]) is False


def test_is_live_standard_bridge_gates_on_standard_cmdline(monkeypatch):
    # Standard subcommand + alive + matching create-time -> trusted.
    monkeypatch.setattr(
        procutil.psutil, "Process", _fake_proc(cmdline=("claude", "remote-control"), ct=1000.0)
    )
    assert procutil.is_live_standard_bridge(1234, 1000.0) is True
    # The flag-form (pty) bridge is alive and passes the loose is_live_bridge gate, but
    # the standard gate rejects it — that asymmetry is the whole point of the new check.
    monkeypatch.setattr(
        procutil.psutil,
        "Process",
        _fake_proc(cmdline=("claude", "--remote-control", "--resume", "u"), ct=1000.0),
    )
    assert procutil.is_live_bridge(1234, 1000.0) is True
    assert procutil.is_live_standard_bridge(1234, 1000.0) is False


def test_kill_if_match_kills_only_on_match(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: True)
    assert procutil.kill_if_match(1234, 1000.0) is True
    assert killed == [1234]
    killed.clear()
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: False)
    assert procutil.kill_if_match(1234, 1000.0) is False
    assert killed == []  # never killed when the match fails


def test_kill_if_match_fails_closed_without_comparable_start(monkeypatch):
    # Even if liveness would pass, a missing/uncomparable proc_start must NOT kill:
    # without a create-time match this destructive path could hit a reused PID.
    killed: list[int] = []
    monkeypatch.setattr(procutil, "force_kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: True)
    assert procutil.kill_if_match(1234, None) is False
    assert procutil.kill_if_match(1234, "garbage") is False
    assert killed == []


def test_is_killable_hosted_requires_comparable_start(monkeypatch):
    # The shared orphan-classification/kill predicate: alive + comparable create-time.
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: True)
    assert procutil.is_killable_hosted(1234, 1000.0) is True  # comparable + alive
    assert procutil.is_killable_hosted(1234, None) is False  # no create-time evidence
    assert procutil.is_killable_hosted(1234, "garbage") is False  # uncomparable
    monkeypatch.setattr(procutil, "is_live_process", lambda *a, **k: False)
    assert procutil.is_killable_hosted(1234, 1000.0) is False  # comparable but dead


def test_child_env_strips_clauster_secrets(monkeypatch):
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "must-not-leak-to-a-child")
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH", "must-not-leak-the-password-hash")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = procutil.child_env()
    assert "CLAUSTER_SESSION_SECRET" not in env
    assert "CLAUSTER_AUTH_PASSWORD_HASH" not in env
    assert env["PATH"] == "/usr/bin"  # ordinary env preserved


def test_child_env_preserves_recap_and_path_pointers(monkeypatch):
    monkeypatch.setenv("CLAUSTER_RESUME_RECAP", "1")
    monkeypatch.setenv("CLAUSTER_RESUME_RECAP_MAX_CHARS", "8000")
    monkeypatch.setenv("CLAUSTER_CONFIG", "/etc/clauster.yml")
    monkeypatch.setenv("CLAUSTER_HOME", "/home/clauster")
    env = procutil.child_env()
    assert env["CLAUSTER_RESUME_RECAP"] == "1"
    assert env["CLAUSTER_RESUME_RECAP_MAX_CHARS"] == "8000"
    assert env["CLAUSTER_CONFIG"] == "/etc/clauster.yml"
    assert env["CLAUSTER_HOME"] == "/home/clauster"


def test_child_env_overlays_extra_after_scrub(monkeypatch):
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "must-not-leak-from-environ")
    # extra overlays non-secret keys, but a secret passed back via extra is also
    # dropped — the chokepoint never emits a secret, even on caller misuse.
    env = procutil.child_env(
        {"CLAUSTER_RESUME_RECAP": "1", "CLAUSTER_SESSION_SECRET": "must-not-leak-from-extra"}
    )
    assert env["CLAUSTER_RESUME_RECAP"] == "1"
    assert "CLAUSTER_SESSION_SECRET" not in env  # neither the environ copy nor the overlay


def test_child_env_returns_independent_copy(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "v")
    env = procutil.child_env()
    env["MUTATED"] = "x"
    assert "MUTATED" not in os.environ  # mutating the child env never touches os.environ


def test_is_secret_env_name_matches_known_and_shaped():
    assert procutil.is_secret_env_name("CLAUSTER_SESSION_SECRET") is True
    assert procutil.is_secret_env_name("CLAUSTER_AUTH_PASSWORD_HASH") is True
    # a future CLAUSTER_* secret is caught by the token heuristic, no code change
    assert procutil.is_secret_env_name("CLAUSTER_API_TOKEN") is True
    assert procutil.is_secret_env_name("CLAUSTER_DB_PASSWD") is True


def test_is_secret_env_name_allows_non_secrets():
    for name in (
        "CLAUSTER_CONFIG",
        "CLAUSTER_HOME",
        "CLAUSTER_RESUME_RECAP",
        "CLAUSTER_RESUME_RECAP_MAX_CHARS",
        "PATH",
        "HOME",
        "SECRET_SANTA",  # secret-shaped but not CLAUSTER_-prefixed: not ours to scrub
    ):
        assert procutil.is_secret_env_name(name) is False


def test_bridge_env_overlay_appends_path_in_order(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")
    overlay = procutil.bridge_env_overlay(path_append=["~/.local/bin", "/opt/tools"])
    # inherited PATH stays first; appends follow in order with ~ expanded. Compute the
    # expanded element via expanduser so this matches on Windows too (expanduser resolves
    # ~ from USERPROFILE there, not the monkeypatched HOME).
    local_bin = os.path.expanduser("~/.local/bin")
    assert overlay["PATH"] == os.pathsep.join(["/usr/bin", local_bin, "/opt/tools"])


def test_bridge_env_overlay_handles_empty_inherited_path(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    overlay = procutil.bridge_env_overlay(path_append=["/opt/tools"])
    # no leading empty segment when there is no inherited PATH.
    assert overlay["PATH"] == "/opt/tools"


def test_bridge_env_overlay_overlays_env_map():
    overlay = procutil.bridge_env_overlay(env={"FOO": "bar"})
    assert overlay["FOO"] == "bar"
    assert "PATH" not in overlay  # no path_append → PATH left to the inherited copy


def test_bridge_env_overlay_path_append_respects_operator_path(monkeypatch):
    # An operator PATH set via env is the base for path_append, not silently discarded
    # in favour of the inherited PATH (#497 greptile).
    monkeypatch.setenv("PATH", "/usr/bin")
    overlay = procutil.bridge_env_overlay(env={"PATH": "/custom/bin"}, path_append=["/opt/tools"])
    assert overlay["PATH"] == os.pathsep.join(["/custom/bin", "/opt/tools"])


def test_bridge_env_overlay_no_inputs_is_empty():
    assert procutil.bridge_env_overlay() == {}
    assert procutil.bridge_env_overlay(path_append=[], env={}) == {}


def test_bridge_env_overlay_drops_secret_env_through_child_env(monkeypatch):
    # A config env map that names a Clauster secret must not re-introduce it: the
    # overlay flows through child_env, which scrubs it on the way to the child.
    monkeypatch.setenv("PATH", "/usr/bin")
    overlay = procutil.bridge_env_overlay(
        path_append=["/opt/tools"],
        env={"FOO": "bar", "CLAUSTER_SESSION_SECRET": "must-not-leak"},
    )
    env = procutil.child_env(overlay)
    assert env["FOO"] == "bar"
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/opt/tools"])
    assert "CLAUSTER_SESSION_SECRET" not in env  # scrubbed by child_env, never reaches the bridge


# -- resolve_nvm_default_node_bin_dir (#792: npx/node MCP servers under systemd) -----------


def test_resolve_nvm_default_node_bin_dir_none_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert procutil.resolve_nvm_default_node_bin_dir() is None


def test_resolve_nvm_default_node_bin_dir_none_without_bash(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert procutil.resolve_nvm_default_node_bin_dir() is None


def test_resolve_nvm_default_node_bin_dir_resolves_node(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/bash")
    bin_dir = tmp_path / "v18.20.4" / "bin"
    bin_dir.mkdir(parents=True)
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)  # must be executable — the resolver rejects a non-executable node

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{node}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = procutil.resolve_nvm_default_node_bin_dir(nvm_dir=str(tmp_path / ".nvm"))
    assert result == str(bin_dir)
    # NVM_DIR travels via the subprocess env, never interpolated into the script text.
    assert captured["env"]["NVM_DIR"] == str(tmp_path / ".nvm")
    assert captured["cmd"][0] == "/bin/bash"


def test_resolve_nvm_default_node_bin_dir_none_when_no_default_alias(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/bash")

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="N/A\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert procutil.resolve_nvm_default_node_bin_dir() is None


def test_resolve_nvm_default_node_bin_dir_none_when_resolved_path_missing(monkeypatch):
    # nvm printed a path but the file doesn't exist (stale/garbled output) — fail closed.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/bash")

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="/nonexistent/node\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert procutil.resolve_nvm_default_node_bin_dir() is None


def test_cached_nvm_default_node_bin_dir_resolves_once(monkeypatch):
    # The spawn path AND the doctor panel share this memo, so the (bash-shelling) resolver
    # runs at most once per process — not per spawn or per dashboard refresh.
    calls = {"n": 0}

    def _counting(*a, **k):
        calls["n"] += 1
        return "/home/u/.nvm/versions/node/v20/bin"

    monkeypatch.setattr(procutil, "resolve_nvm_default_node_bin_dir", _counting)
    first = procutil.cached_nvm_default_node_bin_dir()
    again = procutil.cached_nvm_default_node_bin_dir()
    assert first == again == "/home/u/.nvm/versions/node/v20/bin"
    assert calls["n"] == 1  # second call served from the memo, no re-probe


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX execute-bit semantics; os.access(X_OK) is ~existence on Windows (the "
    "feature itself is POSIX-only and returns None on win32 before the X_OK check)",
)
def test_resolve_nvm_default_node_bin_dir_none_when_node_not_executable(monkeypatch, tmp_path):
    # nvm printed a real file, but it isn't executable — appending its dir would leave
    # node/npx MCP servers failing with the same symptom this feature fixes, so fail closed.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/bash")
    bin_dir = tmp_path / "v18" / "bin"
    bin_dir.mkdir(parents=True)
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\n")
    node.chmod(0o644)  # regular file, NOT executable

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{node}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert procutil.resolve_nvm_default_node_bin_dir() is None


def test_resolve_nvm_default_node_bin_dir_none_on_timeout(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/bash")

    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5.0))

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert procutil.resolve_nvm_default_node_bin_dir() is None


def test_resolve_nvm_default_node_bin_dir_none_on_oserror(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/bash")

    def _fake_run(cmd, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert procutil.resolve_nvm_default_node_bin_dir() is None


# ----- audited coverage gaps (2026-07 audit) -----------------------------


def test_is_keeper_process_zombie_is_false(monkeypatch):
    # procutil.py 275-276: a ZOMBIE with a matching keeper cmdline is dead — the
    # cmdline gate must fail closed so orphan listing / hard-kill never act on it.
    keeper_argv = (
        sys.executable,
        "-m",
        "clauster.pty_keeper",
        "--sidecar",
        "/tmp/k.json",
        "--",
        "claude",
        "--remote-control",
    )
    monkeypatch.setattr(
        procutil.psutil,
        "Process",
        _fake_proc(status=psutil.STATUS_ZOMBIE, cmdline=keeper_argv),
    )
    assert procutil.is_keeper_process(1234) is False
    # Differential control: the identical cmdline while RUNNING is a live keeper —
    # proves the zombie status (not the cmdline) is what failed the check above.
    monkeypatch.setattr(
        procutil.psutil,
        "Process",
        _fake_proc(status=psutil.STATUS_RUNNING, cmdline=keeper_argv),
    )
    assert procutil.is_keeper_process(1234) is True


def test_is_bridge_process_zombie_is_false(monkeypatch):
    # The bridge twin of the check above, and previously pinned by nothing: deleting the
    # zombie arm left the whole suite green. The phantom-prune asks "is this EXTERNAL
    # session really a live bridge?" before deleting a resumable card — a zombie answering
    # yes would keep a card alive against a process that is already gone.
    bridge_argv = ("claude", "remote-control", "--name", "alpha")
    monkeypatch.setattr(
        procutil.psutil,
        "Process",
        _fake_proc(status=psutil.STATUS_ZOMBIE, cmdline=bridge_argv),
    )
    assert procutil.is_bridge_process(1234) is False
    # Differential control: same cmdline, RUNNING -> True, so it is the status that decided.
    monkeypatch.setattr(
        procutil.psutil,
        "Process",
        _fake_proc(status=psutil.STATUS_RUNNING, cmdline=bridge_argv),
    )
    assert procutil.is_bridge_process(1234) is True


def test_is_bridge_cmdline_matches_the_rc_alias():
    # `--rc` is a real spelling — Clauster's own supervisor spawns bridges with it — but the
    # substring test only ever sees the literal "remote-control", so the alias read as "not a
    # bridge". That made the #1096 phantom-prune's "is it actually a bridge?" gate answer no
    # for a genuine bridge.
    assert procutil.is_bridge_cmdline(["claude", "--rc", "alpha"]) is True
    assert procutil.is_bridge_cmdline(["claude", "--rc=alpha"]) is True
    # The long flag form and the subcommand form were already matched; keep them pinned.
    assert procutil.is_bridge_cmdline(["claude", "--remote-control", "alpha"]) is True
    assert procutil.is_bridge_cmdline(["claude", "remote-control", "--name", "alpha"]) is True
    # Still requires the binary hint, so an unrelated `--rc` (e.g. an rc-file flag) is not one.
    assert procutil.is_bridge_cmdline(["someothertool", "--rc", "alpha"]) is False


def test_rc_alias_is_not_adoptable_as_a_standard_bridge():
    # Widening is_bridge_cmdline must NOT widen the adoption gate: the flag form is a pty
    # bridge (terminal-coupled stop, no recoverable keeper) and stays non-adoptable.
    for argv in (["claude", "--rc", "alpha"], ["claude", "--rc=alpha"]):
        assert procutil.is_bridge_cmdline(argv) is True
        assert procutil.is_standard_bridge_cmdline(argv) is False


def test_reap_if_exited_without_wnohang_is_noop(monkeypatch):
    # procutil.py 323-324: the Windows arm — no os.WNOHANG means no zombies to reap,
    # so the function returns before ever calling waitpid.
    calls: list[tuple] = []
    monkeypatch.setattr(procutil.os, "waitpid", lambda *a: calls.append(a), raising=False)
    monkeypatch.delattr(procutil.os, "WNOHANG", raising=False)
    procutil.reap_if_exited(12345)
    assert calls == []


def test_bridge_env_overlay_blank_path_append_entries_ignored():
    # procutil.py 408->414: path_append entries that are all empty/falsy expand to
    # nothing — no PATH key is injected (an empty append must not clobber PATH).
    overlay = procutil.bridge_env_overlay(path_append=["", ""])
    assert "PATH" not in overlay


class _FakeChild:
    def __init__(self, pid):
        self.pid = pid


def test_owned_pids_collects_roots_and_children_recursive(monkeypatch):
    # The ownership set (#820) is every root PLUS the union of each root's recursive
    # children — the roots are included so an in-process pty (session pid == bridge
    # pid) still reads as owned.
    trees = {10: [11, 12], 20: [21]}

    class FakeProc:
        def __init__(self, pid):
            self._pid = pid

        def children(self, recursive=False):
            assert recursive is True
            return [_FakeChild(p) for p in trees.get(self._pid, [])]

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    assert procutil.owned_pids([10, 20]) == {10, 11, 12, 20, 21}


def test_owned_pids_includes_roots():
    # A root is always in its own owned set (can't assert equality — under xdist the
    # test process has live children of its own).
    assert os.getpid() in procutil.owned_pids([os.getpid()])


def test_owned_pids_empty_roots_is_empty():
    assert procutil.owned_pids([]) == set()


def test_owned_pids_dead_root_contributes_only_itself(monkeypatch):
    # A dead/absent root (NoSuchProcess/Zombie) is not indeterminate: it has no live
    # children, so it only contributes its own pid — the other roots still expand.
    class FakeProc:
        def __init__(self, pid):
            self._pid = pid

        def children(self, recursive=False):
            if self._pid == 99:
                raise psutil.NoSuchProcess(self._pid)
            return [_FakeChild(self._pid + 1)]

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    assert procutil.owned_pids([99, 30]) == {99, 30, 31}


def test_owned_pids_denied_root_contributes_only_itself(monkeypatch):
    # A root whose child tree can't be READ (AccessDenied: hidepid/hardened /proc)
    # contributes only its own pid, never descendants — so a child session it spawned
    # reads EXTERNAL (fail closed) rather than being trusted on cwd alone.
    class FakeProc:
        def __init__(self, pid):
            self._pid = pid

        def children(self, recursive=False):
            raise psutil.AccessDenied(self._pid)

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    assert procutil.owned_pids([42]) == {42}


def test_owned_pids_denied_root_does_not_drop_co_located_readable_root(monkeypatch):
    # Per-root: one root's AccessDenied must NOT discard a co-located readable root's
    # descendants. Root 10 is readable (child 11); root 20 is denied → the union keeps
    # {10, 11, 20}, so bridge 10's genuine child stays owned even though 20 is opaque.
    class FakeProc:
        def __init__(self, pid):
            self._pid = pid

        def children(self, recursive=False):
            if self._pid == 20:
                raise psutil.AccessDenied(self._pid)
            return [_FakeChild(11)]

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    assert procutil.owned_pids([10, 20]) == {10, 11, 20}
