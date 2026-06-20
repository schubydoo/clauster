"""Atheris fuzz harness for ``discovery._load_trusted_paths``.

``_load_trusted_paths`` ``json.loads`` ``~/.claude.json`` — external state written
by the ``claude`` CLI and freely editable by the user / other tooling. Its contract
(docstring) is to degrade EVERY malformed / missing / non-UTF-8 file to an empty
set, never raising. This harness found a real bug: a valid-JSON-but-non-dict top
level (``[]``, ``"x"``, ``5``) reached ``.get`` and raised ``AttributeError`` (the
#122 malformed-reader class). Any exception it surfaces is a contract violation;
it catches nothing.
"""

import sys
import tempfile
from pathlib import Path

import atheris

with atheris.instrument_imports():
    from clauster import discovery

# One reused temp path — the fuzzed bytes are the input, not the path — so the
# per-iteration file I/O stays cheap.
_CLAUDE_JSON = Path(tempfile.gettempdir()) / "clauster_load_trusted_paths_fuzz.json"


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    _CLAUDE_JSON.write_bytes(fdp.ConsumeBytes(fdp.remaining_bytes()))
    # Contract: total — degrades any malformed file to an empty set, never raises.
    discovery._load_trusted_paths(_CLAUDE_JSON)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
