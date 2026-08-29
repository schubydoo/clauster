from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import psutil
import pytest
from hypothesis import settings
from sqlalchemy import Engine


def _can_create_symlink() -> bool:
    """Whether this runner can create a symlink (probed once, at import).

    POSIX always can; Windows needs the create-symlink privilege — admin or Developer
    Mode — which the GitHub ``windows-latest`` runner and the project's Windows test VM
    both have. So the symlink-escape *defense* tests should run wherever symlinks are
    actually available (Windows included) rather than being blanket-skipped on win32;
    they fall back to a skip only on a locked-down box that genuinely can't create one.
    """
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "t"
        target.write_text("x")
        try:
            (Path(d) / "l").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
        return True


#: Skip only where symlinks truly can't be created (see :func:`_can_create_symlink`),
#: NOT unconditionally on Windows — an admin Windows runner exercises the symlink-escape
#: defenses for real (#929).
needs_symlink = pytest.mark.skipif(
    not _can_create_symlink(),
    reason="symlink creation unavailable (needs privilege / Developer Mode on Windows)",
)


def audit_autofill(html: str) -> tuple[list[dict], list[dict]]:
    """Audit #1036 per field: return (non-password inputs LACKING the opt-out, password inputs
    that WRONGLY carry it).

    Every non-credential ``input``/``textarea`` must opt out of password-manager autofill
    (``data-lpignore``); credential fields must not (login autofill). Parses via ``HTMLParser`` so
    a ``>`` inside an Alpine attribute value (e.g. ``x-show="a > b"``) can't fool a naive regex.
    Both lists empty == correct.
    """
    from html.parser import HTMLParser

    class _Audit(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.missing: list[dict] = []
            self.pw_optout: list[dict] = []

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag not in ("input", "textarea"):
                return
            d = {k: v for k, v in attrs}
            is_pw = d.get("type") == "password"
            has_optout = "data-lpignore" in d
            if is_pw and has_optout:
                self.pw_optout.append(d)
            elif not is_pw and not has_optout:
                self.missing.append(d)

    a = _Audit()
    a.feed(html)
    return a.missing, a.pw_optout


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

# ⚠️ BELOW the pin on purpose — a `noqa: E402` on each is the cost of the invariant above.
# (The hash is omitted from that token deliberately: ruff scans every comment for it and
# reads a mention in prose as a real, malformed directive.)
# These are the only module-level `clauster` imports in this file; every other one is
# function-local for exactly this reason. Nothing they pull in resolves `~` at import today
# (verified: `clauster.db.*` reaches 13 clauster modules and none of them is `discovery`,
# `supervisor` or `pointers`), so importing them at the top was not a live bug — but it
# removed the ORDERING GUARANTEE. The next `from .discovery import …` added to `config.py`
# or `auth.py`, a change with no visible connection to tests, would then freeze a real-home
# path at collection. That is the failure this block exists to make structurally impossible,
# so the imports move rather than the pin.
from clauster.db import bootstrap as db_bootstrap  # noqa: E402
from clauster.db import engine as db_engine  # noqa: E402
from clauster.db import persistence as db_persistence  # noqa: E402

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


@pytest.fixture(autouse=True)
def _reset_atomicio_lock_dir():
    """Reset atomicio's process-wide cross-process lock-dir state around each test.

    ``configure_lock_dir`` sets a module global (in prod, once in ``create_app``). A test
    that configures it to a ``tmp_path`` subdir would otherwise leak that (soon-deleted)
    dir into every later test in the same xdist worker — a config write in an unrelated
    test would then try to open a lock file under a path that no longer exists. Reset to
    the unconfigured state before AND after each test, and clear the warn-once latch so the
    "unconfigured" warning is assertable in isolation.
    """
    from clauster import atomicio

    def _reset() -> None:
        atomicio._LOCK_DIR = None
        atomicio._CROSS_PROCESS_UNCONFIGURED_WARNED = False

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _dispose_db_engines():
    """Dispose any DB engine a test leaves open, after each test.

    A bare ``TestClient(create_app(...))`` (no ``with``) skips the app lifespan that
    calls ``persistence.dispose()``, so the throwaway app's SQLite connection lingers
    until GC and warns ``ResourceWarning: unclosed database``. Closing every live engine
    at teardown removes that noise deterministically WITHOUT suppressing the warning — a
    genuine unclosed connection outside the engine registry would still surface. Safe:
    the suite has no session/module-scoped app fixture whose engine this could yank out,
    and ``dispose()`` is idempotent for an engine a ``with``-client already closed.
    """
    yield
    from clauster.db.engine import dispose_live_engines

    dispose_live_engines()


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


# --- Reap the fixture-stub subprocesses a session abandons (#1086) --------------------
#
# The `fake_claude` stub idles until it is signalled — that is its contract, and several
# tests deliberately never signal it: `test_forget_running_bridge` and the rest of the
# forget family exist precisely to ABANDON a running bridge, and the API-spawn route
# tests simply never stop the bridge they spawned. Nothing reaped them, so each xdist
# worker exited leaving its stubs behind, reparented to init. They idle for ~600s and a
# full run left ~24 of them, so a day of iterating buried a dev host in idle pythons
# whose argv reads `claude remote-control --name alpha` — indistinguishable, at a glance,
# from real bridges on a box that also runs clauster.
#
# Fixing it in the tests would mean making them stop the bridge, which is the very thing
# they assert does NOT happen. So the reap belongs here, once, at the end of the session.


def _fixture_stub_descendants() -> list[psutil.Process]:
    """Live descendants of THIS pytest process that are running a `tests/fixtures/` stub.

    Identity comes from the process TREE, never from a name match. We walk our own
    descendants and keep only those whose argv names a path inside `tests/fixtures/`, so
    a developer's real `claude` bridge — or the live clauster service on the same host —
    is structurally unreachable from here. A `pkill -f remote-control` would hit both.

    Both halves of a Windows stub match: the direct child is the `.cmd` shim under
    `tests/fixtures/`, and the python grandchild it spawns carries the expanded stub path
    in its own argv. Killing the shim alone would strand that grandchild (#1126/#1127),
    which is why the reap below goes through `force_kill_tree`.
    """
    marker = os.path.normcase(str(FIXTURES))
    try:
        descendants = psutil.Process().children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):  # pragma: no cover — defensive
        return []
    stubs = []
    for proc in descendants:
        try:
            argv = proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue  # exited, or not ours to inspect — either way, not ours to kill
        if any(marker in os.path.normcase(arg) for arg in argv):
            stubs.append(proc)
    return stubs


