"""PyInstaller entry shim for the standalone ``clauster`` binary.

PyInstaller executes the analyzed script as the top-level ``__main__`` module with
no package context, so pointing it at ``clauster/__main__.py`` (which uses relative
imports like ``from . import …``) fails at runtime with "attempted relative import
with no known parent package". This shim is the analyzed script instead: it imports
the real entry point *absolutely*, which needs no package context, then delegates.
"""

import sys

from clauster.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
