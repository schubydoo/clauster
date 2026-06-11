from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Make importable fixture modules (e.g. fake_claustrum) reachable by bare name.
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))

# Windows CreateProcess can't launch the extensionless Python stubs, so on Windows
# the fixtures expose a same-named `.cmd` wrapper that shells out to `python`.
WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""


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
        # encoding="utf-8" matches how load_config reads it (config.py); without it
        # the platform default (cp1252 on Windows) mangles non-ASCII symbols like €.
        cfg.write_text(f"projects_root: {projects_root}\n{extra}", encoding="utf-8")
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
