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


def test_force_kill_tree_wait_timeout_confirms_death():
    """`wait_timeout` makes the process observably gone by the time the call returns.

    Killing is asynchronous, so a caller that re-checks liveness immediately can otherwise
    still see the target alive — which sends `runner`'s poison heal back into the reattach
    loop, because `clear_pointer` is gated on the (descendant) pid being dead.
    """
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        procutil.force_kill_tree(proc.pid, wait_timeout=5)
        # No proc.wait() first: the point is that force_kill_tree already waited.
        assert not psutil.pid_exists(proc.pid) or psutil.Process(proc.pid).status() in (
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        ), "wait_timeout must not return while the target is still running"
    finally:
        # NB on POSIX psutil's wait() already reaped this child, so Popen.poll() reports 0
        # (not -SIGKILL) and this guard never fires — it is a Windows/edge-case net only.
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_force_kill_tree_wait_survivor_is_logged_not_raised(monkeypatch, caplog):
    """A process that outlives the wait degrades to the old behaviour, never a raise."""
    survivor = object()

    class FakeProc:
        def __init__(self, pid):
            pass

        def children(self, recursive=False):
            return []

        def kill(self):
            return None

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    monkeypatch.setattr(procutil.psutil, "wait_procs", lambda targets, timeout: ([], [survivor]))
    with caplog.at_level("DEBUG", logger="clauster.procutil"):
        procutil.force_kill_tree(4242, wait_timeout=0.01)
    assert "outlived" in caplog.text


def test_force_kill_tree_wait_failure_is_swallowed(monkeypatch):
    """A psutil failure in the WAIT must not undo or mask the kills already delivered."""

    class FakeProc:
        def __init__(self, pid):
            pass

        def children(self, recursive=False):
            return []

        def kill(self):
            return None

    def _boom(targets, timeout):
        raise psutil.Error("wait blew up")

    monkeypatch.setattr(procutil.psutil, "Process", FakeProc)
    monkeypatch.setattr(procutil.psutil, "wait_procs", _boom)
    procutil.force_kill_tree(4242, wait_timeout=0.01)  # no raise


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


def test_bridge_env_overlay_prepends_before_base_and_appends_after(monkeypatch):
    # #1018: prepend dirs sit BEFORE the base PATH (so they win resolution — nvm's node
    # over a distro /usr/bin/node), append dirs AFTER (gap-fill). ~ is expanded in both.
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")
    overlay = procutil.bridge_env_overlay(
        path_append=["/opt/tools"], path_prepend=["~/.nvm/v24/bin"]
    )
    nvm = os.path.expanduser("~/.nvm/v24/bin")
    assert overlay["PATH"] == os.pathsep.join([nvm, "/usr/bin", "/opt/tools"])


def test_bridge_env_overlay_prepend_respects_operator_path(monkeypatch):
    # A prepend still sits before an operator-supplied env['PATH'] base (which replaces
    # the inherited PATH), never silently discarding it.
    monkeypatch.setenv("PATH", "/usr/bin")
    overlay = procutil.bridge_env_overlay(env={"PATH": "/custom/bin"}, path_prepend=["/nvm/bin"])
    assert overlay["PATH"] == os.pathsep.join(["/nvm/bin", "/custom/bin"])


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


def test_keeper_and_bridge_predicates_fail_closed_on_a_negative_pid():
    # Both docstrings promise "fails closed on ANY psutil error", but psutil raises
    # ValueError — NOT the NoSuchProcess/AccessDenied/ZombieProcess the catch listed — for a
    # non-positive pid. Several callers feed these a pid read straight out of a keeper
    # sidecar, which is an on-disk file that can hold a negative value, so while it was
    # uncaught it raised out of `rediscover`'s to_thread and failed LIFESPAN STARTUP rather
    # than skipping one sidecar. Real psutil here, not a fake: the guarantee is about
    # psutil's actual behaviour, and a fake would let the catch drift back out of sync.
    with pytest.raises(ValueError):
        psutil.Process(-1)  # the raise these two guards must absorb

    assert procutil.is_keeper_process(-1) is False
    assert procutil.is_bridge_process(-1) is False
    # The other two in the family, which take the SAME untrusted on-disk ints. Hardening
    # only the cmdline pair left these raising, and `forget`/`iter_keepers` reach them
    # first — a shared guard has to be pinned at every call site, not the loudest one.
    assert procutil.proc_create_time(-1) is None
    assert procutil.is_live_bridge(-1, None) is False
    assert procutil.is_live_process(-1, None) is False


