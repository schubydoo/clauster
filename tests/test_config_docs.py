"""The generated config-reference tables in docs/configuration.md stay in sync.

Guards against the drift that prompted the generator: a config field added or
changed in ``config.py`` without regenerating the docs page. The generator is the
single source of truth (descriptions live in ``Field(description=...)``), so this
just runs it in ``--check`` mode and fails if the committed page is stale.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_config_reference_docs_are_up_to_date():
    result = subprocess.run(
        [sys.executable, "scripts/gen_config_reference.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "docs/configuration.md is out of sync with the config models. "
        "Run: python scripts/gen_config_reference.py\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
