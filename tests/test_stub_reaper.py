"""The session-end reaper that stops `tests/fixtures/` stubs leaking onto the host (#1086).

These spawn REAL subprocesses — the leak the reaper exists to close is a real-process
one, so a mocked stand-in could not fail the way the bug did. Every spawn here is
cleaned up on the test's own way out, whether or not the reaper did its job.
"""

from __future__ import annotations

import subprocess
import sys

from conftest import FAKE_CLAUDE, reap_fixture_stub_strays

#: Generous: this only bounds a SIGKILL'd process's disappearance, never a happy path.
DEATH_TIMEOUT = 30


def _spawn_stub(tmp_path) -> subprocess.Popen:
    """Start a `fake_claude` bridge stub that idles until signalled, as the runner does."""
    return subprocess.Popen(  # noqa: S603 — fixture stub, absolute path, list argv
        [
            str(FAKE_CLAUDE),
            "remote-control",
            "--name",
            "reaper-probe",
            "--debug-file",
            str(tmp_path / "bridge.log"),
        ]
    )


def test_reaper_kills_an_abandoned_stub(tmp_path):
    proc = _spawn_stub(tmp_path)
    try:
        # The stub must be genuinely alive first: a reaper that "found nothing" against a
        # process that had already exited would pass this test while reaping nothing.
        assert proc.poll() is None
        reaped = reap_fixture_stub_strays()

        assert proc.pid in reaped
        assert proc.wait(timeout=DEATH_TIMEOUT) != 0  # killed, not a clean exit
    finally:
        if proc.poll() is None:  # pragma: no cover — only on a reaper regression
            proc.kill()
            proc.wait(timeout=DEATH_TIMEOUT)


def test_reaper_leaves_non_stub_children_alone(tmp_path):
    """A descendant that isn't a fixture stub is not ours to kill.

    The blast-radius half of the contract: the reaper walks our own process tree, but
    within it selects on the `tests/fixtures/` path, so an unrelated subprocess a test
    spawned survives.
    """
    proc = subprocess.Popen(  # noqa: S603 — sys.executable, list argv
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
    )
    try:
        assert proc.poll() is None
        assert proc.pid not in reap_fixture_stub_strays()
        assert proc.poll() is None  # still running after the reap
    finally:
        proc.stdin.close()
        proc.wait(timeout=DEATH_TIMEOUT)