def test_bridge_ancestor_finds_the_bridge_above_an_sdk_worker(monkeypatch):
    # THE #1116 regression test, in the shape measured on the dogfood host: `agents --json`
    # reports a Server Mode session's pid as the SDK worker
    # (`…/versions/2.1.220 --print --sdk-url …`), whose own cmdline is NOT a bridge cmdline.
    # `is_bridge_process` on it therefore answered False for every session, the prune's
    # `external_cwds` was always empty, and #1096's fix could never execute. The bridge is
    # that worker's PARENT.
    worker = ("/home/u/.local/share/claude/versions/2.1.220", "--print", "--sdk-url", "x")
    bridge = ("/home/u/.local/bin/claude", "remote-control", "--name", "alpha")
    service = ("/home/u/.local/bin/clauster", "run", "-c", "clauster.yml")
    tree = {3227285: (worker, 3227255), 3227255: (bridge, 3153047), 3153047: (service, 1)}

    class _P:
        def __init__(self, pid):
            if pid not in tree:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return list(tree[self.pid][0])

        def parent(self):
            return _P(tree[self.pid][1]) if tree[self.pid][1] in tree else None

    monkeypatch.setattr(procutil.psutil, "Process", _P)
    # The measured control: the session pid itself is NOT a bridge...
    assert procutil.is_bridge_process(3227285) is False
    # ...but its bridge is one hop up, which is what the prune actually needs to know.
    assert procutil.bridge_ancestor(3227285) == 3227255
    # A flag-form pty session IS the bridge pid, matched at depth 0.
    assert procutil.bridge_ancestor(3227255) == 3227255


def test_bridge_ancestor_stops_when_the_parent_chain_ends(monkeypatch):
    # A process whose parent is gone (or is init) ends the walk with None — a session whose
    # bridge already exited must never charge an unrelated ancestor.
    tree = {60: (("bash",), None), 61: (("bash",), 1), 1: (("init",), None)}

    class _P:
        def __init__(self, pid):
            if pid not in tree:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return list(tree[self.pid][0])

        def parent(self):
            parent_pid = tree[self.pid][1]
            return _P(parent_pid) if parent_pid in tree else None

    monkeypatch.setattr(procutil.psutil, "Process", _P)
    assert procutil.bridge_ancestor(60, max_depth=3) is None  # parent is None
    assert procutil.bridge_ancestor(61, max_depth=3) is None  # parent is pid 1


def test_bridge_ancestor_refuses_to_answer_with_a_pty_keeper(monkeypatch):
    # A keeper matches `is_bridge_cmdline` — it carries the bridge argv after `--`, and that
    # test is a substring match over the joined cmdline. The keeper is the bridge's PARENT,
    # so a pty bridge that died with its keeper not yet reaped would hand the walk to the
    # keeper. That pid is NOT the one the prune excludes as managed (`bridge_pid`), so our
    # own keeper would become evidence for deleting our own resumable card.
    keeper = (
        "python3",
        "-m",
        "clauster.pty_keeper",
        "--sidecar",
        "S",
        "--",
        "claude",
        "--remote-control",
        "alpha",
    )
    # The premise, asserted rather than assumed: the keeper does look like a bridge.
    assert procutil.is_bridge_cmdline(list(keeper)) is True
    tree = {50: (("bash",), 51), 51: (keeper, 1)}

    class _P:
        def __init__(self, pid):
            if pid not in tree:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return list(tree[self.pid][0])

        def parent(self):
            return _P(tree[self.pid][1]) if tree[self.pid][1] in tree else None

    monkeypatch.setattr(procutil.psutil, "Process", _P)
    assert procutil.bridge_ancestor(50, max_depth=2) is None
    # ...and asked about the keeper directly it still refuses, rather than naming itself.
    assert procutil.bridge_ancestor(51) is None


