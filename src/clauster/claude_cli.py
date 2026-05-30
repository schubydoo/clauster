"""Thin wrappers around the `claude` CLI binary.

v0.1 needs only a version probe; bridge spawn / `claude agents --json` polling
land in later sprints (features 2-4).
"""

from __future__ import annotations

import shutil
import subprocess


class ClaudeNotFound(RuntimeError):
    pass


def resolve_binary(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        raise ClaudeNotFound(f"claude binary {binary!r} not found on PATH; Clauster cannot start.")
    return resolved


def claude_version(binary: str) -> str:
    """Return the version string from `claude --version` (e.g. '2.1.153')."""
    resolved = resolve_binary(binary)
    proc = subprocess.run(
        [resolved, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    # Output looks like: "2.1.153 (Claude Code)"
    return proc.stdout.strip().split()[0]
