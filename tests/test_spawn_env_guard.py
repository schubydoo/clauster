"""Every process Clauster spawns gets an explicit, scrubbed environment (issue 1708).

``procutil.child_env`` is the one place that removes Clauster's secrets and the standalone
binary's PyInstaller state from a child's environment. A spawn that passes no ``env=``
silently inherits ``os.environ`` — both the secrets and, in the binary, a library path into
its temporary unpack directory — so this walks the source tree's AST and fails on one.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "clauster"

_SPAWNERS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "os": {
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
        "system",
        "popen",
    },
}

# (file, function) pairs allowed to spawn without a child_env. Each needs a reason.
_EXEMPT = {
    # The in-place restart re-execs Clauster ITSELF: it needs its own secrets (session
    # secret, password hash) and stays inside the running bundle, so it keeps os.environ.
    ("__main__.py", "_reexec"),
}


def _spawn_calls(tree: ast.AST):
    """Yield ``(enclosing function name, call)`` for each module-qualified spawn call."""
    stack: list[tuple[ast.AST, str]] = [(tree, "<module>")]
    while stack:
        node, func = stack.pop()
        for child in ast.iter_child_nodes(node):
            is_def = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.attr in _SPAWNERS.get(child.func.value.id, ())
            ):
                yield func, child
            stack.append((child, child.name if is_def else func))


def _violations(rel: str, tree: ast.AST) -> list[str]:
    found = []
    for func, call in _spawn_calls(tree):
        if (rel, func) in _EXEMPT:
            continue
        where = f"{rel}:{call.lineno} {ast.unparse(call.func)} in {func}()"
        env = next((kw.value for kw in call.keywords if kw.arg == "env"), None)
        if env is None:
            found.append(f"{where}: no env=")
        elif "os.environ" in ast.unparse(env):
            found.append(f"{where}: env built from os.environ, not procutil.child_env")
    return found


def _source_trees():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def test_every_spawn_passes_an_env_not_built_from_os_environ():
    found = [v for rel, tree in _source_trees() for v in _violations(rel, tree)]
    assert found == []


def test_the_guard_sees_the_real_spawns():
    # Positive control: a walk that found nothing would pass the test above vacuously.
    seen = {(rel, func) for rel, tree in _source_trees() for func, _ in _spawn_calls(tree)}
    assert ("__main__.py", "_reexec") in seen
    assert ("bridge_launch.py", "_popen") in seen
    assert any(rel == "claustrum_daemon.py" for rel, _ in seen)  # an asyncio spawn
    assert len(seen) > 20


def test_the_guard_flags_a_missing_and_an_os_environ_env():
    tree = ast.parse(
        "import os, subprocess\n"
        "def bare():\n    subprocess.run(['x'])\n"
        "def copied():\n    subprocess.Popen(['x'], env={**os.environ, 'K': 'v'})\n"
        "def clean():\n    subprocess.Popen(['x'], env=procutil.child_env())\n"
        "def _reexec():\n    os.execv('x', ['x'])\n"
    )
    found = _violations("probe.py", tree)
    assert len(found) == 3  # `_reexec` is only exempt in __main__.py
    assert any("bare()" in v and "no env=" in v for v in found)
    assert any("copied()" in v and "os.environ" in v for v in found)
    assert any("_reexec()" in v for v in found)
    assert not any("clean()" in v for v in found)
