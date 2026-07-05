from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import settings

# --- Live-account safety: pin HOME BEFORE any clauster import -------------------------
#
# The ``_isolate_clauster_home`` fixture below is function-scoped, so it cannot protect
# paths that clauster resolves AT IMPORT TIME — those freeze on first import, which
# happens at collection, before any fixture runs:
#   * ``discovery.CLAUDE_JSON``        = Path("~/.claude.json").expanduser()
#   * ``supervisor.JOBS_DIR`` / ``ROSTER_JSON`` under ``~/.claude/``
#   * ``pointers.CLAUDE_PROJECTS_DIR`` under ``~/.claude/projects``
# ``~/.claude.json`` is the developer's *live* remote-control account; a test that
# imported and exercised ``discovery`` / ``supervisor`` could read or WRITE it,
# corrupting the running service. Repointing HOME here, at conftest import (before any
# clauster module loads), makes those constants expand under a throwaway dir. The
# per-test fixture re-redirects to a fresh dir for runtime paths; this block is the
# import-time backstop the fixture structurally cannot be.
#
# Capture the TRUE home before repointing and stash it, so the regression tests in
# test_db_isolation.py can assert paths resolve off the real home (after this pin,
# ``expanduser("~")`` everywhere — including at their own import — already yields the
# temp dir, so they cannot recover the real home on their own).
os.environ["CLAUSTER_TEST_REAL_HOME"] = os.path.expanduser("~")
_SESSION_HOME = tempfile.mkdtemp(prefix="clauster-test-home-")
os.environ["HOME"] = _SESSION_HOME
os.environ["USERPROFILE"] = _SESSION_HOME  # Windows resolves ``~`` from USERPROFILE
os.environ["CLAUSTER_HOME"] = str(Path(_SESSION_HOME) / ".clauster")
os.environ.pop("CLAUSTER_CONFIG", None)
os.environ.pop("CLAUSTER_STATE_DIR", None)
atexit.register(lambda: shutil.rmtree(_SESSION_HOME, ignore_errors=True))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# --- Hypothesis CI-determinism profiles (issue #450) ----------------------------------
#
# The strict 96% coverage gate runs against the property suite, so a flaky
# property test would fail CI nondeterministically. Two safeguards keep it stable:
#
# * ``derandomize=True`` on the ``ci`` profile makes example generation a pure
#   function of the test source — the same inputs run every time, so a green CI
#   run is reproducible and a failure is never a one-off seed.
# * ``database=None`` everywhere disables the on-disk example database. Persisting
#   a ``.hypothesis/`` cache would make a re-run depend on a prior run's findings
#   (and on CI there is no cache to replay from), so we never persist.
#
# Profile selection (explicit, not relying on Hypothesis' CI auto-detection,
# which an explicit ``load_profile`` below would override anyway):
#   * ``HYPOTHESIS_PROFILE`` env var wins if set (local repro of a CI failure);
#   * else the ``ci`` profile on a detected CI runner (``CI`` env var, set by
#     GitHub Actions and every other major CI);
#   * else the ``dev`` profile (random but non-persistent) for local runs.
# Both profiles set ``print_blob=True`` so a failure always prints a shareable
# base64 reproduction blob, regardless of which profile happened to be active.
settings.register_profile("ci", derandomize=True, database=None, print_blob=True)
settings.register_profile("dev", database=None, print_blob=True)
_HYP_PROFILE = os.getenv("HYPOTHESIS_PROFILE") or ("ci" if os.getenv("CI") else "dev")
settings.load_profile(_HYP_PROFILE)

# Make importable fixture modules (e.g. fake_claustrum) reachable by bare name.
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

# Windows CreateProcess can't launch the extensionless Python stubs, so on Windows
# the fixtures expose a same-named `.cmd` wrapper that shells out to `python`.
WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""


@pytest.fixture(autouse=True)
def _isolate_clauster_home(tmp_path_factory, monkeypatch):
    """Redirect the clauster home to a throwaway temp dir for *every* test.

    Why this is autouse and unconditional: ``Config.state_dir`` defaults to
    ``~/.clauster`` and is resolved through ``Path.expanduser()`` (config.py), so the
    persistence DB lands at ``<HOME>/.clauster/clauster.db``. Startup then runs the
    Alembic ``upgrade(..., "head")`` against that file (``db/bootstrap.py``). A test
    that builds a default ``ClausterConfig`` — or goes through ``create_app`` /
    ``load_config`` with a config that omits ``state_dir`` (e.g. the ``write_config``
    fixture) — would therefore reach, and *migrate*, the developer's real
    ``~/.clauster/clauster.db``. That actually corrupted a live database once, and
    nothing in the suite previously isolated HOME, so the gap was silent.

    The redirection levers, all set with ``monkeypatch`` so they touch only this
    process's environment (never uv's cache or the real shell):

    * ``HOME`` (and ``USERPROFILE`` for Windows) — the lever ``expanduser()`` reads,
      so the default ``state_dir`` / SQLite DB and ``~/.claude.json`` (trust /
      remote-control) all resolve under the temp dir.
    * ``CLAUSTER_HOME`` — a config-discovery lever (``$CLAUSTER_HOME/clauster.yml``);
      pointed at the temp ``.clauster`` so a stray real value can't pull in an
      out-of-tree config.
    * ``CLAUSTER_CONFIG`` / ``CLAUSTER_STATE_DIR`` — *removed* so a value present in
      the developer's real environment can't make ``load_config()`` read a real
      config file or override ``state_dir`` straight back onto a real path.

    Additive by design: this runs first (autouse), so a test that sets its own
    ``HOME`` / ``CLAUSTER_*`` later via the *same* function-scoped ``monkeypatch``
    simply overrides these defaults for that test, then everything is undone at
    teardown. It never points anything at the real home.
    """
    tmp_home = tmp_path_factory.mktemp("clauster-home")
    monkeypatch.setenv("HOME", str(tmp_home))
    # Windows resolves ``~`` from USERPROFILE, not HOME — keep them in lockstep so the
    # isolation holds regardless of platform.
    monkeypatch.setenv("USERPROFILE", str(tmp_home))
    monkeypatch.setenv("CLAUSTER_HOME", str(tmp_home / ".clauster"))
    # Drop any real-environment overrides that could redirect config/state back onto a
    # real path even with HOME isolated. ``raising=False`` because they may be unset.
    monkeypatch.delenv("CLAUSTER_CONFIG", raising=False)
    monkeypatch.delenv("CLAUSTER_STATE_DIR", raising=False)
    # Drop NVM_DIR too: CI runners (and many dev shells) export it pointing at a real
    # nvm install, which would otherwise leak past the isolated HOME and make the
    # nvm-dependent code paths (doctor's node-toolchain check, node_from_nvm) resolve
    # off the host's real toolchain — non-deterministic. Tests that WANT nvm present set
    # it explicitly. ``raising=False`` because it may be unset.
    monkeypatch.delenv("NVM_DIR", raising=False)