def reap_fixture_stub_strays() -> list[int]:
    """Hard-kill every surviving fixture-stub descendant, returning the pids reaped.

    Each pid is one we just read out of our own descendant tree, and `force_kill_tree`
    is safe on a pid that died in between. No `wait_timeout`: on POSIX these ARE our own
    children, and psutil's wait goes through `os.waitpid`, which would reap them out from
    under any `Popen` still holding them and rewrite a SIGKILL as a clean exit 0 (see
    `procutil.force_kill_tree`). Nothing observes an exit code after the session ends, so
    the wait buys nothing and the footgun is real.
    """
    from clauster import procutil

    strays = _fixture_stub_descendants()
    for proc in strays:
        procutil.force_kill_tree(proc.pid)
    return [proc.pid for proc in strays]


def pytest_sessionfinish() -> None:
    """Reap any fixture-stub subprocess the session left running (#1086).

    A hook rather than a session-scoped autouse fixture: this must run AFTER every
    fixture finalizer, including one that legitimately still owns a stub while tearing
    it down. Finalizer ordering would only approximate that; `sessionfinish` is after
    all of it by definition. It runs per xdist worker, which is where the stubs are.
    """
    reap_fixture_stub_strays()


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
        # One subdir per fake so each gets its own `rpc.pipe` (the Windows pipe-name
        # advertisement lives beside the socket path); harmless on POSIX.
        sub = sock_dir / f"d{len(started)}"
        sub.mkdir(exist_ok=True)
        sock = str(sub / "d.sock")
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
    """A ClausterConfig wired to the fake binary, a tmp state_dir, and trusted
    projects (so spawn isn't blocked on trust by default).

    Grants an own trust key to the projects_root AND each project under it: Claude
    Code 2.1.232+ (#1224) no longer honors a parent grant for nested git repos, so a
    git-repo project needs its own key to clear the spawn trust gate.
    """
    from clauster.config import ClausterConfig

    claude_json = tmp_path / "claude.json"
    projects = {str(projects_root.resolve()): {"hasTrustDialogAccepted": True}}
    projects.update(
        {
            str(c.resolve()): {"hasTrustDialogAccepted": True}
            for c in projects_root.iterdir()
            if c.is_dir()
        }
    )
    claude_json.write_text(json.dumps({"projects": projects}))
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


# ---------------------------------------------------------------------------
# Template database: run Alembic ONCE per worker, then copy the file per test.
# ---------------------------------------------------------------------------
# Every test that builds an app pays `Persistence()` = engine + `upgrade_to_head`,
# and on Windows that is **157.7 ms** (vs 7.9 ms on Linux — a 20x blowup, measured
# on a Windows VM at commit 5fef238). Across the ~2371 app-building tests that is
# the single largest slice of the Windows CI matrix's cost. The schema is identical
# for all of them, so migrating it thousands of times is pure waste: migrate once
# into a template file and copy that instead.
#
# Fidelity: the copy IS a real migrated database — the same bytes Alembic produced,
# at head — so a test cannot tell the difference by inspecting the schema.
#
# The fast path applies ONLY to a database that does not exist yet (the fresh-state_dir
# case). A test that pre-seeds a DB at an older revision, or one testing migration
# behaviour itself, has a non-empty file and takes the REAL upgrade untouched. Opt out
# explicitly with `@pytest.mark.real_migration` when a test needs Alembic to run even
# on a fresh database.

_TEMPLATE_DB: Path | None = None
_REAL_UPGRADE = db_bootstrap.upgrade_to_head