def test_bridge_ancestor_default_depth_is_the_measured_distance(monkeypatch):
    # The default bound is 1 — the measured Server Mode distance (session pid is the SDK
    # worker, its parent is the bridge). Slack is a liability on a gate that deletes cards,
    # so a bridge TWO hops up must not answer at the default.
    bridge = ("claude", "remote-control", "--name", "alpha")
    tree = {60: (("node", "x"), 61), 61: (("sh",), 62), 62: (bridge, 1)}

    class _P:
        def __init__(self, pid):
            if pid not in tree:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return list(tree[self.pid][0])

        def parent(self):
            return _P(tree[self.pid][1]) if tree[self.pid][1] in tree else None

    monkeypatch.setattr(procutil.psutil, "Process", _P)
    assert procutil.bridge_ancestor(60) is None, "two hops must not resolve at the default"
    assert procutil.bridge_ancestor(61) == 62, "one hop is the measured distance and resolves"


def test_bridge_ancestor_stops_before_reaching_an_unrelated_ancestor(monkeypatch):
    # The walk must not keep climbing until *something* looks like a bridge. A session whose
    # own bridge already exited would otherwise charge whatever bridge happens to sit further
    # up the tree, manufacturing prune evidence — and the prune DELETES a resumable card.
    bridge = ("claude", "remote-control", "--name", "alpha")
    plain = ("bash",)
    # A bridge sits 4 hops up, one beyond the bound.
    tree = {10: (plain, 11), 11: (plain, 12), 12: (plain, 13), 13: (plain, 14), 14: (bridge, 1)}

    class _P:
        def __init__(self, pid):
            if pid not in tree:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def status(self):
            return psutil.STATUS_RUNNING

        def cmdline(self):
            return list(tree[self.pid][0])

        def parent(self):
            return _P(tree[self.pid][1]) if tree[self.pid][1] in tree else None

    monkeypatch.setattr(procutil.psutil, "Process", _P)
    assert procutil.bridge_ancestor(10) is None
    # Differential control: raise the bound and the same tree DOES resolve, so it is the
    # depth limit that decided and not a broken walk.
    assert procutil.bridge_ancestor(10, max_depth=4) == 14


def test_bridge_ancestor_fails_closed_on_an_unreadable_tree(monkeypatch):
    # An unreadable process tree (hidepid, a container) must never manufacture evidence that
    # an unmanaged bridge is alive — that would delete a resumable card.
    class _Denied:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            raise psutil.AccessDenied(self.pid)

        def cmdline(self):
            raise psutil.AccessDenied(self.pid)

        def parent(self):
            raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(procutil.psutil, "Process", _Denied)
    assert procutil.bridge_ancestor(4242) is None


def test_is_bridge_cmdline_does_not_match_the_bare_rc_alias():
    # A deliberate miss, not an oversight (#1107). An earlier revision of this branch matched
    # `--rc`, on the premise that Clauster's own supervisor spawns bridges with it. It does
    # not: `supervisor.build_dispatch_argv` builds `claude --bg --rc <name>`, a BACKGROUND
    # AGENT opening a cloud door. Both bridge spawners emit `remote-control` /
    # `--remote-control` instead.
    #
    # `is_bridge_process` gates the phantom-prune, where True DELETES a resumable card. So
    # matching the alias would let a background agent stand as proof that "the bridge is
    # alive, just unmanaged" and prune the card out from under it. A miss only leaves a
    # phantom card lingering, which is the failure to prefer in a delete path.
    #
    # The intermediate fix — anchoring on the executable basename — is also pinned below
    # by the `somelinter` case: the binary hint matches "claude" anywhere in the joined
    # argv, and this host's service user IS `claude`, so every path under /home/claude
    # satisfied it.
    assert procutil.is_bridge_cmdline(["claude", "--rc", "alpha"]) is False
    assert procutil.is_bridge_cmdline(["claude", "--rc=alpha"]) is False
    assert procutil.is_bridge_cmdline(["somelinter", "--rc", "/home/claude/x"]) is False
    # The two spellings carrying the literal `remote-control` token stay matched...
    assert procutil.is_bridge_cmdline(["claude", "--remote-control", "alpha"]) is True
    assert procutil.is_bridge_cmdline(["claude", "remote-control", "--name", "alpha"]) is True
    # ...and only the subcommand form is adoptable as a standard bridge.
    assert procutil.is_standard_bridge_cmdline(["claude", "--remote-control", "alpha"]) is False
    assert procutil.is_standard_bridge_cmdline(["claude", "remote-control", "-n", "a"]) is True


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