@pytest.fixture(autouse=True)
def _reset_nvm_bin_dir_cache():
    """Clear procutil's process-wide nvm bin-dir memo before each test.

    ``cached_nvm_default_node_bin_dir`` resolves once per process (so the spawn path and
    the doctor panel share one probe); without this reset, the first test to populate it
    would leak its (possibly monkeypatched) result into every later test.
    """
    from clauster import procutil

    procutil._nvm_bin_dir_cache = None
    procutil._nvm_bin_dir_resolved = False


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    """A projects_root with a git project, a CLAUDE.md project, a plain one,
    a dotdir, and a bad-name dir (should be skipped)."""
    (tmp_path / "alpha" / ".git").mkdir(parents=True)
    beta = tmp_path / "beta"
    beta.mkdir()
    (beta / "CLAUDE.md").write_text("# beta\n")
    (tmp_path / "gamma").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "bad name!").mkdir()  # invalid project name -> skipped
    return tmp_path


@pytest.fixture
def write_config(tmp_path: Path, projects_root: Path):
    def _write(extra: str = "") -> Path:
        cfg = tmp_path / "clauster.yml"
        # Default the `claude` binary to the fake stub unless the test declares its
        # own `claude:` block. Without this, any app-level test that hits an endpoint
        # invoking the CLI (e.g. `/healthz`, which probes `claude --version` and, per
        # #838, `claude auth status --json`) would shell out to the REAL `claude` on
        # the host's PATH — non-deterministic and a live account (never a real
        # `auth status`). A test needing specific claude config still overrides it by
        # passing its own `claude:` in `extra` (this default is then omitted so YAML
        # never carries two `claude:` keys).
        default_binary = "" if "claude:" in extra else f"claude:\n  binary: {FAKE_CLAUDE}\n"
        # encoding="utf-8" matches how load_config reads it (config.py); without it
        # the platform default (cp1252 on Windows) mangles non-ASCII symbols like €.
        cfg.write_text(
            f"projects_root: {projects_root}\n{default_binary}{extra}", encoding="utf-8"
        )
        return cfg

    return _write


FAKE_CLAUDE = FIXTURES / "fake_claude" / f"claude{WIN_STUB_SUFFIX}"


@pytest.fixture
def fake_claude() -> Path:
    """Absolute path to the parameterizable fake `claude` binary."""
    return FAKE_CLAUDE


@pytest.fixture
async def fake_claustrum() -> AsyncIterator[Callable[..., Awaitable]]:
    """Factory yielding started :class:`FakeClaustrum` daemons, all stopped on teardown.

    Uses a short ``mkdtemp`` socket dir rather than ``tmp_path`` so the ``AF_UNIX``
    path stays under the ~108-char kernel limit.
    """
    from fake_claustrum import FakeClaustrum

    started: list[FakeClaustrum] = []
    sock_dir = Path(tempfile.mkdtemp(prefix="fclaustrum-"))

    async def _make(*, token: str = "tok", **kwargs) -> FakeClaustrum:
        sock = str(sock_dir / f"d{len(started)}.sock")
        fake = FakeClaustrum(sock, token, **kwargs)
        await fake.start()
        started.append(fake)
        return fake

    try:
        yield _make
    finally:
        for fake in started:
            await fake.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)


@pytest.fixture
def runner_config(tmp_path: Path, projects_root: Path):
    """A ClausterConfig wired to the fake binary, a tmp state_dir, and a trusted
    projects_root (so spawn isn't blocked on trust by default)."""
    from clauster.config import ClausterConfig

    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(projects_root.resolve()): {"hasTrustDialogAccepted": True}}})
    )
    config = ClausterConfig(
        projects_root=projects_root,
        state_dir=tmp_path / "state",
        claude={"binary": str(FAKE_CLAUDE)},
    )
    return config, claude_json


async def _raise_cancelled(_seconds: float) -> None:
    """Stand in for ``asyncio.sleep`` to break an otherwise-infinite ``*_forever`` loop.

    Patched over ``clauster.runner.asyncio.sleep`` so a single loop iteration runs and
    then the cancel escapes — shared by the poll-loop and metrics-loop resilience tests.
    """
    raise asyncio.CancelledError


async def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 2.0,
    interval: float = 0.01,
) -> Any:
    """Poll predicate() until truthy, returning the result; raise AssertionError on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)
