"""Thin wrappers around the `claude` CLI binary.

PATH resolution (`resolve_binary`) and a version probe (`claude_version`).
Bridge spawn lives in `runner`; `claude agents --json` polling in `inspector`
(both reuse `resolve_binary`).
"""

from __future__ import annotations

import shutil
import subprocess


class ClaudeNotFound(RuntimeError):
    """Raised when the configured ``claude`` binary cannot be found on PATH."""


def resolve_binary(binary: str) -> str:
    """Resolve ``binary`` to an absolute path via PATH, or raise ``ClaudeNotFound``."""
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
