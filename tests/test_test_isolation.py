"""The suite's own isolation and growth checks must be able to fail (TASK-546, TASK-552)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from _growth import assert_linear_time, count_ops
from clauster import claude_cli
from conftest import FAKE_CLAUDE, _path_without_claude, needs_symlink

_SUFFIX = ".cmd" if sys.platform == "win32" else ""


def _tool(directory: Path, name: str) -> Path:
    """Create an executable stub called ``name`` (a ``.cmd`` on Windows)."""
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / f"{name}{_SUFFIX}"
    tool.write_text("@echo off\n" if _SUFFIX else "#!/bin/sh\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return tool


def test_the_suite_cannot_resolve_a_real_claude() -> None:
    assert shutil.which("claude") is None


def test_a_bare_claude_name_fails_as_it_does_on_ci() -> None:
    with pytest.raises(claude_cli.ClaudeNotFound):
        claude_cli.resolve_binary("claude")


@pytest.mark.skipif(sys.platform == "win32", reason="runs a POSIX shell")
def test_a_subprocess_cannot_resolve_a_real_claude() -> None:
    done = subprocess.run(
        ["/bin/sh", "-c", "command -v claude"], capture_output=True, text=True, check=False
    )
    assert done.returncode != 0, done.stdout


def test_a_test_can_opt_in_to_a_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", f"{FAKE_CLAUDE.parent}{os.pathsep}{os.environ['PATH']}")
    found = shutil.which("claude")
    assert found is not None
    assert Path(found).parent == FAKE_CLAUDE.parent


@needs_symlink
def test_a_directory_with_claude_keeps_its_other_tools(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _tool(bin_dir, "claude")
    uv = _tool(bin_dir, "uv")
    other = tmp_path / "other"
    _tool(other, "git")
    path = os.pathsep.join([str(bin_dir), str(other)])
    assert shutil.which("claude", path=path) is not None  # the control: it is really there

    hidden = _path_without_claude(path, tmp_path / "mirror")

    assert shutil.which("claude", path=hidden) is None
    found = shutil.which("uv", path=hidden)
    assert found is not None and Path(found).resolve() == uv.resolve()
    assert shutil.which("git", path=hidden) is not None
    assert str(other) in hidden.split(os.pathsep)  # a directory without claude is untouched


def test_a_directory_that_cannot_be_mirrored_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    _tool(bin_dir, "claude")
    _tool(bin_dir, "uv")

    def refused(*_args: object, **_kwargs: object) -> None:
        raise OSError("no symlink privilege")

    monkeypatch.setattr(Path, "symlink_to", refused)
    hidden = _path_without_claude(str(bin_dir), tmp_path / "mirror")
    assert shutil.which("claude", path=hidden or os.devnull) is None
    assert str(bin_dir) not in hidden.split(os.pathsep)


def test_count_ops_is_deterministic_and_grows_with_the_work() -> None:
    def walk(size: int) -> None:
        total = 0
        for value in range(size):
            total += value

    small = count_ops(lambda: walk(100))
    assert small == count_ops(lambda: walk(100))
    assert 3 * small < count_ops(lambda: walk(400)) < 5 * small


def test_count_ops_stops_a_runaway_call_at_its_budget() -> None:
    reached: list[int] = []

    def runaway() -> None:
        for value in range(10**9):
            reached.append(value)

    with pytest.raises(pytest.fail.Exception, match="past its budget of 1000"):
        count_ops(runaway, budget=1_000)
    assert len(reached) < 1_000


def test_count_ops_budget_is_not_swallowed_by_a_seam_that_catches_exception() -> None:
    def seam() -> None:
        try:
            for _ in range(10**9):
                pass
        except Exception:  # noqa: S110
            pass

    with pytest.raises(pytest.fail.Exception, match="past its budget"):
        count_ops(seam, budget=100)


def test_count_ops_budget_survives_a_seam_that_swallows_base_exception() -> None:
    def seam() -> None:
        try:
            for _ in range(10**9):
                pass
        except BaseException:  # noqa: S110
            pass

    with pytest.raises(pytest.fail.Exception, match="past its budget"):
        count_ops(seam, budget=100)


def _sentinel_tracer(*_args: object) -> None:
    """Stand in for a coverage tracer, so a missing restore cannot pass as `None is None`."""


@pytest.mark.parametrize("failing", [False, True])
def test_count_ops_restores_the_previous_tracer(failing: bool) -> None:
    previous = sys.gettrace()

    def call() -> None:
        if failing:
            raise ValueError("boom")

    sys.settrace(_sentinel_tracer)
    try:
        if failing:
            with pytest.raises(ValueError, match="boom"):
                count_ops(call)
        else:
            count_ops(call)
        assert sys.gettrace() is _sentinel_tracer
    finally:
        sys.settrace(previous)


def test_assert_linear_time_passes_a_linear_scan() -> None:
    def make(size: int):
        text = "a" * size
        return lambda: text.count("b")

    assert_linear_time(make, unit=20_000)


def test_assert_linear_time_fails_a_quadratic_scan() -> None:
    # The positive control: each step copies the rest of the input in a C builtin, so a count
    # of Python lines would call this linear. Only the time ratio can see it.
    def make(size: int):
        text = "a" * size

        def quadratic() -> None:
            for index in range(0, size, 8):
                text[index:].count("b")

        return quadratic

    with pytest.raises(pytest.fail.Exception, match="not linear"):
        assert_linear_time(make, unit=4_000)


def test_assert_linear_time_restores_the_collector_and_tracer() -> None:
    import gc

    previous = sys.gettrace()
    assert gc.isenabled()
    calls: list[int] = []

    def make(_size: int):
        def fail() -> None:
            calls.append(1)
            if len(calls) > 1:  # the untimed first run passes, the timed one fails
                raise ValueError("boom")

        return fail

    sys.settrace(_sentinel_tracer)
    try:
        with pytest.raises(ValueError, match="boom"):
            assert_linear_time(make, unit=1)
        assert sys.gettrace() is _sentinel_tracer
    finally:
        sys.settrace(previous)
    assert gc.isenabled()