# ----- running claude version (#1275) -------------------------------------

# The exact paths measured on the dogfood host: `~/.local/bin/claude` is a SYMLINK to the
# versioned binary, so the bridge's argv[0] is unversioned while its resolved exe is not.
_LIVE_BRIDGE_EXE = "/home/claude/.local/share/claude/versions/2.1.251"
_LIVE_BRIDGE_ARGV0 = "/home/claude/.local/bin/claude"


def test_parse_claude_version_reads_the_native_install_layout():
    # The measured shapes: the bare versioned binary (what `exe` resolves to, and what the
    # SDK worker's argv[0] is), and a path with components BELOW the version segment.
    assert procutil.parse_claude_version(_LIVE_BRIDGE_EXE) == "2.1.251"
    assert procutil.parse_claude_version("/opt/claude/versions/2.1.247/bin/claude") == "2.1.247"
    # Windows: backslashes, and a drive letter. Parsed on a POSIX host too — the string comes
    # from ANOTHER process, so the separator is the remote host's, not ours.
    win = r"C:\Users\u\.local\share\Claude\Versions\2.1.251"
    assert procutil.parse_claude_version(win) == "2.1.251"
    # A prerelease suffix survives intact rather than being rejected or truncated.
    assert procutil.parse_claude_version("/o/claude/versions/2.2.0-rc.1") == "2.2.0-rc.1"


def test_parse_claude_version_refuses_other_version_managers():
    # THE false-positive class this guard exists for. `versions/<x>` is pyenv's, nvm's and
    # rbenv's layout too, and such paths really do appear in a live bridge's process tree —
    # an nvm-installed language server was a grandchild of a running bridge on the dogfood
    # host. Without the `claude/` parent anchor, pyenv would render "3.12.0" as the Claude
    # version: a confidently WRONG label, the one outcome #1275 rules out.
    assert procutil.parse_claude_version("/home/u/.pyenv/versions/3.12.0/bin/python") is None
    assert procutil.parse_claude_version("/home/u/.nvm/versions/node/v24.19.0/bin/node") is None
    assert procutil.parse_claude_version("/home/u/.rbenv/versions/3.3.0/bin/ruby") is None
    # A `claude/versions/` whose next segment isn't a release number is still refused.
    assert procutil.parse_claude_version("/o/claude/versions/node/v24/bin/x") is None
    assert procutil.parse_claude_version("/o/claude/versions/latest") is None
    # `versions/` with nothing after it, no `versions/` at all, and the empty inputs.
    assert procutil.parse_claude_version("/o/claude/versions") is None
    assert procutil.parse_claude_version("/usr/local/bin/claude") is None
    assert procutil.parse_claude_version("") is None
    assert procutil.parse_claude_version(None) is None


def _version_tree(monkeypatch, tree):
    """Patch ``psutil.Process`` with a {pid: (exe, argv, [child_pids])} stand-in."""

    class _P:
        def __init__(self, pid):
            if pid not in tree:
                raise psutil.NoSuchProcess(pid)
            self.pid = pid

        def exe(self):
            value = tree[self.pid][0]
            if isinstance(value, BaseException):
                raise value
            return value

        def cmdline(self):
            value = tree[self.pid][1]
            if isinstance(value, BaseException):
                raise value
            return list(value)

        def children(self, recursive=False):
            # Pinned, not incidental: `running_claude_version` must ask for DIRECT children
            # only. A recursive walk would reach the nvm/pyenv paths deeper in a bridge's
            # tree and hand them to the parse.
            assert recursive is False
            return [_P(p) for p in tree[self.pid][2]]

    monkeypatch.setattr(procutil.psutil, "Process", _P)