def _db_path_for(engine: Engine) -> Path | None:
    """The SQLite file an engine points at, or None if it isn't a plain file URL.

    ``:memory:`` counts as "not a file": `sqlite://` yields ``database == ":memory:"``, and
    treating that as a path would copy the template to a file literally called ``:memory:``
    in the CWD (or raise on Windows, where ``:`` is invalid in a name) while the engine's
    actual in-memory database stayed empty. Not reachable today — `Persistence` always goes
    through ``resolve_url`` — but production's own snapshot helper guards it explicitly, and
    matching that costs one clause.
    """
    database = getattr(getattr(engine, "url", None), "database", None)
    return Path(database) if database and database != ":memory:" else None


def _assert_template_is_migrated(path: Path) -> None:
    """Fail LOUDLY, at build time, if the template isn't a real migrated database.

    Without this the failure mode is silent and badly delayed: an un-checkpointed copy is
    a perfectly valid, *empty*, WAL-header database, and `import_legacy_json` returns early
    on a fresh `state_dir` — so `Persistence()` constructs fine and the blow-up surfaces
    hundreds of tests later as an unrelated `no such table`.
    """
    # `closing`, not a bare `with`: sqlite3.Connection's context manager commits the
    # transaction but does NOT close the connection, which leaks it (one ResourceWarning
    # per xdist worker) — and the engine-disposal fixture can't reach a raw connection.
    with closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # An un-checkpointed copy has NO tables at all, so read the revision defensively —
        # a bare OperationalError here would obscure what actually went wrong.
        revision = (
            conn.execute("SELECT version_num FROM alembic_version").fetchone()
            if "alembic_version" in tables
            else None
        )
    if not revision or "instances" not in tables:
        raise RuntimeError(  # noqa: TRY003 — a fixture-build failure wants the detail inline
            f"template database at {path} is not migrated "
            f"(alembic_version={revision!r}, tables={sorted(tables)})"
        )


def _template_db_path() -> Path:
    """Build (once per process) a migrated-to-head database and return its path."""
    global _TEMPLATE_DB
    if _TEMPLATE_DB is None:
        root = Path(tempfile.mkdtemp(prefix="clauster-db-template-"))
        # Registered BEFORE anything that can raise. `_assert_template_is_migrated` below
        # raises on a bad template, and with the registration after it the temp dir would
        # survive the session — the exact accumulation this PR exists to stop. Worse,
        # `_TEMPLATE_DB` stays None on that path, so the next test re-enters here and
        # re-migrates: one leaked dir and one full Alembic run per test thereafter, with the
        # useful first traceback buried under thousands of identical ones.
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        target = root / "template.db"
        engine = db_engine.create_db_engine(root / "build")
        try:
            _REAL_UPGRADE(engine, root / "build", backup_before_migrate=False)
            # `VACUUM INTO` — the same primitive `_snapshot_before_migrate` uses — writes a
            # consistent, sidecar-free file BY CONSTRUCTION. Copying the live DB file instead
            # would depend on `dispose()` having checkpointed the WAL: the engine runs in WAL
            # mode, and before a checkpoint the whole schema lives in `clauster.db-wal` while
            # the main file is a 4096-byte header. That happens to hold today, but it is a
            # coupling to production-code internals this fixture doesn't own — and its failure
            # is invisible (see `_assert_template_is_migrated`).
            # Bound parameter for the target, matching `_snapshot_before_migrate` exactly —
            # sqlite accepts any expression there, so this avoids hand-quoting a path into
            # SQL text. The previous quote-doubling was never exercised (the path is a
            # `mkdtemp` result), which is precisely what makes that kind of escaping easy to
            # break later without noticing. Verified locally on sqlite 3.46.1.
            with engine.connect() as conn:
                conn.exec_driver_sql("VACUUM INTO ?", (str(target),))
        finally:
            db_engine.dispose_engine(engine)
        _assert_template_is_migrated(target)
        _TEMPLATE_DB = target
    return _TEMPLATE_DB


def _templated_upgrade(
    engine: Engine, state_dir: Path, *, backup_before_migrate: bool = True
) -> None:
    """Seed a brand-new database from the template; otherwise run the real migration."""
    path = _db_path_for(engine)
    if path is None or (path.exists() and path.stat().st_size > 0):
        _REAL_UPGRADE(engine, state_dir, backup_before_migrate=backup_before_migrate)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_template_db_path(), path)


@pytest.fixture(autouse=True)
def _fast_migrations(request, monkeypatch):
    """Swap `upgrade_to_head` for the template-copy fast path (opt out per test)."""
    if request.node.get_closest_marker("real_migration"):
        return
    # Patch BOTH bindings: `persistence` imported the symbol by value at import time,
    # so patching only the `bootstrap` module would leave the real one in use there.
    monkeypatch.setattr(db_bootstrap, "upgrade_to_head", _templated_upgrade)
    monkeypatch.setattr(db_persistence, "upgrade_to_head", _templated_upgrade)