def test_running_claude_version_reads_the_bridge_process_itself(monkeypatch):
    # The primary path, and why it is primary: the bridge is exec'd straight from the
    # versioned binary, so ONE exe read answers for both bridge modes without depending on
    # either one's process shape. Here the bridge has no children at all — a standard bridge
    # between sessions — and the version still resolves.
    standard = (_LIVE_BRIDGE_ARGV0, "remote-control", "--name", "clauster")
    _version_tree(monkeypatch, {577519: (_LIVE_BRIDGE_EXE, standard, [])})
    assert procutil.running_claude_version(577519) == "2.1.251"
    # The pty (flag-form, under a keeper) bridge: different argv, same answer.
    pty = (_LIVE_BRIDGE_ARGV0, "--remote-control", "alpha")
    _version_tree(monkeypatch, {4242: (_LIVE_BRIDGE_EXE, pty, [])})
    assert procutil.running_claude_version(4242) == "2.1.251"


def test_running_claude_version_falls_back_to_the_sdk_worker_child(monkeypatch):
    # #1275's measured route, for a layout whose launcher is a WRAPPER rather than the
    # versioned binary: the bridge's own exe/argv carry no version, but its direct child —
    # the SDK worker — execs `…/claude/versions/<version> --print --sdk-url …`.
    worker_argv = (
        "/home/claude/.local/share/claude/versions/2.1.247",
        "--print",
        "--sdk-url",
        "https://example.invalid/x",
    )
    _version_tree(
        monkeypatch,
        {
            43400: ("/usr/bin/node", ("node", "/opt/wrapper/claude", "remote-control"), [846286]),
            846286: ("/usr/bin/node", worker_argv, []),
        },
    )
    assert procutil.running_claude_version(43400) == "2.1.247"


def test_running_claude_version_does_not_recurse_past_direct_children(monkeypatch):
    # A GRANDCHILD's versioned path is not consulted. The bound is what keeps the parse away
    # from the other version managers living deeper in a bridge's tree; `_version_tree`
    # additionally asserts the call is non-recursive, so this pins the outcome as well as
    # the call shape.
    _version_tree(
        monkeypatch,
        {
            10: ("/usr/bin/node", ("node", "wrapper"), [11]),
            11: ("/usr/bin/node", ("node", "inner"), [12]),
            12: (_LIVE_BRIDGE_EXE, (_LIVE_BRIDGE_EXE, "--print"), []),
        },
    )
    assert procutil.running_claude_version(10) is None


def test_running_claude_version_uses_argv_when_exe_is_denied(monkeypatch):
    # A hardened /proc denies `exe` while `cmdline` stays readable. The per-process fallback
    # must absorb that rather than give up on the process.
    worker = "/home/claude/.local/share/claude/versions/2.1.238"
    _version_tree(monkeypatch, {7: (psutil.AccessDenied(7), (worker, "--print"), [])})
    assert procutil.running_claude_version(7) == "2.1.238"


def test_running_claude_version_fails_closed(monkeypatch):
    # Never a stale or guessed value: a dead pid, a negative pid (psutil raises ValueError,
    # not NoSuchProcess — the same untrusted-on-disk-int path the rest of this module
    # absorbs), and a process whose exe, cmdline AND children are all unreadable.
    assert procutil.running_claude_version(2_000_000_000) is None
    assert procutil.running_claude_version(-1) is None

    class _Opaque:
        def __init__(self, pid):
            self.pid = pid

        def exe(self):
            raise psutil.AccessDenied(self.pid)

        def cmdline(self):
            raise psutil.AccessDenied(self.pid)

        def children(self, recursive=False):
            raise psutil.AccessDenied(self.pid)

    monkeypatch.setattr(procutil.psutil, "Process", _Opaque)
    assert procutil.running_claude_version(99) is None


def test_running_claude_version_absorbs_a_raw_oserror_from_child_enumeration(monkeypatch):
    # This runs inside poll_once's tick: a raw errno from the OS while listing children
    # (not one of psutil's wrapped exceptions) must degrade to "no version", never
    # escape and abort the whole poll — status reconciliation, crash notifications and
    # phantom pruning all ride the same tick.
    class _RawErrno:
        def __init__(self, pid):
            self.pid = pid

        def exe(self):
            return "/usr/bin/other"  # no version derivable -> falls through to children

        def cmdline(self):
            return ["/usr/bin/other"]

        def children(self, recursive=False):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(procutil.psutil, "Process", _RawErrno)
    assert procutil.running_claude_version(99) is None


# ----- #1399: boot-relative start ticks vs. a wall clock that moves ---------------------


def test_proc_start_ticks_reads_the_boot_relative_start_of_a_live_process():
    ticks = procutil.proc_start_ticks(os.getpid())
    if sys.platform != "linux":  # pragma: no cover - exercised on the Linux matrix leg
        assert ticks is None
        return
    assert isinstance(ticks, int) and ticks > 0
    # The whole point: re-reading returns the value the process was born with, so it is
    # stable in a way psutil's create_time (btime + ticks, btime re-read per call) is not.
    assert procutil.proc_start_ticks(os.getpid()) == ticks


def test_proc_start_ticks_fails_closed_on_a_pid_that_cannot_name_a_process():
    assert procutil.proc_start_ticks(-1) is None
    assert procutil.proc_start_ticks(2_000_000_000) is None


def test_proc_start_ticks_survives_a_comm_containing_spaces_and_parens(tmp_path, monkeypatch):
    # Field 2 is the executable name, unquoted and free to hold ')' and spaces. A
    # left-to-right split would miscount every later field and return the wrong number —
    # silently, as a plausible tick count, which is the dangerous way to be wrong here.
    fields = " ".join(str(i) for i in range(1, 51))
    monkeypatch.setattr(
        procutil.Path, "read_text", lambda self, **kw: f"77 (evil) proc name) S {fields}"
    )
    # After comm: "S" is index 0, so index 19 is the 19th of the numbered run -> 19.
    assert procutil.proc_start_ticks(77) == 19


def test_proc_start_ticks_returns_none_for_a_truncated_stat_line(monkeypatch):
    monkeypatch.setattr(procutil.Path, "read_text", lambda self, **kw: "77 (claude) S 1 2 3")
    assert procutil.proc_start_ticks(77) is None
    monkeypatch.setattr(procutil.Path, "read_text", lambda self, **kw: "no parens here")
    assert procutil.proc_start_ticks(77) is None


def test_clock_drift_no_longer_reads_a_live_bridge_as_dead(monkeypatch):
    # THE #1399 regression. psutil's create_time is `starttime/CLK_TCK + boot_time()`, and
    # boot_time() re-reads /proc/stat btime every call — NTP slew moves it under a process
    # that never restarted. Measured on the dogfood host: five distinct btime values, a
    # 4-second spread, inside 3.5 minutes, against a 0.05s bound.
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(ct=1000.0))
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: 770579)

    # Epoch-only (a pre-#1399 row, or a non-Linux host): the drift IS the bug.
    assert procutil.is_live_bridge(1234, 1004.0) is False
    # With the boot-relative half recorded, the same drift is absorbed.
    assert procutil.is_live_bridge(1234, 1004.0, start_ticks=770579) is True
    assert procutil.is_live_bridge(1234, 996.0, start_ticks=770579) is True


def test_ticks_still_reject_a_recycled_pid_the_epoch_bound_would_have_admitted(monkeypatch):
    # The tight epoch bound exists to close the PID-reuse window, so the replacement must
    # not be laxer. It is strictly tighter: a pid recycled even 10ms later differs by a
    # whole tick, where 0.05s of epoch slack admitted it.
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(ct=1000.0))
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: 770580)
    assert procutil.is_live_bridge(1234, 1000.0, start_ticks=770579) is False
    assert procutil.is_live_bridge(1234, 1000.01) is True  # what the epoch alone allowed


def test_ticks_do_not_authenticate_a_process_from_a_different_boot(monkeypatch):
    # Ticks restart at zero each boot, so an exact match across a reboot means nothing. The
    # epoch is kept precisely to discriminate that — coarsely, but a reboot moves it far
    # further than any clock correction does.
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(ct=9_000_000.0))
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: 770579)
    assert procutil.is_live_bridge(1234, 1000.0, start_ticks=770579) is False


def test_unreadable_ticks_fall_back_to_the_epoch_comparison(monkeypatch):
    # Non-Linux, or /proc unreadable. The epoch is the only evidence left, so the tight
    # bound applies exactly as it did before #1399 — no silent widening.
    monkeypatch.setattr(procutil.psutil, "Process", _fake_proc(ct=1000.0))
    monkeypatch.setattr(procutil, "proc_start_ticks", lambda pid: None)
    assert procutil.is_live_bridge(1234, 1000.0, start_ticks=770579) is True
    assert procutil.is_live_bridge(1234, 1004.0, start_ticks=770579) is False
